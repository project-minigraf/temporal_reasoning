# Ingestion status and progress reporting (#222 phase 4)

Status: design approved 2026-09-11. Phase 4 of #222's five. #222 stays open
for phase 5 (hardening); the PR says "Part of #222" and carries no closing
keyword.

Scope is the "middle" cut of the July 2026 status comment on #222: fix the
measured defects, add per-stream counters with a rate, add the two
termination signals with coverage numbers, and add a benchmark
`--progress-interval`. Explicitly out: a tip-moving flag (there is no live tip
polling — `Ht` is fixed per run), a raw interval-registry listing, ETA,
progress logging from the MCP server itself, and every phase-5 item
(including Stage B holding one lease for the whole sweep).

## Problem

`minigraf_ingest_status` reports one scalar pair, `processed / total`, and
`processed` means two things at once: it is SEEDED with
`prior_ingested = _count_commit_entities(db)` (`mcp_server.py`, `_run_ingestion`)
and then incremented once for every position the walk RETIRES this run — a
skip, an extraction failure, or a write dispatch, successful or not. It is
then displayed as progress against `total`, which it can exceed, and it
cannot distinguish a run that lost a commit from one that did not.

### Measured on master `54ae878` (throwaway probe, real backend)

Small linear repos, `_run_ingestion` driven directly, `_ingest_progress`
sampled inside `_reverse_apply` / `_forward_apply` / `_correction_sweep_apply`.

| Scenario | What status reported | What was true |
|---|---|---|
| A1 fresh 10 commits | `complete`, 10/10, `last_commit` = `8d04946…` | HEAD was `8b5118e…`; `last_commit` names the meeting point |
| A2 +1 commit | `complete`, 11/11 | correct — #325's retention removed the headline 145% for tip growth |
| A3 no-op rerun | `complete`, 11/11, `last_commit` = `49049fc…` | HEAD `6281340…`; `last_commit` now names the forward watermark |
| B1 20 commits, shutdown mid-Stage-A | `stopped`, 8/20 | correct |
| B2 resume | `complete`, 20/20; all 10 Stage B steps sampled at **20/20** | the sweep's whole duration shows 100% and no sweep counter |
| C1 20 commits, one reverse write injected to fail | **`complete`, 20/20** | the graph holds **19** commits; Stage B never ran (gap never read closed, reverse floor) |
| C2 rerun | `complete`, **28/20 (140%)**; `:last-run-at :total-ingested` persisted as **28** | 9 positions re-walked below the floor |
| C3 rerun | `complete`, 20/20 | correct |

So of #222's inherited list: the 145% is GONE on ordinary tip growth and on
interrupted resume, but SURVIVES on every re-walk path (reverse floor after a
failed write, a count-break discard, a torn-position re-walk); the Stage B
freeze is confirmed; `last_commit` is wrong and unstable. C1 is a finding the
list did not carry: `processed == total` HIDES a lost commit and a starved
sweep, on a run reporting `complete`.

## Decisions (settled before design)

1. **Scope:** middle cut, as above.
2. **`processed` is removed, not redefined.** So are `processed_this_run`,
   `positions_skipped` and `positions_skipped_this_run`. There are no external
   consumers, so the break is cheap; every internal consumer is re-pointed.
3. **Structure:** a pure progress model in its own module, not inline dict
   arithmetic and not status derived from graph queries at poll time (which
   would contend with ingestion on `_db_native_lock`, add latency the
   benchmark measures, and be staler than the in-memory allocator).
4. **`visibility` is frontier-verified coverage**, which may dip between runs
   after a failure, with a separate per-run work counter that cannot.

## The status contract

While a run is active, or after it ends in this process:

```jsonc
{
  "ok": true, "status": "running", "phase": "converging",
  "total": 20, "current_commit": "…", "prior_ingested": 19,
  "error": null, "error_at": null, "owner_pid": null,
  "index_cross_check": {…}, "checkpoint_summary": {…},
  "this_run": {
    "to_retire": 9, "retired": 4, "written": 4, "skipped": 0, "failed": 0,
    "started_at": "2026-09-11T10:00:00.000Z",
    "last_progress_at": "2026-09-11T10:00:41.213Z",
    "seconds_since_progress": 3.1
  },
  "streams": {
    "forward": {"state": "running", "retired": 2, "written": 2, "failed": 0,
                "rate_per_min": 12.0, "last_at": "…"},
    "reverse": {"state": "running", "retired": 2, "written": 2, "skipped": 0,
                "failed": 0, "rate_per_min": 11.8, "last_at": "…"},
    "sweep":   {"state": "waiting", "swept": 0, "to_sweep": null,
                "blocked_reason": null, "rate_per_min": null, "last_at": null}
  },
  "visibility": {"verified": 15, "total": 20, "complete": false},
  "lineage":    {"confirmed": 11, "total": 20, "complete": false,
                 "confirmed_through": "<hash>"}
}
```

Every key `_ingest_progress` carries today other than the four removed ones
is unchanged (`status`, `phase`, `total`, `current_commit`, `prior_ingested`,
`error`, `error_at`, `owner_pid`, `stale`, `index_cross_check`,
`checkpoint_summary`, and the idle-only `last_run_at` / `last_commit` /
`total_ingested`).

### Field semantics

- **`this_run`** is work-based. `to_retire` is the number of positions in the
  allocator's gap at load; every one is retired at most once per run
  (`_RoundRobinClaimer.next_claim` returns None only on `is_gap_empty()`, and
  the allocator never serves a claimed position), so `retired <= to_retire`
  always, and `retired == to_retire` on a run that completes Stage A.
  `retired = written + skipped + failed`, where `failed` covers both an
  extraction failure and a write failure — the two lines `_SKIPPED_COMMIT_RE`
  already matches.
- **`streams.forward` / `streams.reverse`** — `state` is `not-started`
  (Stage A not yet begun), `running`, `done` (Stage A ended and the stream
  retired at least one position), or `not-needed` (the gap was empty at load,
  or Stage A ended with zero claims for that stream). `rate_per_min` is the
  stream's `retired` over wall time since Stage A began; null before the first
  retirement.
- **`streams.sweep`** — `state` is `waiting` (Stage A running), `running`,
  `done` (the sweep reached frontier-high's `:hi-hash`, whether this run swept
  anything or not), `not-needed` (no frontier-high: nothing provisional to
  confirm), `blocked` (with `blocked_reason`), `not-run` (Stage A ended
  stopped or in error, so Stage B was never entered), `stopped` (shutdown
  requested mid-sweep), or `aborted` (a sweep step raised; the next run
  re-selects that commit, as today). `blocked_reason` is one of `gap-open`,
  `fragmented`, `stale-bound`, `metadata-mismatch`. `to_sweep` is known once
  Stage B plans.
- **`visibility.verified`** = `(total − to_retire) + written + skipped`: the
  positions the frontier can prove complete (loaded intervals are the #326
  completion witness; skipped positions are covered by an archived region),
  plus the positions this run wrote. At run end it equals `total − failed`
  when Stage A completed. **`visibility.complete`** is
  `to_retire == 0` at load, and at run end `retired == to_retire and
  failed == 0`.
- **`lineage.confirmed`** = the size of the UNION `[0, lineage_pos] ∪
  [fh_lo, sweep_pos]`, where `lineage_pos` is the position of
  `:ingestion/lineage-confirmed-through` (contiguous from C0) and
  `[fh_lo, sweep_pos]` is the part of frontier-high's region at or below
  `:ingestion/correction-sweep-through`. The union, not a sum: after a
  finished run the fold has already moved `lineage_pos` to the old tip while
  frontier-high and the sweep watermark still describe `[fh_lo, old tip]`, so
  a sum would count that region twice on the next run. After the fold it is
  `total`. If either watermark's hash does not resolve in
  this run's linearization, `confirmed` is **null**, never a guessed number
  (#316's absent-is-not-zero idiom). **`lineage.complete`** is
  `confirmed == total`. **`lineage.confirmed_through`** is the hash itself, so
  a caller can ask "is this region trustworthy?" without doing position math.
- **`status: complete`** keeps its meaning — the run finished without a stop
  or an error. Ingestion is DONE when `visibility.complete and
  lineage.complete`. SKILL.md says so explicitly; C1 is the case where the two
  disagree.

### Idle

With no run in this process the response is as today (`last_run_at`,
`last_commit`, `total_ingested`, read from the graph under a lease), plus
`lineage_confirmed_through` (one extra point query in the same lease).
`visibility` is not reported idle: it needs the git linearization.

### `last_commit` and `:total-ingested`

`_last_run_write` is called with `linearization[-1]` — the tip of the branch
this run covered — instead of `last_hash`, which on a converging run is
whichever commit Stage A applied last (the meeting point) and on a no-op run
is the forward watermark. Nothing resumes from it; its one consumer is the
idle status display.

`:total-ingested` is written from `_count_commit_entities(db)` at the same
site instead of from the walk counter. One count-distinct query per completed
run. Its only production reader was already `handle_minigraf_ingest_status`,
which ignores it in favour of the live count, but a persisted 28-of-20 is a
wrong fact in the graph.

## `RunProgress` (`ingest_progress.py`)

A new top-level module, added to `[tool.setuptools] py-modules`
(`tests/test_packaging.py` enforces it). Pure: no DB, no git, no import of
`mcp_server`. Clocks are injected (a monotonic clock for rates and a wall
clock for the ISO timestamps) so the model is unit-testable without sleeping.

### Construction

Built once per run, in the `db_lease_async()` block that already calls
`_frontier_load` and `_completed_regions_load`, from:

- `total` — `repo_total`
- `to_retire` — the sum of `allocator._unclaimed()` hole sizes at load
  (exposed as a public `FrontierAllocator.unclaimed_count()` rather than
  reaching into the private method)
- `lineage_pos` — position of `_lineage_confirmed_through_query(db)` in the
  linearization, `-1` if unset, or unresolvable (→ null lineage)
- `sweep_region` — frontier-high's `lo` position (from the allocator's base
  interval) and the position of `_correction_sweep_through_query(db)`, when
  that hash lies inside frontier-high's range

Two extra point queries per run. Frontier-high's bounds come from the
allocator already in hand.

It is published to a module global `_ingest_run_progress: Optional[RunProgress]`,
reset to None at the top of `_run_ingestion` (so a refused or early-failing
run never shows a previous run's numbers — the same reasoning as
`index_cross_check`) and set once construction succeeds. Tests and harnesses
that assign `_ingest_progress` dicts directly keep working; the handler
renders `snapshot()` only when the global is non-None.

### Events

Each replaces an existing `_ingest_progress[...]` mutation in `_run_ingestion`:

| Event | Site |
|---|---|
| `stage_a_started()` | where `phase = "converging"` is set |
| `retired("rev", "skipped", pos)` | the `_skip_claim` path in `submit_next` |
| `retired(tag, "failed", pos)` | the extraction-failure `except` |
| `retired(tag, "written" \| "failed", pos)` | after the write dispatch, keyed on `_trace_write_ok` |
| `forward_confirmed(pos)` | same site, `tag == "fwd"` and the write succeeded — mirrors the `:ingestion/lineage-confirmed-through` `_forward_apply` just persisted |
| `stage_a_finished(completed)` | after the `while pending` loop |
| `sweep_planned(to_sweep=…)` / `sweep_planned(blocked_reason=…)` / `sweep_planned(done=True)` / `sweep_planned(not_needed=True)` | Stage B, before its loop |
| `swept(pos)` | after `_correction_sweep_through_update` succeeds |
| `sweep_ended("stopped" \| "aborted")` | the shutdown check and the sweep-step `except` |
| `folded()` | after `_lineage_confirmed_through_update` in the fold |
| `ended(status)` | the three terminal paths |

`snapshot(now)` computes `seconds_since_progress` at poll time, so a run that
has stopped retiring anything shows it growing rather than a frozen rate.

### Sweep plan

`_correction_sweep_select_position` has seven `None` returns a caller cannot
tell apart. Its body moves to `_correction_sweep_next(...) -> (selected,
reason, start_pos, ceiling_pos)`, and the old name becomes a one-line wrapper
returning `selected`, so every existing test of it is unchanged and still
guards the selection logic. Reasons map as:

| `_correction_sweep_next` reason | sweep state |
|---|---|
| no frontier-high | `not-needed` |
| reached frontier-high's `:hi-hash` | `done` |
| gap still open | `blocked: gap-open` |
| provisional side fragmented (#325) | `blocked: fragmented` |
| a boundary hash does not resolve (three sites) | `blocked: stale-bound` |
| `commit_metadata` violates its contract | `blocked: metadata-mismatch` |

Stage B calls `_correction_sweep_next` once before its loop: the result both
plans (`to_sweep = ceiling_pos − start_pos + 1`) and is the loop's first
iteration, so planning costs no queries.

## Consumers

Re-pointed from `processed`, each pinned by a test asserting the value equals
the old formula on the same run:

- `evals/at_scale/run_ingestion_benchmark.py` — `commits_ingested` and the
  `walk_claimed` it hands `commit_census` become `prior_ingested +
  this_run.retired`. The metrics JSON key `commits_ingested` keeps its name,
  so `report.py` and every recorded `results/` file stay comparable.
- `evals/at_scale/probe_resume_census.py` — `walk_claimed`, and
  `processed_this_run` → `this_run.retired` inside `retention_engaged`.
- `evals/at_scale/profile_forward_reconcile_attribution.py`.
- The `_ingest_progress` literal dicts in `run_ingestion_benchmark.py`,
  `probe_resume_census.py`, `probe_dep_preload_exposure.py` and the test
  suite drop the removed keys.

`walk_claimed`'s VALUE is unchanged, so `commit_census`'s gates, its
`walk_vs_graph`-before-`repo_vs_walk` ordering and the CLAUDE.md passages
explaining why `walk_vs_graph` is nonzero on any resume stay correct in
substance; only the name of the thing they cite changes.

## `--progress-interval`

`run_ingestion_benchmark.py --progress-interval SECONDS`, default off. It
reuses the existing `_poll_during_ingestion` loop and its existing status
call — no additional calls — and prints at most one line per interval to
**stdout**:

```
[progress] converging this_run 412/9000 · fwd 206 @11.2/min · rev 206 @11.0/min · sweep waiting · visibility 8203/53289 · lineage 7800/53289 · idle 3s
```

stdout, never stderr: stderr is teed and scanned by `stderr_capture`, and no
workflow parses the benchmark's stdout. The nightly does not pass the flag.
The formatting helper is importable so a probe can adopt it later; no probe
is changed here.

## Docs

- SKILL.md `minigraf_ingest_status`: new example, the stream-state table,
  the "done = both flags" rule, `this_run.skipped` as the replay signal
  (replacing the `positions_skipped_this_run` paragraph).
- `_TOOLS`' `minigraf_ingest_status` description, and `tools/ingest_status.json`
  regenerated (`tests/test_tool_schemas.py`).
- README's one-line tool summary.
- CLAUDE.md: every passage naming `_ingest_progress["processed"]`,
  `processed_this_run` or `positions_skipped[_this_run]` — the #326 skip
  section, the #325 resume-census section, the `commit_census` section — is
  rewritten against the CODE, not against neighbouring prose. That mechanism
  was explained wrongly twice in #325's review before the measured passage
  was found; cite it, do not re-derive it.
- `commit_census.py` / `probe_resume_census.py` / `stderr_capture.py`
  docstrings that describe `processed`.

## Tests

Every guard below must be shown failing against master's behaviour before
it is believed.

**`tests/test_ingest_progress.py` (pure model, fake clocks):**
`retired <= to_retire` and `retired == written + skipped + failed` under
arbitrary event sequences; `visibility.verified == total − failed` at a
completed end; `visibility.complete` false whenever `failed > 0`;
`not-needed` states when the gap is empty at load; null lineage for an
unresolvable watermark; lineage `== total` after `folded()`; the lineage
union does not double-count a swept region `lineage_pos` already covers
(the post-fold +1-commit shape); rate and
`seconds_since_progress` arithmetic; `snapshot()` never raises on a model
that has only been constructed.

**End-to-end (real backend, real git repos — the probe's scenarios):**

- Fresh / +1 / no-op rerun: `last_commit == HEAD` each time (master: meeting
  point, then the forward watermark).
- Stage B advances: samples inside `_correction_sweep_apply` show
  `sweep.swept` climbing to `to_sweep` with `phase == "sweeping"` (master: a
  frozen 20/20 and no sweep counter).
- C1, one injected reverse write failure: `visibility.complete is False`,
  `verified == total − 1`, `sweep.state == "blocked"` with
  `blocked_reason == "gap-open"`, `lineage.complete is False` (master:
  `complete` at 20/20 with 19 commits in the graph).
- C2, the re-walk: `this_run.retired <= this_run.to_retire` at every sample,
  and the test also computes the old seeded formula on the same samples and
  asserts it exceeds `total` — so the scenario provably reaches the defect
  and the guard cannot pass vacuously. Ends with both flags complete.
- Census parity: `prior_ingested + this_run.retired` equals the old
  `processed` on C2 and on a clean resume.
- `:total-ingested` equals `_count_commit_entities` after C2 (master: 28).

**Sweep plan:** one test per `_correction_sweep_next` reason, asserting the
reason string; the existing `_correction_sweep_select_position` tests stay
as the wrapper's guard.

**`--progress-interval`:** the line reaches stdout and not stderr, and the
number of status calls is unchanged with the flag on.

Full suite under `.venv/bin/python`.

## Residuals (stated, not fixed)

- **Visibility is only as precise as the frontier.** A failed FORWARD write
  persists nothing, and the next successful forward claim moves
  frontier-low's `:hi-hash` past it (`_frontier_persist_claim(from_low=True)`)
  — #326 left the forward stream's failure semantics explicitly out of
  scope. The next run's `verified` therefore counts that commit though the
  graph lacks it. The run that failed still reports `visibility.complete ==
  False`, and `stderr_capture`'s `skipped_commits` still fails the at-scale
  gate. Worth its own issue.
- **`verified` can dip between runs.** After C1, C2 starts at
  `total − to_retire` (the positions below the floor are back in the gap)
  while the graph holds more commits than that. This is the frontier's
  truth — those positions will be re-walked because nothing can prove them —
  and `this_run` is the monotonic number to watch within a run.
- **`streams.forward` reads `running` while starved.** Once the bulk gap
  closes, `claim_low()` refuses every further hole by design (#325) and the
  forward stream is finished for the run, but its state only turns `done`
  when Stage A ends. Harmless for a display; stated so nobody reads it as a
  stall.
- **`rate_per_min` is a stage average.** It lags a sudden slowdown;
  `seconds_since_progress` is the prompt signal.
