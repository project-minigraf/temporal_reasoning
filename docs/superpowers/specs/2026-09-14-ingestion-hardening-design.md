# Ingestion hardening (#222 phase 5)

Status: design approved 2026-09-14. Phase 5 of #222's five, and the last one.

**Closing #222 is a deliberate act, never a side effect.** Every PR in this
phase says "Part of #222" and carries no closing keyword, including the last
one; #222 is closed by hand once the final item lands and is verified. Commit
messages and the PR body are SEPARATE channels and no single check sees both —
`closingIssuesReferences` covers only the body and title, a whole-message grep
covers only commits — so both are scanned, and re-scanned after every new
commit. This project has had an issue auto-closed twice this way, once by a
NEGATED keyword in a PR body.

Scope is the four items agreed before design: the frontier-low retention
check, divergent-ref orphan DETECTION (no repair), Stage B's lease hold, and
a hygiene batch. Explicitly out: per-position completion markers (#326 spec's
approach B), any repair or migration of an affected graph, and phase 3's
tip-liveness stream, which was already declined as a legitimate close path
for #222.

## What the inherited list got wrong

#222's phase-5 inherit list (carried in the issue comments and in the project
memory) is partly stale. Verified against the code at master `49acbb1` before
designing:

| Inherited item | State |
|---|---|
| Position-indexed preload replacing the valid-time bound | **DONE** — filed as #238, closed |
| `_forward_apply` double-checkpoints per swept commit | **DONE** — gated `if not lifecycle_only` (`mcp_server.py:12140`) |
| `test_sweep_does_not_start_before_stage_a_drains` docstring claims an impossible race | **DONE** — rewritten |
| Stage B holds the lock for the whole sweep | **LIVE** — one lease spans the loop (`mcp_server.py` ~`:13970`–`:14095`) |
| `_forward_apply` is ~490 lines | **LIVE**, and now ~600 (`:11555`–`:12160`) |
| Test-oracle gaps, deferred minors, stale comments | **LIVE** |

And the issue's four named hardening edge cases are mostly already handled by
construction, which is worth recording so nobody re-opens them:

- **DAG diamonds** — the linearization is a positional list of unique hashes,
  so dedupe is by construction, not by care.
- **Octopus merges** — `--cc` reports a path only when it differs from EVERY
  parent, and the `D`-supplement checks each candidate against the merge-base
  with every other parent (`mcp_server.py:5216`).
- **Multiple roots** — `git log --topo-order --reverse <branch>` emits one
  total order over all reachable roots; `linearization[0]` is used only as the
  seed migration's lo bound, which is positionally correct.
- **Non-monotonic committer dates** — `--topo-order` throughout, and #238
  replaced the date-based preload bound with a positional one.

What is NOT handled is the fifth case, **"merge grafts old history"**, and the
unmade half of **force-push / rebase**. Those are items A and B below.

## A. frontier-low is retained on bare hash bounds

### Problem

`_frontier_load` reconstructs the authoritative interval like this
(`mcp_server.py:6438`):

```python
low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
if low_bounds is not None and low_bounds[0] in hash_to_pos and low_bounds[1] in hash_to_pos:
    intervals.append(frontier_registry.Interval(...))
```

Both bounds resolving is the ONLY test. There is no `lo <= hi` guard and no
`:pos-count` check — while `_load_one_interval`, which handles every
provisional interval, demands all three, and `_frontier_check_load_invariants`
inspects provisional intervals only. The authoritative interval is checked by
nothing.

The denominator it fails to read is already written and already maintained:
`_frontier_seed_from_watermark` seeds `:pos-count` (`:6414`) and
`_frontier_persist_claim` calls `_frontier_pos_count_delta` unconditionally
(`:7561`), on the `from_low` path as much as the reverse one. So this is a
read-side fix; nothing new has to start being written.

### Why it is silent permanent loss

`FrontierAllocator._unclaimed()` defines the gap as the COMPLEMENT of the
interval set. A commit grafted into history below the forward frontier — a
merge that brings in ancestors predating the watermark, which is #222's own
"merge grafts old history" edge case — lands at a position INSIDE the retained
`[lo_pos, hi_pos]`. It is therefore in no hole, is handed to no stream, and is
never walked. Both stored bounds still resolve (they are hashes, and the graft
did not rewrite them), so today's test retains the interval and widens its
span silently.

Every detector reads clean, for the reasons this arc has now recorded five
times: `fact_audit`'s `divergence` is 0 because the commit reaches neither
witness; both `:introduced-by` checks only examine entities that EXIST;
`stderr_capture` has nothing to read; and the at-scale `commit_census` runs
once on a fresh graph, which has no interval to retain.

This is the first of the two follow-ups #326 deliberately left open without
filing: "`_frontier_load` reconstructs frontier-low from bare hash bounds with
the SAME defect as Critical 2."

**Reasoned from the code, NOT yet measured.** Constructing the loss against a
real repo is task 1, before any fix is written — the standing rule here is
that a zero-tolerance gate gets its clean baseline measured and its positive
control checked first, and the same discipline applies to believing a defect
exists at all.

### Design

Retain the authoritative interval iff all three hold:

1. both bounds resolve in this linearization,
2. `lo_pos <= hi_pos`,
3. the stored `:pos-count` equals `hi_pos - lo_pos + 1`.

Otherwise call `_frontier_discard_interval` — which already derives the
`:authoritative` tag from the ident, so it needs no new argument — and append
no interval. The forward stream then re-walks from C0. An interval carrying NO
`:pos-count` is not retained either: "no denominator" and "a denominator that
still checks out" must not be the same branch when the failure mode is silent
permanent loss.

**A separate code path from `_load_one_interval`, deliberately.** That function
also ARCHIVES a `:type/completed-region` on its unresolvable-bounds path, and
regions are consumed by `_skip_claim`, which honours provisional regions only.
Routing the authoritative interval through it would either archive a region
nothing can consume, or force `_skip_claim` to be widened to authoritative
regions — which is exactly the interaction #326's follow-up warns about
("harmless today only because `_skip_claim` honours provisional regions
only"). The narrow check has a smaller blast radius and mirrors the fail-safe
direction `_load_one_interval` already chose.

### Cost

A discard means the forward stream re-walks from C0. Expensive, never lossy —
the same accepted price #325 and #326 both established. The realistic exposure
on an existing graph is small: `_frontier_pos_count_delta` runs on every
forward claim, so any graph that has taken one since #326 carries a live
count. Only the seed-migration path can omit it
(`if seeded_count is not None`), and that omission is itself the signal that
the interval is untrustworthy.

### Residual, stated not fixed

`:pos-count` remains a **CHECKSUM, not a proof of set identity**. Equal count
does not imply the same member set; the undemonstrated residual recorded for
#326 and #325 applies verbatim to the authoritative interval too. Do not
upgrade that language to "sound". Closing it properly is per-position markers
(#326 spec's approach B), which cost one fact per commit on the write path
this arc exists to make cheaper, and are out of scope here.

### Graph format

No `GRAPH_FORMAT_VERSION` bump and no migration. This reads a fact that is
already written; it changes a retain/discard boundary condition only.

## B. Divergent-ref orphan detection, without repair

### Problem

A force-push or rebase rewrites commits out of history. Today the frontier
side of that is handled — bounds that no longer resolve are discarded and
archived, and `TestDivergentRefEndToEnd` pins a full, correct re-walk. What is
NOT handled is the **facts left behind**: the rewritten commits keep their
`:type/commit` entities, and every reference to them stays live —
`:introduced-by` and `:modified-in` (on the five code types and on
external-dependency), `:parent`, `:tagged-commit`, and the lineage marker's
`:commit`. Nothing detects them, and the at-scale `repo_vs_graph` delta stays
nonzero on such a graph forever.

#222 asks for an explicit policy here ("retract orphaned lineage vs. keep as
bi-temporal history; must not silently continue against a divergent history"),
and it has never been made.

### The constraint that shapes the whole design

**The graph does not record which ref it was ingested against.** The
`:ingestion/*` idents are format-version, watermark, last-run-at,
frontier-low, frontier-high, the minted interval and completed-region idents,
correction-sweep-through and lineage-confirmed-through. There is no branch
fact. Meanwhile `handle_minigraf_ingest_git(branch=...)` accepts any branch,
and `MINIGRAF_GIT_BRANCH` overrides the default.

So "a `:type/commit` entity absent from the current linearization" means
EITHER "rewritten away by a force-push" OR "belongs to another branch that was
also ingested into this graph" — and the two are indistinguishable. A detector
that cannot separate them would condemn a legitimately ingested branch's
entire history. That is the `:type/external-dependency` trap of #316 exactly:
a check whose denominator was never established reports a number that means
nothing.

### Decision

**Detect and record; never repair.** Retraction was considered and declined.
It is repair, and the standing decision throughout this arc is that an
affected graph is REBUILT into a fresh graph path — never migrated, never
repaired, never re-ingested in place. #329 also established that a detector is
not repair and does not violate a scope decision that excluded repair.

Refusing to run was considered and declined too: it is a permanent denial of
service with no repair path, which #329 rejected for the base-order invariant
on exactly these grounds.

### Design

**1. Record the branch.** A dedicated `:ingestion/branch` entity, written on
EVERY run — the branch can change between runs, so this is not a stamp-if-new
like the format version.

**Write it after `_graph_format_version_stamp_if_new`, inside the same lease,
before `_frontier_load`** — but the ordering is defensive, NOT load-bearing,
and the distinction is worth stating precisely because the obvious reasoning
gets it wrong. `_graph_format_version_stamp_if_new` no-ops when
`_graph_has_ingestion_state(db)` is true, which reads like a trap for any new
`:ingestion/*` fact written ahead of it. It is not: that function's
disjunction is exactly three specific reads — `_watermark_query`, and
`_frontier_read_bounds` on frontier-low and frontier-high — so an
`:ingestion/branch` fact is invisible to it and the stamp fires correctly in
either order. Keeping the stamp unambiguously first costs nothing, so do it
anyway.

**The real hazard is downstream, and must be recorded: `:ingestion/branch`
must NEVER be added to `_graph_has_ingestion_state`'s disjunction.** It reads
like ingestion state and someone will be tempted. But this fact is written
before any walk, so a run that recorded a branch and then died before writing
a watermark or a frontier bound would afterwards read as "already ingested" —
suppressing its own stamp, and then being refused by
`_graph_format_version_verify` on the run after as a state-present,
stamp-absent pre-#263 graph. That condemns a graph holding no ingested data at
all. The three reads in that disjunction are the three things that mean a walk
actually happened; keep it that way.

Value-diffed against
its current live value before writing, the way `_last_run_write` and
`_ingest_tags` both do: minigraf is NOT idempotent at the graph level for
re-transacting the same `(entity, attribute, value)` at a fresh valid-from
(#156), so an unconditional re-transact accumulates duplicate live facts.

Not folded into `:ingestion/last-run-at`, which is written only under
`if completed_all:` — an interrupted run would record no branch, and the
detector needs the branch far more on an interrupted graph than on a clean
one.

**2. `:branch` MUST be added to `MINIGRAF_SCHEMA["ingestion"]["optional"]`.**
`handle_minigraf_audit` iterates every registered type and retracts any
attribute outside the allowed set, querying the live graph directly. `ingestion`
is a registered type, so omitting this line makes an audit run silently delete
the fact the detector depends on. The `:version` entry in that same dict
carries a comment saying precisely this; it is the second instance of the same
trap.

**3. Detect BESIDE the audit, never inside it.** An earlier draft of this
spec said the orphan check rides `fact_audit`'s whole-graph scan the way
`introduced_by_audit` does. **That is wrong**, and the harness already states
why at `commit_census`'s call site: this is a check that "holds a reference
the graph did not produce -- the repo itself -- which is also why it cannot
ride fact_audit's scan the way the two `:introduced-by` checks do. fact_audit
deliberately takes no repo handle, and giving it one for this would cost that
signature its honesty."

Orphan detection needs `set(linearization)`, which is repo-derived, so it
belongs exactly where `commit_census` belongs — in
`evals/at_scale/commit_census.py`, which already takes `repo_path`, `ref` and
a leased `db`, already runs `git rev-list` against the ref, and already counts
commit entities. It is a second question over references that module already
holds, so it costs one extra graph query and no extra git subprocess.

It reports under its own key, `orphaned_commits`, carrying:

- `entities` — the count of `:type/commit` entities absent from the ref's
  linearization,
- `sample` — a bounded sample, named by ident,
- `commit_entities_scanned` — the denominator, so a check that matched no
  commit entities cannot read clean by having scanned nothing (#316's idiom),
- `recorded_branch` and `audited_ref` — what makes the count interpretable,
- `proved_nothing` — true when the branch is absent or does not match.

`None` on a failed scan, never a zero dict, matching
`introduced_by_duplicates` and `entities_without_introduced_by`.

**4. Gate narrowly.** `_exit_code` gains clause 9, failing on a nonzero
`entities` **only** when `recorded_branch` matches `audited_ref`. A mismatch
or an absent branch renders and does not fail — it is a multi-branch or
pre-fix graph, about which the check has proved nothing. The clean baseline
must be MEASURED on the at-scale graph before the clause is wired, and the
positive control (that the check matches commit entities at all) is what
`commit_entities_scanned` makes every subsequent run re-prove.

An absent `orphaned_commits` key stays clean, matching every other clause: a
metrics file from a harness that predates this check cannot be retro-failed.

**5. Surface it in status, computed ONCE per run.** Not a poll-time graph
query: phase 4 settled that status is never derived from graph queries at poll
time, because that contends with ingestion on `_db_native_lock`, adds latency
the benchmark measures, and is staler than the in-memory state anyway. The
count is computed once in `_run_ingestion`, where the linearization and a
lease are both already in hand, and stored as a plain `_ingest_progress` key.

It does NOT go on `RunProgress`. That class is deliberately PURE — no DB, no
git, injected clocks — and an orphan count is a graph-and-repo fact. Putting
it there would cost the class the property phase 4 built it for.

### Graph format

No bump. This adds a fact going forward. An existing graph has no
`:ingestion/branch` until its next run, and until then the detector reads
`proved_nothing` — which is the honest answer, not a degraded one.

## C. Stage B's lease hold

### Problem

Stage B takes ONE lease that spans the entire sweep loop (`mcp_server.py`,
~`:13970` through the lineage fold at ~`:14095`). Stage A releases per commit.

A lease is cheap in-process and exclusive out-of-process, and both auto-memory
hooks (`hooks/claude-code.json`) are `command` hooks in SEPARATE processes.
`finalize_hook.py` takes a lease to write each turn's facts, the retry budget
is `_LOCK_RETRY_MAX` × `_LOCK_RETRY_BASE` doubling = **0.75 s total**, and both
hooks swallow failures with `except Exception: pass`. So holding the lease for
the sweep's whole duration does not block queries — it **silently discards
every auto-memory write for that duration**. On a large repo the sweep is a
large fraction of the whole ingest.

### Why not simply release per commit

`_DbLeaseManager.release()` at refcount 1 → 0 drops the handle, and minigraf's
`Drop for Inner` runs a full O(graph size) checkpoint — #280, measured at
**47.3% of Stage A's write time** and growing 3.47× within a 220-commit run.
Releasing per swept commit imports that cost into Stage B wholesale.

`_DbLeaseManager` exposes no waiter or contention signal, so "release only when
something is waiting" is not available without building one.

### Design

A bounded yield window: release and reacquire every **N** swept commits or
every **T** seconds, whichever comes first.

Two constraints the implementation must respect:

1. **`sweep_fragmented` stays computed once** and is carried across windows.
   #325 review Finding 3 moved it out of the loop deliberately — Stage B only
   starts once the whole gap is claimed, and nothing in the loop body writes
   an interval fact, so fragmentation cannot change mid-sweep. Recomputing it
   per window would restore two datalog queries per window for no information.
2. **A window boundary lands only between commits.**
   `_correction_sweep_apply`, `_forward_apply(..., lifecycle_only=True)` and
   `_correction_sweep_through_update` are ONE unit — the watermark is
   deliberately deferred until both halves land
   (`update_watermark=False`), so a boundary inside that sequence creates
   exactly the half-processed state that deferral exists to prevent.

T is sized against the hooks' 0.75 s retry budget: the window must leave the
lock free long enough for a hook that is already retrying to win it. N is
MEASURED, not guessed, using the existing `MINIGRAF_INGEST_TRACE_PATH` and
`evals/at_scale/probe_per_commit_cost.py` — the numbers to record are the
drop-checkpoint count and Stage B wall clock, before and after.

### Accepted cost

Stage B gets strictly slower, by roughly (sweep length / N) drop checkpoints.
That is the trade for not silently discarding the user's auto-memory writes
for the sweep's whole duration. When #280 lands — it is blocked on upstream
minigraf#322 — the drop checkpoint is suppressed and N can be lowered to 1,
at which point this becomes the simple per-commit release. The design should
make N easy to change for that reason.

## D. Hygiene

None of these change behaviour. They are grouped so they can be split into
their own PR if the diff grows.

**Dead walk wrappers.** `_reverse_bulk_fill_walk`,
`_reverse_fill_claim_and_process`, `_correction_sweep_walk` and
`_correction_sweep_claim_and_process` are reachable only from tests. They
always persist claims and have NO floor or ceiling concept, so — per #326's
second unfiled follow-up — wiring either into a real run reintroduces
Critical 3 (a failed write swallowed by range semantics) wholesale. Delete
them if their tests can be retargeted onto the real path; otherwise keep them
behind a loud docstring plus a test asserting `_run_ingestion` never calls
them.

**`_forward_apply` decomposition.** ~600 lines with `lifecycle_only` threading
three distinct behaviours through one body. This is the riskiest item in the
batch — two load-bearing invariants hang on this function, and #253 records a
previous lifecycle-adjacent change that segfaulted the suite. Extract ONLY
along the `lifecycle_only` seams that already exist, with the parity oracle as
the safety net. If the diff grows beyond a reviewable size, file it as its own
issue rather than forcing it into this phase.

**Test oracles.** Add a direct unit test for
`_forward_structural_triples_by_ident` (it has none). Both `_SNAPSHOT_QUERIES`
oracles bind `[?e :ident ?i]`, so an entity whose `:ident` is closed
contributes no rows to either snapshot — add a closed-entity arm so the oracle
can see a resurrection or a purge.

**`_lineage_marker_ident` injectivity.** `entity_ident.lstrip(':').replace('/', '-')`
maps both `:module/a-b` and `:module/a/b` to `:lineage/module-a-b`.
CONSTRUCT the collision before deciding anything — "it is a function of the
ident so it cannot differ" is a claim about the pre-slug inputs, and this
project has already been wrong about exactly that once. Then decide whether
`_canonical_ident` makes the colliding input reachable at all.

**Smaller items.** `_entity_introduced_by_set_provisional_batch` returns a
`Set[str]` that both production call sites discard — drop it or document the
test-only contract. `_correction_sweep_apply`'s `candidate_idents` is
un-deduplicated (idempotent, degenerate input only). `_parse_stream_ratio`
logs under a `[_run_ingestion]` tag it does not belong to.

## Testing

Real-backend only, per `docs/testing-conventions.md`.

**Every regression test is ablation-proven** — the experiment is run, and the
counterfactual must match the REAL old code, not a plausible reconstruction of
it. This project has had four tests on one branch claim guarantees they did
not provide.

Specifically:

- **A** — build a repo, ingest, graft an old ancestor via a merge, re-ingest;
  assert the grafted commit's entity exists. Ablate by reverting the count
  check and showing the commit is lost. The graft must go through the REAL
  write path, not a seeded interval, since the whole question is whether the
  real path produces the retained-and-widened state.
- **B** — a force-push scenario producing real orphans, asserting the count
  and the sample; plus the multi-branch scenario asserting `proved_nothing`
  rather than a condemnation. The `MINIGRAF_SCHEMA` entry needs its own test:
  run `handle_minigraf_audit` and assert `:ingestion/branch` survives it.
- **C** — assert a second process can acquire the lock during a sweep (via the
  existing `_another_process_can_open` helper, never a `.lock` file
  assertion, which is a tautology under minigraf 2.0.0), and that the two
  halves of a swept commit are never split across a window boundary.
- **D** — the collision construction for `_lineage_marker_ident` is itself the
  deliverable; if no collision is reachable, that finding is recorded rather
  than a guard being added for a hazard that does not exist.

## Sequencing

A → B → C → D. A is the only silent data loss and goes first; its measurement
task precedes its fix. B is independent of A. C is independent of both and
carries its own before/after measurement. D is independent but touches the
same file, so it lands last and may become its own PR.

Separate commits throughout, so the ablation evidence survives — this arc has
twice chosen `--merge` over squash for exactly that reason.

## To be measured during implementation, not assumed

1. That the graft loss in A is real, constructed against a live repo, before
   the fix is written.
2. Whether real existing graphs carry `:pos-count` on frontier-low, and so how
   often the fix forces a full forward re-walk.
3. The clean `orphaned_commits` baseline on the at-scale graph, before clause 9
   is gated.
4. N and T for Stage B's yield window, and Stage B's before/after wall clock
   and drop-checkpoint count.
5. Whether a `_lineage_marker_ident` collision is reachable from
   `_canonical_ident`'s output at all.
