# Stage A lease window — design (#280)

## Goal

Stop Stage A from dropping the `MiniGrafDb` handle once per commit. Each drop
runs a full O(graph size) checkpoint inside minigraf's `Drop for Inner`,
outside `_CheckpointPolicy`'s duty gate and invisible to the trace's
`ckpt_d_seconds`. On minigraf 2.0.2 a full-history run spent 1036–1905 s in
drops (35–46% of wall clock, `results/239-minigraf-2.0.2-ab.json`).

The out-of-process auto-memory hooks must keep a bounded path to the graph
lock. That constraint is why the issue's original direction (one lease for
the whole run) is wrong: `finalize_hook.py` retries for ~0.75 s of sleep and
then silently discards its write.

This is #280's own **fallback** (a batched lease with a bounded hold), which
Stage B has shipped since #222 phase 5 item C. The preferred direction,
suppressing the drop checkpoint via `OpenOptions.wal_checkpoint_threshold`,
stays blocked on minigraf#322 (v3.1.0).

## Evidence this is worth doing (spike, 2026-09-28)

A throwaway spike emulated a Stage A window (25 commits / 2 s / 0.1 s pause)
by pinning an extra lease while `phase == "converging"`, with no
`mcp_server.py` edits. The runs used this repo's history on minigraf 2.0.2,
with the two arms interleaved. Every run finished `complete`, and repeat runs
agreed within 0.3%.

| | per-commit lease | window |
|---|---|---|
| 600 commits: Stage A time | 277 s | 196 s (−29%) |
| 600 commits: total time | 726 s | 644 s (−11%) |
| Stage A handle drops | 601 | 70 |
| Stage A drop + reopen time | 110 s | 15 s |
| Stage A auto-checkpoints | 1 | 1 |
| 250 commits (3 runs each): Stage A time | 19.5 s | 15.7 s (−20%) |

**The one unknown that could have voided the premise did not.** The drop
also resets minigraf's 1000-WAL-entry auto-checkpoint counter
(`src/db.rs:1275`). If a window ran past 1000 transacts, auto-checkpoint
would fire and each firing would cost the same as a drop. Stage A makes about
21 WAL writes per commit, and `_CheckpointPolicy`'s explicit checkpoints also
reset the counter, so a 25-commit window stays well under 1000. The
auto-checkpoint detector (the WAL file shrank after a transact) was checked
with a positive control first: with explicit checkpoints suppressed and one
run-long window, it caught 3 auto-checkpoints in 3940 writes.

## Approaches considered

1. **Window object wrapping a nested lease (chosen).** A small `_LeaseWindow`
   holds its own `_db_lease_async_committing_index` lease across commits.
   Stage A's existing per-commit `async with` stays where it is and simply
   *joins* that lease at refcount 1→2, so it no longer drops the handle. The
   window decides when to release for real. This follows the documented
   nesting pattern ("a handler that takes its own lease nests at count 1->2
   rather than opening a second handle"). The per-commit dispatch code, both
   `except` paths and the #342/#326 claim gating stay untouched.
2. **Restructure Stage A into an outer window loop, as Stage B does.** This
   re-indents the whole ~200-line dispatch body. Its `continue` (extraction
   failure) and `break` (shutdown) would then have to be re-expressed across
   two loop levels, a much larger diff through the code with the most
   load-bearing comments in the file. Rejected.
3. **Hold one lease for all of Stage A.** This is the issue's original
   proposal, rejected in its own correction comment: it silently discards
   every auto-memory write for the length of Stage A.

## Design

### `_LeaseWindow` (new, `mcp_server.py`, beside `_db_lease_async_committing_index`)

```python
class _LeaseWindow:
    def __init__(self, loop, write_executor, index_con, *,
                 max_commits, max_seconds, pause_seconds): ...
    async def ensure_open(self) -> None      # lazy; no-op if already open
    def note_commit(self) -> None            # one DB-touching commit applied
    async def maybe_yield(self) -> float     # boundary: close + pause; returns seconds spent
    async def close(self) -> None            # close, NO pause; idempotent
```

- **The window's own lease is `_db_lease_async_committing_index`**, entered
  through a `contextlib.AsyncExitStack`. So a real release (1→0) always
  commits `index_con` first, which is #347's invariant, enforced by
  construction rather than by call-site care. The per-commit lease inside
  still commits on every exit exactly as today, so nothing changes about when
  index rows become durable.
- **It opens lazily**, on the first commit that reaches write dispatch. An
  empty Stage A, or a run of extraction failures, opens no handle. That
  matches today, where those paths take no lease.
- **Boundary rule:** `maybe_yield` closes the window when it is open and
  either `count >= max_commits` or `monotonic() - opened_at >= max_seconds`.
  It then does `await asyncio.sleep(pause_seconds)` outside any lease. Never
  `time.sleep` (#99); `_forbid_blocking_sleep_on_event_loop` enforces this.
- **`close()` has no pause.** It is used at the end of Stage A and on every
  exceptional exit.

### Wiring in `_run_ingestion` Stage A

- Build the window just before `run_progress.stage_a_started()`. Wrap the
  `while pending:` loop in `try: ... finally: await window.close()`. This is a
  one-level re-indent of the loop body, reviewable with `git diff -w`. The
  `finally` covers the shutdown `break`, `BrokenProcessPool` and any other
  propagating exception, so no lease can leak past Stage A. A leaked lease
  would make the outer `finally`'s final checkpoint fail with "Database is
  already open in this process", the hazard the Stage B abort path documents.
- **The boundary sits at the loop head, after the shutdown check and before
  `_trace_t_await` is read.** That is between two commits whatever path the
  previous one took (written, write failed, or extraction failed). It never
  pays a pause after the last commit, because the head only runs while
  `pending` is non-empty. It never pays a pause on shutdown either, because
  the shutdown `break` comes first.
- `await window.ensure_open()` immediately before the existing per-commit
  `async with _db_lease_async_committing_index(...)`, inside the `apply_s`
  span. `apply_s` already documents that it includes lease acquire.
- `window.note_commit()` after the per-commit `async with` exits, on both the
  success path and the write-failure path, since either one touched the DB.

### Constants

Stage A **reuses** `_SWEEP_YIELD_COMMITS` (25), `_SWEEP_YIELD_SECONDS` (2.0)
and `_SWEEP_YIELD_PAUSE_SECONDS` (0.1). All three measure things about the
hooks, not about the sweep: how long a hook can be locked out, and how long
the lock stays genuinely free. A second set of knobs would be two answers to
one question. The spike measured these exact values. The names stay (renaming
churns every Stage B test and the CLAUDE.md prose that cites them); the
comment above them is updated to say they govern both stages. Its "When
#280 lands (blocked on upstream minigraf#322) … N can safely go to 1"
paragraph is reworded to name minigraf#322, not #280, as the condition: #280
is landing via the fallback, which leaves the drop checkpoint in place, so N
still cannot go to 1 after this change.

### Trace (#260)

The cost #280 exists to expose must not go invisible again. Add one field to
every `_IngestTrace` record:

- **`yield_s`**: seconds spent in `maybe_yield` boundaries since the
  previously emitted record (window drop + pause). It accumulates across
  commits that emit no record (extraction or write failure), so the sum over
  the trace equals total boundary time. The one exception is the final
  `close()` after the loop, which drops the handle after the last record has
  been emitted. That is one drop per run; it is stated in the field's comment
  rather than fixed.

`apply_s` keeps its definition (acquire + write + release). Its release no
longer includes a drop except on the first commit of a window, where it now
includes the *open*. The #260 M2 comment at the dispatch site is rewritten to
say so. `evals/at_scale/probe_per_commit_cost.py` reads fields with `.get`,
so old traces keep parsing. It gains a `yield_s` total so the drop cost stays
attributable.

## What does not change

- Stage B's window loop. `_LeaseWindow` could replace it, but that is a
  refactor of shipped, tested code with no cost win. Out of scope; noted as a
  possible follow-up.
- The preload lease, skipped-span flush, lineage fold, tags/last-run write
  and the final checkpoint lease. Each is one lease per run.
- The single-handle invariant. The window takes an ordinary lease and the
  refcount stays authoritative. Nothing reuses a handle outside
  `_DbLeaseManager`.
- No `GRAPH_FORMAT_VERSION` bump and no migration. This writes the same facts
  in the same order and changes only when the handle drops. The
  write-sequence parity oracle
  (`probe_forward_apply_write_parity.py`) is **not** a fit as a gate: it
  records `_db_checkpoint_gated` calls, and the duty gate's decisions depend
  on wall time, which this change deliberately alters. Graph equivalence is
  checked with `fact_audit` instead (below).

## Testing (TDD, real backend per `docs/testing-conventions.md`)

New class `TestStageAYieldsTheLock`, reusing `TestStageBYieldsTheLock._prepare`:

1. **Handle drops are amortized.** A real Stage A run over n=12 commits with
   `_SWEEP_YIELD_COMMITS=4` and `_SWEEP_YIELD_SECONDS` set huge should see
   1→0 drops during `phase == "converging"` equal to `ceil(applied / 4)`.
   Ablation: with the `ensure_open` call removed (per-commit lease again), it
   reads `applied`.
2. **The clock alone closes a window.** Count set to 10**9 and seconds small,
   with a patched slow `_forward_apply` → more than one window. This mirrors
   Stage B's `test_the_clock_alone_closes_a_window`: short-circuit evaluation
   of the `or` means a count-driven test never evaluates the clock operand.
3. **A hook writing mid-Stage A lands in graph and index and the run
   completes.** A real separate process calls `handle_minigraf_transact` at
   shipped defaults, with Stage A kept slow enough to span several windows.
   Ablation: a window that never closes (count and seconds both huge),
   with Stage A kept longer than the hook's ~2.6 s acquire budget → the hook
   fails. That failure is deterministic. Dropping only the pause is not a
   usable ablation, because a hook already blocked in minigraf's polling
   `open()` can still win the bare release instant sometimes.
4. **No lease leaks on an exceptional exit.** `BrokenProcessPool` injected
   mid-window → `_lease_manager.lease_count == 0` after `_run_ingestion`
   returns, and the final checkpoint ran. A shutdown mid-window gives the same
   result, with no pause paid.
5. **Every real release commits the index.** The existing
   `test_no_lease_is_released_with_an_open_index_transaction` already checks
   `index_con.in_transaction` at each release. It must keep passing, and it
   must actually observe a window close. The test asserts it saw one, because
   a check that never saw a window close proves nothing about one.
6. **`yield_s`**: its sum over the trace is > 0 on a multi-window run, and
   equals the patched sum of boundary durations.

**Existing test needing adaptation:**
`test_a_hook_writing_between_stage_a_leases_lands_in_both` widens the gap
between per-commit leases. Under a window, that gap is a join rather than a
release. It will patch `_SWEEP_YIELD_COMMITS = 1` so it keeps testing what it
tests (#347's per-release index commit). With N=1 every per-commit exit is
also a window close, which preserves exactly its original mechanics.

Every regression test is ablation-proven before it is trusted.

## Measurement (acceptance)

The spike's instrument is promoted into the repo rather than left in a
scratchpad (standing rule: keep the instrument behind a decision).
`probe_lease_drop_cost.py` gains:

- a per-phase split (preload/converging/sweeping) of opens, drops and times;
- the WAL-shrink auto-checkpoint counter, with its positive control
  documented;
- an explicit-checkpoint counter.

Acceptance runs, with master and the branch interleaved on one machine and
**both arms run back to back**, because absolute numbers drift between
batches:

- 600-commit slice, ≥2 runs per arm. Stage A opens and drops per commit
  ≈ 1/25 (not ~1.0). The Stage A wall-time reduction reproduces the spike
  (≥20%). Stage A auto-checkpoints stay ≤ 1 per run.
- One full-history run per arm. This is the issue's "not measured yet:
  full history". Report Stage A drop totals and auto-checkpoint counts at
  full graph size. If auto-checkpoints become frequent at depth, that is a
  finding to record, not a reason to widen scope.
- `fact_audit` divergence 0 on both arms' graphs, and identical
  `:type/commit` counts.

Results go to `evals/at_scale/results/280-stage-a-window-ab.json`.

#280's acceptance bullet about the `apply_s` intercept is #260's re-fit and
stays with #260. This PR closes #280 on the drops-per-commit and wall-clock
evidence and says so explicitly.

## Docs

- CLAUDE.md: update "Dropping the handle is not free … Ingestion currently
  drops it ~1.02 times per commit" to the measured windowed figures. Extend
  the Stage B yield section to say the window now covers Stage A too, and
  reword the "When #280 lands … N can safely go to 1" claim to name
  minigraf#322 in both CLAUDE.md and the constant's comment.
- SKILL.md: no change. No tool, argument or query syntax changes.

## Out of scope, recorded

- **Stage B's auto-checkpoints.** Stage B makes about 237 WAL writes per
  commit, so its 25-commit window already crosses 1000 and minigraf
  auto-checkpoints mid-window: 77 firings = 24 s, plus 22 s of drops, at 600
  commits (spike). That is small against Stage B's ~450 s, and not #280's
  target. File it as a follow-up if the full-history run shows it growing.
- **Unifying Stage B onto `_LeaseWindow`.**
- **minigraf#322** remains the real fix. When it lands, the window can shrink
  to 1 commit.
