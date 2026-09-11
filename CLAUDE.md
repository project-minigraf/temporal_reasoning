# Temporal Reasoning — AI Coding Agent Memory

Temporal Reasoning provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill (--harness is required: claude-code, opencode, or codex)
python install.py --harness claude-code
```

Everything else goes through the MCP tools — there is no wrapper module:

```
minigraf_transact(facts='[[:decision/cache :description "use Redis"]]',
                  reason="Caching strategy")
minigraf_query(datalog='[:find ?d :where [?e :description ?d]]')
```

`from minigraf import query, transact` has never worked. The installed
`minigraf` package exports `MiniGrafDb`, `MiniGrafError` and `minigraf_ffi`;
there is no `minigraf.py` in this repo and `git log --all --diff-filter=A`
shows there never was. To read a graph outside the server, open a
`MiniGrafDb` directly — subject to the single-handle invariant below.

## Key Files

- `mcp_server.py` - Persistent MCP server (the only runtime interface to the graph)
- `fact_index.py` - SQLite FTS5 fact index; retrieval, and the graph's only independent witness (#302)
- `frontier_registry.py` - Per-position claim registry for the two ingestion streams
- `SKILL.md` - Skill definition with all query syntax. Every example is an MCP
  tool call with named arguments, checked against `mcp_server._TOOLS` by
  `tests/test_skill_doc.py` (#322) — nothing executes those examples, so the
  dead `from minigraf import query, transact` form survived in them for months
- `skill.json` + `tools/*.json` - Portable manifest, generated from `mcp_server._TOOLS` and guarded by `tests/test_tool_schemas.py`
- `install.py` - Setup script (runs weekly updates)
- `docs/testing-conventions.md` - Real-backend-only test conventions for `tests/test_mcp_server.py`
- `hooks/claude-code.json` - Claude Code MCP + auto-memory hook config
- `evals/at_scale/` - at-scale ingestion + query-correctness benchmark tier (real repo history, observational, see its own `benchmark.md`). It also holds one-off read-only probes (e.g. `probe_dep_preload_exposure.py`, #245) and their recorded measurements under `results/`, which are analysis artifacts rather than part of the recurring benchmark run.

## Python Version Support

**Supported: 3.10 through 3.14, and `requires-python` is deliberately left
unbounded above.** `.github/workflows/pytest.yml` runs the full suite on every
one of those five interpreters and `pyproject.toml`'s classifiers list exactly
that set — so the three declarations agree by construction, and a new
interpreter is added by editing all three together.

An unbounded ceiling is the honest choice here rather than the lazy one. A cap
does not make an untested interpreter work; it only refuses to install on it.
Since nothing in this repo is version-fragile in a way anyone has been able to
demonstrate, refusing to install would cost real users a working install to buy
a guarantee CI already provides for every version that exists. **When a new
interpreter ships, add a matrix row and a classifier rather than a cap** — and
if it genuinely fails, cap it then, with the failure recorded.

**Measured before the matrix rows were added, not predicted.** Full suite,
this repo, #331: **3.14.7 — 1993 passed, 1 xfailed**; **3.13.14 — 1993 passed,
1 xfailed**. The `<3.13` cap the issue offered as a fallback was therefore never
needed. 3.10/3.11/3.12 were already green in CI. Dependency resolution was
checked separately on 3.13 before trusting the run: minigraf 2.0.0 ships a
`py3-none-manylinux` wheel and every `tree-sitter-*` grammar resolves, so no
dependency pins the ceiling either.

**One flake surfaced during that measurement and is NOT a version problem —
worth knowing before it is blamed on 3.13.**
`TestMcpToolWiring::test_call_tool_lock_retry_does_not_block_event_loop` failed
once in a full 3.13 run and passed in isolation, on the rerun, and on master
under the same interpreter, so it is pre-existing and unrelated to any change
here. Cause: the test does `monkeypatch.setattr(mcp_server.time, "sleep", ...)`,
and `mcp_server.time` IS the `time` module, so the patch replaces `time.sleep`
**process-wide**. `_open_index_writer_safe` (`mcp_server.py`) and
`fact_index.py`'s SQLite retry both call `time.sleep` from WORKER THREADS by
design — both are documented as safe precisely because they never run on the
event loop. A worker thread from an earlier test hitting lock contention inside
this test's window therefore trips an assertion written only for the event-loop
path. The guard is too broad, not the code under it. Tracked as #334; do not
"fix" it by widening the version policy.

**The six "Python 3.14 failures" that prompted this policy were not Python 3.14
failures, and the misdiagnosis is the part worth remembering (#331).**
`.claude/settings.local.json` exports `MINIGRAF_NO_AUTO_INGEST=1` into every
Claude Code session in this repo, and every `pytest` run started inside one
inherits it. `main()` gates its whole auto-ingest and backfill startup block on
that variable (`mcp_server.py`, `if not os.environ.get("MINIGRAF_NO_AUTO_INGEST")`),
so with it set `_ingest_progress["status"]` stays `idle` and `_ingest_task` /
`_backfill_task` stay `None` — which is precisely what
`TestMainAutoIngestLockCheck` and `TestMainStartupBackfill` assert against. CI
carries no such variable and was green throughout.

The failure was environmental, as the issue said, but the environment was the
SESSION, not the interpreter. Every reproduction ran from inside a Claude Code
session carrying the same `settings.local.json`, which is exactly what kept the
variable invisible and left 3.14 holding the blame. Two details are worth
keeping because both misled: only ONE of the six actually raised the
`TimeoutError` the issue reported — the other five failed on ordinary
assertions — so "all six share one mechanism" was true while the named mechanism
was wrong; and the cost landed on PR #328, where nine tasks and ~15 review
rounds each had to be told a six-failure baseline and trusted not to wave a
seventh through.

**`tests/conftest.py` now scrubs every `MINIGRAF_*` variable before each test,
and scrubbing beats pinning.** A test that wants a value sets it with
`monkeypatch.setenv`, which runs in the test body and therefore still wins over
the autouse fixture. The tests that need `MINIGRAF_NO_AUTO_INGEST` PRESENT
always set it explicitly; it was the tests needing it ABSENT that depended on an
unstated precondition, which is the asymmetry the fixture removes.

`MINIGRAF_EXTRACTION_STRATEGY` was ambient in the same session and is the more
expensive instance of the same class: `handle_memory_finalize_turn` defaults to
`heuristic`, so a test exercising it without setting the variable would have
taken the **LLM path and a real network call** under a session exporting
`llm`. The scrub closes that too.

**What the scrub CANNOT reach: module-level reads, which happen at import,
before any fixture runs.** `_OWNER_HINT_TTL`, `_MAX_MATCH_POOL_SIZE` and
`_MAX_FACT_VALUE_LENGTH` (`mcp_server.py`) all bake an ambient value into a
module constant at import time, and no autouse fixture can undo that. A session
exporting one of those still perturbs the suite silently. This is stated rather
than fixed — no test depends on those defaults today, so there is nothing to
guard yet, but a new test that does must set the constant, not the variable.

**The regression test runs pytest in a SUBPROCESS with the variable genuinely
set in `os.environ`**, against a copy of `tests/conftest.py`, because the
in-process form is vacuous: on CI, where no `MINIGRAF_*` variable is ambient, a
test asserting "no such variable is visible" passes whether or not the fixture
exists. Ablation-proven — deleting the fixture reddens it.

**`install.py` mirrors the floor and had drifted below it.**
`check_python_version` accepted 3.9 while `requires-python` said `>=3.10`, so on
3.9 the installer printed a tick for the version check and then handed the run
to a `pip install -e .` that refused the very interpreter it had just approved.
`pyproject.toml` stays canonical; raise the floor there first and mirror it
here. It carries no ceiling for the same reason the spec does not.

`.github/workflows/pylint.yml` no longer carries a version matrix. It installs
**ruff**, a standalone binary that parses with its own front end and never
imports the code under the host interpreter, and its one step ends in
`|| echo "Lint warnings (non-blocking)"`. The old 3.8/3.9/3.10 matrix therefore
ran the identical check three times, could not fail, and said nothing about
Python compatibility — while two of its three rows named interpreters
`requires-python` already excludes.

## Graph Storage

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

Memory retrieval index: `<graph_path>.fts.sqlite3` alongside the graph file.

Override: `MINIGRAF_INDEX_PATH=/custom/path`

Ingestion checkpoint budget: `MINIGRAF_INGEST_CHECKPOINT_DUTY` (default `0.05`).

`db.checkpoint()` is full WAL-to-graph compaction, so it costs O(graph size)
regardless of how much was written since the last one. Ingestion holds it to
this fraction of wall clock instead of running it once per commit. Writes are
durable via `<graph_path>.wal` without it; a larger WAL only slows the next
process that opens the graph. See #241.

Per-commit cost trace: `MINIGRAF_INGEST_TRACE_PATH` (unset by default).

When set, ingestion appends one JSON object per applied commit — timings, work
counters, and per-commit checkpoint deltas — for #260's cost attribution. Unset
means no trace and no file. Commits skipped for extraction failure emit no
record, and neither do commits whose write fails, so a trace is not a commit
census; `stderr_capture.py` counts those.
Read with `evals/at_scale/probe_per_commit_cost.py`.

The fact index is bi-temporal: it includes historical (retracted/superseded) facts
alongside current ones, labeled with their validity window.

**The index is also the graph's only independent witness (#302).** It is written
from the same triples in the same transaction boundary but by a different
storage engine, so `evals/at_scale/fact_audit.py` can ask what the graph has
stopped producing. That gap is real: garbling one fact page cost ~11% of a
measured graph with **zero bytes on stderr and zero `error_signals`**, so
`stderr_capture.py` — the tier's other detector — reported it clean. The
at-scale gate now fails on any divergence, with NO tolerance, because a clean
audit diverges by exactly zero — verified on the 822-commit at-scale graph, at
a cost of 0.8s. Two normalizations buy that exactness and both are
load-bearing: index entities are mapped forward into UUID space the way
minigraf derives them (`uuid5(NAMESPACE_OID, ":the/ident")`), and graph values
are rendered back into the datalog text the index stored (`_index_text`:
minigraf returns `1`, the index holds `'1'`).

**The audit also found that boolean-valued facts were never indexed, and #303
fixed it.** `_FACTS_TRIPLE_PATTERN` accepted a quoted string, keyword, number
or `#uuid`/`#inst` literal as a value but not a bare `true`/`false`, so
`[:function/f :static true]` reached the graph and never the index — 83 facts
on the at-scale graph, all `:static`, invisible to memory retrieval as well as
to the audit. The pattern now has a `true|false` alternative, and the audit's
`unindexed_boolean_facts` key is **deleted, not zeroed**: a key that always
reports 0 reads like a covered case while covering nothing, and while the
exclusion stood a `:static` fact the graph had genuinely LOST was
indistinguishable from one the index could never hold.

Two things the fix depends on. The index stores the **EDN spelling**, lowercase
`true`/`false` — the datalog text it was transacted from, the same rule every
other value type follows — and `fact_audit._index_text` renders minigraf's
Python `True` back into it. That test is on the Python **type**, never the
text, so a fact whose value is genuinely the string `"True"` keeps its
capitalization on both sides and stays auditable. No graph format bump: stored
facts stay readable, the index simply gains rows going forward. Existing graphs
are not repaired — index rows are written at transact time — so a graph that
needs `:static` searchable gets rebuilt, per the standing decision below.

**`nil` is not a storable value, and #306 is the decision that settled it.**
The one remaining bare EDN literal outside the pattern was `nil`, and #303 left
it there deliberately rather than closing it speculatively. It was NOT then
fixed the way `true`/`false` was. Indexing it would store a row whose value is
the text `'nil'` — "no value" occupying a slot in a lexical retrieval index and
answering a search for "nil" — and it would have to teach
`fact_audit._index_text` a `None` case in the same commit, because minigraf
returns `None` and `str(None)` is `'None'`, not `'nil'`. A pattern-only fix
trades one divergence for another.

So `handle_minigraf_transact` REFUSES a nil-valued triple, before the graph is
touched (`_has_nil_valued_triple`, mcp_server.py). `_FACTS_TRIPLE_PATTERN` and
`_index_text` are unchanged. The at-scale audit gate stays honest as a result: a
nil fact found in a graph is now a genuine write-path defect, not the index
being blamed for what it cannot hold. Rejection is all-or-nothing for the
block — a partial write the caller cannot detect from `ok:True` is worse than
a refusal.

Two things the guard depends on. It blanks quoted strings before scanning, and
that is load-bearing, not tidiness: a note written ABOUT this defect carries the
offending triple as prose, so a raw scan would refuse a legitimate write.
`_FACTS_TRIPLE_PATTERN` shares that blind spot but pays only a spurious index
row for it; here the cost is a rejected write. And the trailing `\]` is what
makes the unanchored `nil` safe, exactly as for `true`/`false` —
`[:d/x :note nilpotent]` does not match. Both are ablation-proven, not merely
asserted.

The guard is on the public handler only; the internal `_transact` (ingestion)
is unguarded, and deliberately so — ingestion emits no `nil`, and if it ever
did, a red gate is then the correct signal rather than a misattribution.
Existing graphs are not repaired either way.

**The same scan also answers a graph-only question: #287's two-value
`:introduced-by`.** `fact_audit`'s full `[:find ?e ?a ?v]` scan is already in
memory, so `evals/at_scale/introduced_by_audit.py` reads #235's corruption off
it for one pass over a dict — no second query, no second scan, no second lease.
It is reported under its own key, `introduced_by_duplicates`, and deliberately
NOT folded into `divergence`, because it is not a two-witness finding: both
values are faithfully in the index too, so the two witnesses agree perfectly
about a graph that is wrong. **A run can read `divergence | 0` and still be
condemned**, which is exactly why the report carries a separate row. The check
is also narrowed to `:introduced-by` specifically — `:contains`, `:modified-in`
and `:depends-on` are legitimately multi-valued, so a detector keyed on "this
entity has two values for some attribute" would report every module in every
healthy graph. It reports entities, not surplus facts, and the sample names
each one by its own `:ident` fact, already in the same scan.

**The at-scale gate fails on any nonzero count, with NO tolerance, and that
was measured before it was wired.** The 831-commit at-scale graph carries 3150
`:introduced-by` facts across 3150 entities, **every one of them holding
exactly one** — so a clean graph is zero, not "small", and the count needed no
threshold. The positive control matters as much as the zero: a check that
scanned no `:introduced-by` facts at all would also report 0, so the
distribution was read before the gate was believed. Cost is unchanged at 0.84s
for the whole audit, because this rides the scan rather than paying for one.
An absent `introduced_by_duplicates` key (a metrics file from a harness that
had the fact audit but not this check) stays clean — it cannot be
retro-audited, so it is not retro-failed.

**The OPPOSITE defect needed its own detector, and #316 is it.** An entity
holding ZERO `:introduced-by` is what an interrupted run leaves behind (#313),
and every gate the harness had read it clean — `introduced_by_duplicates` skips
anything with fewer than two values; `divergence` compares two witnesses that
are missing exactly the same fact, because neither was ever written;
`stderr_capture` had nothing to read; and `probe_provisional_residue`'s
`M <= N` reads clean MORE comfortably when the defect is present, because a
torn entity raises N without ever raising M. So
`entities_without_introduced_by` (introduced_by_audit.py) rides the same scan
and reports under its own key, gated as clause 7 with no tolerance. Measured
before wiring: **0 orphans of 3277 live code entities** on the 845-commit
at-scale graph, whole audit 0.93s against 0.84s before.

Two things it depends on. `CODE_ENTITY_TYPES` is the five types
`_build_code_triples` writes an `:introduced-by` for —
module/function/class/variable/field — and **`:type/external-dependency` is
excluded deliberately**: unresolved-import stubs are opened with exactly
`:entity-type`/`:ident`/`:description` and the lineage machinery can never give
them one later (`_reverse_apply`'s `candidate_idents` comes from
`_build_code_triples`, which never yields a stub), so all **72 of 72** on the
at-scale graph legitimately hold none — including the type would have made the
gate permanently red on its first run. And liveness is `:ident` PLUS the
`:entity-type` FACT, never the ident prefix, because those same stubs carry a
`:module/...` ident while being `:type/external-dependency`.

The result carries its own denominator, `code_entities_scanned`, and that is
not decoration: a check that matched no code entities would also report 0, so
shipping the denominator makes every future run re-prove the positive control
instead of it being a fact about the day the gate was wired. The report row
renders `0 of 3277`, never a bare `0`; a run whose denominator is itself 0
renders as "proved nothing about it" and does NOT fail the gate — a graph
holding no code is not a defect. An absent key stays clean, same precedent.

The one false positive it cannot rule out: `module`/`function`/`class`/
`variable`/`field` are registered in `MINIGRAF_SCHEMA` with `:introduced-by`
optional, so `handle_minigraf_transact` accepts `[:module/foo :description
"x"]` and produces a legitimately lineage-free `:type/module`. The at-scale
gate runs on a fresh ingestion-only graph so it cannot fire there; on a mixed
graph the row is informational. The graph carries no discriminator between an
ingested and a memory-written code entity.

**What no fact-level check can see is a whole COMMIT going missing, and #317
is the census that does.** Graph and index are consistent about the absence,
so `divergence` reads 0 again; the two `:introduced-by` checks are
well-formedness checks on entities that EXIST, and a commit that produced no
entities produces nothing for either; `stderr_capture` has nothing to read;
and `_exit_code`'s `graph_facts == 0` clause only fires when the graph reads
back COMPLETELY empty — one commit in 847 is not zero facts. So
`evals/at_scale/commit_census.py` compares THREE numbers, not one delta, and
is wired beside the audit rather than inside it (it needs a repo handle
`fact_audit` deliberately does not take): `git rev-list --count <branch>`,
`_ingest_progress["processed"]`, and `_count_commit_entities`. **`walk_vs_graph`
catches a commit walked and then lost; `repo_vs_walk` catches one NEVER
WALKED** — the case no in-process counter can see, because the counter and the
walk share the bug. Gated as clause 8.

The clean difference **is** zero, which was the open question: **847 = 847 =
847** on the 847-commit at-scale graph (`results/317-commit-census.json`), so
the gate is zero-tolerance. Predicted from the code, then confirmed — shipping
it as zero without measuring would have repeated the `:type/external-dependency`
trap. The five hazards the issue required measuring all resolved clean: merges
count on both sides (`build_linearization` and `rev-list` are the same set, and
both apply functions write the `:type/commit` triple FIRST, before any file is
looked at, so path-ignore changes nothing either); an extraction-skipped commit
raises the walk count without raising the graph's, and is already failed by the
`skipped_commits` clause. **`repo_commits` ships as the census's own
denominator** — three counts that are all zero also agree, so an empty repo
reports `proved_nothing` and is NOT failed. **An incomplete run is not failed
for walking fewer**: `repo_vs_walk` is gated only when `final_status` is
`complete`, while `walk_vs_graph` is gated always.

Two things it depends on. The ref is **the resolved branch, never `HEAD`** —
`_run_ingestion`'s own `repo_total` was hardcoded to `HEAD` while ingestion
takes a `branch` argument, live in the very run that measured this (the harness
resolves `master` while the checkout sits on a feature branch); fixed at source.
The at-scale nightly carried two more instances of the same shape until #330 —
`--branch HEAD` on the ingestion step (truthy, so it DEFEATS
`branch or _default_git_branch(...)` rather than adding to it) and
`git rev-parse --abbrev-ref HEAD` on the resume-census step. Both now resolve
through `_default_git_branch`; `run_ingestion_benchmark.main()` refuses a literal
`HEAD`, and `tests/test_at_scale_nightly_workflow.py` keeps the workflow clean.
The refusal is on the CLI argument only: `_default_git_branch` legitimately
RETURNS `"HEAD"` as its last-resort fallback, and that path stays reachable.
And **`_count_commit_entities` now uses `count-distinct`, not `count`**:
`(count ?e)` counts matching ROWS, so one duplicated commit entity would CANCEL
one genuinely lost commit and the census would read clean on a graph that lost
history. That duplicate is NOT reachable from ingestion (the commit triples are
transacted at `commit_ts_iso`, so a #313 re-walk rewrites the identical triple
at the identical valid-from and it collapses); it is reachable from the public
handler, since `commit` is a registered `MINIGRAF_SCHEMA` type and
`handle_minigraf_transact` writes `:entity-type` at wall-clock valid-from.
`_STATUS_QUERY` keeps `count` deliberately — it is a latency instrument whose
query is frozen for cross-run comparability, and its number is never read as a
count. An absent `commit_census` key stays clean, same precedent as the rest.

**Re-running ingestion repairs none of it, and that is mechanical, not policy.**
A finished run parks `:ingestion/correction-sweep-through` at frontier-high's
own `:hi-hash`, so the next run's `_correction_sweep_select_position` computes
`pos = through + 1 > ceiling_pos` and returns None on its FIRST call —
`_correction_sweep_apply`, which owns the repair, then runs zero times (measured
during #235: watermark at position 13 of 13, one select call, zero apply calls).
Later commits raise the ceiling but never lower the resume point, so a position
already passed is not revisited either. The sweep heals only what it reaches
while a run is still climbing. `_entity_introduced_by_query`'s docstring and its
stderr warning both used to claim the repair unconditionally, which is false for
every reader holding a finished graph — the one state in which anyone consults
it — and #287 corrected both. An affected graph gets **rebuilt into a fresh
graph path**, like every other condemned graph here.

**A commit's write is a SEQUENCE of transacts, not an atomic unit, and #313 is
what that costs on resume.** `_reverse_apply` writes the commit entity and the
structural facts first, the provisional `:introduced-by` several calls later,
and `_frontier_persist_claim` last of all. A process killed inside that window
leaves an entity TORN: live `:ident`, no `:introduced-by`, no lineage marker —
and because the claim never persisted, the resumed run re-walks that very
position and meets its own wreckage.

Reading live-but-unintroduced as *authoritative* was the defect. The
already-authoritative branch only ever considers `:modified-in`, so the entity
kept no lineage at all; `_correction_sweep_apply` could not repair it either,
because its case 3 reads zero `:introduced-by` values as ambiguous and fail-safe
skips. The run then reported `status: complete` with **zero bytes on stderr**.
Neither at-scale detector saw it: `fact_audit`'s two witnesses agree perfectly
(the index is missing exactly what the graph is missing, so `divergence` reads
0), `stderr_capture` has nothing to read, and `introduced_by_audit` looks for
entities with TWO values where this one has none. `_reverse_apply` now treats a
live entity holding no `:introduced-by` as newly discovered — it is re-applying
the commit whose interrupted write created it, so the guess it writes is the one
an uninterrupted run would have.

**Measure this class over many interrupts, never one.** Rate was 3 of 14
interrupted resumes on an 80-commit linear repo (SIGKILL at ~43 of 80), 0 of 5
uninterrupted — the first five interrupted trials of the original report passed
and briefly read as "resume is clean". The two regression tests are
deterministic instead: they produce the tear through the REAL write path, by
killing the lineage batch mid-`_reverse_apply`, so nothing depends on hitting a
timing window.

A harness that SIGKILLs ingestion must kill the **process group**, not the PID.
`_run_ingestion`'s spawn-context `ProcessPoolExecutor` leaves ~9 orphaned
`spawn_main` interpreters per kill (~66 MB RSS each), reparented to init and
blocked forever on a queue whose write end they hold open themselves. They do
not exit on their own; 34 trials of that exhausts 15 GB.

**R3's zero ident collisions is MEASURED, never proven — and #267 is what keeps
measuring it.** `_canonical_ident`'s rule (keep `_`, drop the hyphen-run
collapse) was chosen over R4's hash suffix in full knowledge that a contrived
path/name pair can still collide: `a/b.py` and `a-b.py` both reach
`:module/a-b-py`. Three guards, answering DIFFERENT questions — never treat one
as covering the others:

  * `TestIdentCollisionRegression263` (in the suite) — the 9 measured pairs still
    separate. A FIXED corpus: catches a regression in the rule, discovers nothing.
  * `evals/at_scale/probe_ident_collision_new_history.py` (#267) — censuses FULL
    history against the LIVE `_code_ident`. The only one that can discover a new
    collision. Runs in the at-scale nightly with `--fail-on-collision`; ~97s over
    835 commits, measured clean (3692 inputs, 3692 idents, 0 offenders).
  * `evals/at_scale/probe_ident_collision_census.py` — **FROZEN at the pre-#263
    rule** and NOT a guard on the shipped rule. It reproduces the audit that chose
    R3, whose predictions were pre-registered against the old baseline. Never
    re-point it at production, and never merge the two: each file's tests assert
    its own baseline in both directions precisely so they cannot drift together.

There is deliberately **no `--since` bound** on the new-history census. A collision
is a property of a PAIR, and the pair to fear is a new entity against an OLD one, so
a bounded collection sees only new-vs-new and reports clean while missing the case it
exists for. A red census step in the nightly is **not** a harness failure — it means
history produced two entities sharing one ident, and #263's rule choice is reopened.

**Graph format version — there is no migration, by design.** `GRAPH_FORMAT_VERSION`
(mcp_server.py) is stamped as `:ingestion/format-version` and ingestion refuses to
run against any other version, including an absent stamp (which means the graph
predates it). Bump it whenever a change makes stored facts unreadable by current
code — today that means the ident rule in `_canonical_ident` (#263), since
ingestion recomputes idents from `(type, path, name)` on every run rather than
reading them back, so an old-rule graph read by new-rule code silently FORKS every
entity instead of erroring.

Do not propose a migration for this. The standing decision is that any graph built
before #222 closes gets **rebuilt into a fresh graph path**, never migrated or
re-ingested in place: several #222-arc fixes (#235, #251/#253, #238/#245, phase 2d)
write facts that do not self-heal on a later ingest, so those graphs are condemned
independently of any one bug. Re-running ingestion over an existing file repairs
nothing. See `docs/superpowers/specs/2026-08-14-ident-rule-r3-and-format-version-design.md`.

**Single-handle invariant.** At most one live `MiniGrafDb` handle may exist per
process. Two handles on one file each cache their own `page_count` and corrupt
each other — the flaky `Page N out of bounds (total pages: M)` (#251, #253,
project-minigraf/minigraf#304). minigraf has enforced this since 1.2.2: a second
open raises `Database is already open in this process` instead of silently
succeeding. That makes the bug visible, not absent.

**The floor is `minigraf>=2.0.0,<3.0.0`** as of #284 item 6. The upper bound is
deliberate: with no cap, CI silently resolved 2.0.0 the day it shipped and ran
red for days before anyone connected the two (#286). A major bump must be a
decision, not a resolver outcome. `install.py` mirrors this spec and is
version-aware — pyproject.toml is canonical, and the two have drifted before.

**What ships in the wheel is a hand-maintained list, and it has been wrong.**
`[tool.setuptools] py-modules` (pyproject.toml) names the top-level modules
setuptools packages; the repo is a flat layout with no package directory, so
nothing is discovered automatically. `frontier_registry.py` landed 2026-07-24
and was imported from `mcp_server.py` without being added — the whole suite
stayed green (the repo root is on `sys.path` in every checkout and every CI
run) and a built wheel died at `import mcp_server` with `ModuleNotFoundError`.
It surfaced only when #82 ran `uvx temporal-reasoning` for real.
`tests/test_packaging.py` now walks the shipped modules' imports transitively
and fails if the list does not cover them.

**PyPI metadata is stamped at RELEASE time, so a dependency cap that is not
released does not exist.** 0.6.0 (2026-07-22) is on PyPI with uncapped
`mcp>=1.27.0` and `minigraf>=1.2.1`; the `mcp<2.0.0` cap landed 2026-08-03 in
`4f630c9`. So every `uvx temporal-reasoning` between those dates resolved mcp
2.x and crashed at `@server.list_tools()` — the exact failure the cap exists to
prevent — while `pyproject.toml` on master read as correct. `install.py` writes
a `.mcp.json` pointing at the PyPI package, so this broke the full install as
much as the MCP-only one. After changing a dependency bound, cut a release or
the bound protects only developers.

**`serverInfo.version` is a two-source ladder, and `0.0.0` counts as absent
(#312).** `Server(name)` with no `version` makes the SDK report *its own*
package version, so the published 0.7.0 told every client it was `1.29.1` — a
number that moved with a user's `mcp` resolution and never with a release
here. `_package_version()` (mcp_server.py) reads
`importlib.metadata.version("temporal-reasoning")` first, then
`.claude-plugin/plugin.json`, then gives up with `unknown`. Both sources are
load-bearing and neither covers the other: plugin.json is NOT in the wheel
(`py-modules` ships four `.py` files and nothing else), so an installed
package has only metadata; and a dev install writes a real
`temporal_reasoning-0.0.0.dist-info` carrying the placeholder that only
`release.yml` stamps, so handling `PackageNotFoundError` alone would report
`0.0.0` on every developer machine and in CI. That is why the placeholder is
treated as absent rather than trusted — and why a fix here is verified
against a built, stamped wheel, not just the checkout.

**project-minigraf/minigraf#287 is still OPEN and is worked around here, not
fixed.** Batching facts that share `(entity, attribute, valid_from)` into one
transact silently keeps only the last, because the EAVT pending index omits
value bytes. It is VERSION-INVARIANT — measured identically on 1.2.3 and 2.0.0
— so the upgrade neither helped nor hurt. `:contains`, `:depends-on` and
`:parent` are therefore transacted ONE PER CALL at four sites; never "simplify"
those loops into a single batch. All four are now regression-guarded.

**Which triples go down that one-per-call path is decided by SUBSTRING match on
the whole rendered triple, and its safety rests on an unwritten invariant:
code-entity string-valued attributes never contain `:`.** Eight sites classify
with `":contains" in t` / `":introduced-by" not in t` and friends
(`_forward_apply`, `_reverse_apply`, `_re_date_structural_facts`,
`_ingest_close`, the correction sweep), matching anywhere in the string —
including inside a quoted VALUE. #232 filed that as silent corruption and was
closed 2026-09-01 as an **accepted residual**, on a measurement rather than an
argument: on the 831-commit at-scale graph, **0 of 7,235** code-entity
`:description`/`:file`/`:path` values contain even one colon, because those
attributes hold identifiers and paths and every classification literal starts
with `:`.

The 24 values that DO carry the literals are commit `:description`/`:subject`
(this repo's own subjects, e.g. "Retract `:introduced-by` at every entity close
site"), and they reach only the two `:contains` splits — where a misroute sends
the triple into the one-per-call loop instead of the batch, i.e. toward the
SAFE path. Nothing is dropped.

**So before adding any free-text attribute to a code entity — a docstring, a
comment, a commit-message-derived summary — reopen #232 and switch those sites
to an anchored attribute-slot match (`^\[\S+\s+:contains\s`) FIRST.** The
corrupting direction becomes reachable the moment such an attribute exists: a
structural triple wrongly filtered out of the re-dated set leaves an entity with
lineage but no type, name or file, silently. The same applies to ingesting a
repository with `:` in a tracked file path.

`mcp_server.py` enforces the single-handle invariant through `_DbLeaseManager`
(#255), which replaced the old `_db = None` "release the lock" idiom — that global is **deleted**, so
ignore any comment still describing it. The refcount is authoritative: the
handle opens at 0 -> 1 and drops at 1 -> 0, and every acquisition in between
reuses it. Before touching DB lifecycle, read the invariant comment above
`_db_native_lock`. Enforcing the invariant by reusing a handle held only by a
live weakref was tried and rejected — it resurrects dead graphs and segfaults
(#253).

**A lease is cheap in-process and exclusive out-of-process, and the difference
decides design questions.** At count > 0 `try_acquire` joins and returns the
same handle, so a concurrent `call_tool` never blocks. But `try_acquire` returns
None while another PROCESS holds minigraf's lock on the graph file, and BOTH
auto-memory hooks (`hooks/claude-code.json`) are `command` hooks in separate
processes — `finalize_hook.py` takes a lease to write each turn's facts. The
retry budget is `_LOCK_RETRY_MAX` x `_LOCK_RETRY_BASE` doubling = **0.75 s
total**, and both hooks swallow failures (`except Exception: pass`). So
lengthening how long ingestion holds a lease does not block queries — it
**silently discards auto-memory writes**. Do not "just hold one lease for the
whole run".

**Locking is in the kernel; there is no PID sidecar to read.** As of minigraf
2.0.0 the lock is `File::try_lock` on the `.graph` file itself (`flock` on Unix,
`LockFileEx` on Windows), released by the kernel on process exit however it
exits. The old `.graph.lock` PID sidecar is gone, so nothing on disk names the
holder, and `mcp_server`'s stale-lock self-heal is gone with it — measured, it
recovered nothing on either version (see `evals/at_scale/benchmark.md`).

Ingestion's #108 "decline instead of racing" pre-check therefore reads **our
own** advisory hint, `<graph_path>.owner`, written by `_graph_owner_hint_held`
and read by `_graph_owner_hint` — never minigraf's lock. Freshness is a
heartbeat-refreshed mtime (`_OWNER_HINT_TTL`, default 30s, override
`MINIGRAF_OWNER_HINT_TTL`), never PID liveness: no portable mechanism can both
name a holder and avoid contending, and `os.kill(pid, 0)` terminates the target
on Windows. Correctness still rests entirely on minigraf's kernel lock — a
wrong hint costs one race or one needless decline, never correctness. The hint
is published only around LONG-held ownership (ingestion), not every lease.

**Dropping the handle is not free: it runs a full O(graph size) checkpoint**
inside minigraf's `Drop for Inner`, outside `_CheckpointPolicy`'s duty gate and
invisible to the trace's `ckpt_d_seconds`. Ingestion currently drops it ~1.02
times per commit, measured at 48% of write time and growing 3.47x within a
220-commit run (#280). See `evals/at_scale/benchmark.md`, "Per-Commit Cost
Attribution".

**Re-walking an already-ingested position is now skipped, and the witness is
the thing that decides whether that is safe (#326 — narrowed by #325 below;
see "Narrowed by #325" a few paragraphs down before treating this as the
current answer to replay cost).** The obvious predicate is
unsound: in `_reverse_apply` `[:commit/<hash> :entity-type :type/commit]` is the
FIRST element of `all_triples`, written before any file result is looked at,
while `_frontier_persist_claim` runs LAST. So the commit entity's presence is
the WEAKEST available witness of a completed write — and it is present on
exactly the torn positions #313 needs re-walked. A fast path keyed on it would
make that orphaned lineage permanent.

The witness is membership in a `:type/completed-region` fact set, archived by
`_frontier_load` from the high interval it is about to discard. A torn position's
claim never persisted, so it was never inside the interval that got archived: it
is in no region and is never skipped, by construction rather than by care. Only
a REPRESENTABLE interval is archived — the inverted case reaches the same branch
and describes no completed region at all.

**Narrowed by #325 below: retention, not this skip fast path, is what now
removes the replay cost of ordinary tip growth.** This section describes the
skip fast path #326 shipped — retiring an already-complete position without
parsing or writing it, keyed on membership in an archived `:type/completed-
region`. #325 replaces `_frontier_load`'s discard-on-tip-growth with
RETENTION (see the #325 section below): a persisted provisional interval
whose bounds and `:pos-count` still check out survives tip growth without
ever being discarded, so the "branch tip grew" case this fast path exists to
shortcut no longer produces an archived region for it to consume. After
#325, `_load_one_interval` archives a region only on the UNRESOLVABLE-bounds
path (a divergent ref whose hash no longer resolves), and
`_completed_regions_load` only ever loads a region whose bounds DO resolve —
mutually exclusive within one run, so nothing this run archives is ever
skipped by this same run's own load. The mechanism below stays correct for
that narrower case; it is not what makes an ordinary resume cheap anymore.

**The obvious statement of why that interval is a witness — "`_frontier_persist_
claim` is the LAST write of a position, so membership in a persisted interval
proves that position completed" — is FALSE as written, and an earlier draft of
this section said it.** `:lo-hash` is a closed RANGE bound, so membership was
only ever implied by a NEIGHBOUR's claim, not by the position's own. A write
that RAISES takes `_run_ingestion`'s per-commit `except`, which logs, does
`processed += 1`, and CONTINUES THE DESCENT — the next lower position that
succeeds moves `:lo-hash` beneath the failed one and sweeps it into the interval.
#313's SIGKILL is safe only because the process STOPS there; the `except` path
does not. The interval was always this imprecise, master included; the archive
is what promotes it into a trusted witness, converting a case master healed by
re-walking into permanent silent loss.

So the interval is made PRECISE instead of the predicate being weakened. The
reverse stream descends monotonically, so the highest position a run failed to
complete is a floor `:lo-hash` may not cross for the rest of that run:
`_reverse_apply` takes `persist_claim`, and the end-of-walk
`_frontier_persist_span` flush clamps its lo bound to the same floor. Both
incompleteness paths raise it — a write that raised and an extraction that
raised — because they are the same defect. There is a third way a reverse
position retires incomplete that does NOT raise it: the shutdown `break` in
`_run_ingestion`'s pipeline loop, which leaves whatever is still in `pending`
unclaimed. That path is safe without raising the floor — nothing lower ever
claims after a shutdown, and `completed_all=False` gates the end-of-walk flush
off — so do not go looking for a third `_note_incomplete_rev` call to match it.
**This withholds BOOKKEEPING, never
WORK**: positions below the floor are still claimed, parsed and written in full,
they simply do not assert completion, so the next run re-walks them. Do not
"optimize" it into skipping the work. Cost is one run's re-walk below a
transient failure, which is what master effectively did anyway. A deterministic
failure at a fixed position therefore blocks reverse-frontier progress below it
for as long as it keeps failing — that is the accepted price of a precise
interval, and it is loud (`stderr_capture` reads the per-commit skip line;
`stderr_capture`'s `skipped_commits`, gated by `run_ingestion_benchmark._exit_code`,
fails on both the extraction case and the write-failure case — `_SKIPPED_COMMIT_RE`
matches both log lines).

**The floor also starves Stage B (the correction sweep), for the whole reverse
region below it, not just below the floor.**
`_correction_sweep_select_position` only selects once the PERSISTED gap reads
closed, and a floored run never advances frontier-high's `:lo-hash` down to
meet frontier-low — so the sweep runs zero times for that entire run, meaning
no lineage confirmation and none of `_forward_apply(..., lifecycle_only=True)`'s
D/R closes, renames, dependency churn or gitlink changes for the whole span.
It self-heals the next time a run completes cleanly; `_should_fold_lineage_watermark`
stays correct throughout, since it requires the sweep to have actually reached
the high bound.

`fwd` never skips, and one clause buys two properties. A forward claim inside a
provisional region is the authority upgrade that must still happen; and
`_forward_apply` mutates `_ForwardWalkState`'s cross-position preload dicts
in place, so a skipped forward position would desynchronize that state for every
later forward position, silently. `_reverse_apply` takes no state object and
reads what it needs from the graph, which is why reverse carries no equivalent
hazard.

The region type is deliberately absent from `MINIGRAF_SCHEMA`, like
`:type/ingest-interval` — `handle_minigraf_audit` iterates exactly the registered
types and would otherwise retract its attributes — and every write goes through
the internal `_transact`/`_retract`, since the public handler rejects an
unregistered type outright. Regions carry a string-valued `:ident` because
enumerating by `:entity-type` binds the entity in UUID space.

**A skipped position still costs `processed`, and that is not sloppiness.**
#317's `commit_census` reads `_ingest_progress["processed"]` as `walk_claimed`,
so excluding skips would silently redefine the number that gate compares against
`git rev-list` and turn a clean skip-heavy resume into a reported lost commit.
The counter is `positions_skipped`, never `skipped`: `status` already takes the
value `"skipped"` (run declined, another process owns the graph) and
`stderr_capture` already reports `skipped_commits` (extraction AND write
failures — `_SKIPPED_COMMIT_RE` matches both log lines).

**`walk_vs_graph` is NOT a backstop on the skip predicate, and an earlier draft
of this section said it was.** `_ingest_progress["processed"]` is SEEDED with
`prior_ingested = _count_commit_entities(db)` and then incremented for every
position retired this run, including positions already counted in that seed. So
`walk_vs_graph` is nonzero on ANY resume that touches already-ingested
territory, skip or no skip — measured 10 with the fast path against 9 without,
on the same scenario. It cannot discriminate a wrong skip from an ordinary
resume.

State plainly what follows: **no existing gate catches a wrong skip.**
`fact_audit`'s `divergence` reads 0 because a skipped commit reaches neither
the graph nor the index, so the two witnesses agree about its absence; both
`:introduced-by` checks only examine entities that EXIST; `stderr_capture` has
nothing to read; and the at-scale `commit_census` runs on a fresh
ingestion-only graph where no archived region exists at all. The predicate's
soundness therefore rests entirely on the witness above and on its positive
control (the #313 torn-position test), plus the two stored denominators
described next — not on anything downstream noticing afterwards.

**A region is stored as two HASHES but consumed as a closed POSITION RANGE, and
that needs a denominator.** Every position between the bounds is
treated as proven-complete, which holds only if the linearization grew by
APPENDING above the region. `git log --topo-order --reverse` guarantees no such
thing: it places a new commit immediately after its branch point whenever the
old tip's line stalls behind it — "branch off an old commit, merge the mainline
in, fast-forward the mainline" is enough. That commit lands INSIDE the archived
bounds and is skipped, permanently and silently.

So the frontier interval carries `:pos-count`, the span it was CLAIMED under,
written at claim time in the same transact as the bound that moved, and
`_frontier_load` archives only an interval whose stored count still matches its
current span. The count MUST come from the interval, not be computed where the
region is archived: a count computed at archive time is computed from the very
span it would then be compared against, so it always agrees and discriminates
nothing. Archived regions carry the same denominator and
`_completed_regions_load` re-checks it, which covers an insertion that lands in
a LATER run. An interval or region carrying NO count is not trusted — "no
denominator" and "a denominator that still checks out" must not be the same
branch when the failure mode is silent permanent loss. Cost of a mismatch is one
full re-walk, i.e. exactly master's behaviour. This is #316's
`code_entities_scanned` idiom: every run re-proves its own positive control.

**The denominator is a CHECKSUM, not a proof of set identity, and the residual
is stated rather than papered over.** Equal count does not imply the same member
set: an old commit inside the range that is neither ancestor nor descendant of
`lo` could be reordered below `lo` by a later `git log --topo-order` while a new
commit lands inside, leaving the count unchanged and the region trusted. A real
repository realizing that was NOT constructed — git's tie-breaking constrains
which of the valid topological orders it actually emits, and the attempt did not
produce one — so this is an undemonstrated residual, not a measured loss. It is
recorded here because "the count makes the mapping SOUND" is the claim an
earlier draft made, and it overstates what a checksum can do. A per-position
marker (approach B in the design spec) is what would close it, at the cost of
one fact per commit on the write path this issue exists to make cheaper.

**The end-of-walk flush's hi bound is the highest SKIPPED position, never the
highest reverse position claimed.** `_frontier_persist_span` moves `:hi-hash`
UP, which `_frontier_persist_claim` never does for the high interval. A reverse
position whose write FAILS takes the per-commit `except`, which does
`processed += 1`, persists no claim, and leaves `completed_all` True — so a
flush bounded by the highest claim would raise the persisted top bound over it.
Once `:hi-hash` reaches the tip the interval is representable, so the next
`_frontier_load` RETAINS it instead of discarding it and nothing ever re-walks
those positions. The skipped span is the only thing the flush is entitled to
assert.

There is no `GRAPH_FORMAT_VERSION` bump — this only adds facts going forward.
Existing graphs are not repaired and need no migration: a region is only
knowable from an interval that exists at discard time, so there is nothing to
seed. They simply never skip until their first discard, and — as this was
true under #326's own discard-on-tip-growth mechanism, where a discarded
interval's bounds stayed resolvable in the very linearization that just
discarded it — archiving in `_frontier_load` at LOAD time meant the run that
discarded was the run that could skip via the archive. **This is no longer
true in general after #325 (below): retention removes the tip-growth case
that made it true, and the discard path that remains is either a count
mismatch (`_load_one_interval` discards without archiving at all — see the
retention paragraph below) or unresolvable bounds (archived, but by
definition NOT resolvable in this same run's linearization, so
`_completed_regions_load` cannot load it back until some later run's
history happens to regain those exact hashes — which ordinary history
rewrites never do in practice).** See the #325 section's "skip fast path is
now VESTIGIAL" paragraph for the corrected, general statement.

`handle_minigraf_ingest_status`'s report carries `positions_skipped_this_run`
alongside a bare `positions_skipped` that is always the same number today — the
counter resets at the start of every run rather than being derived from a
process-lifetime total the way `processed_this_run` is derived from
`prior_ingested`, so there is no separate lifetime figure to look for under the
unqualified name. The per-run figure is the one worth watching: #325's incident
read as healthy for 98 minutes because `processed` climbs on a replayed
position exactly as it does on a new one, and nothing distinguished the two. A
run whose `positions_skipped_this_run` climbs alongside `processed_this_run`
while the graph's own commit count stays flat is re-walking territory it
already holds, not making progress.

**A provisional frontier is now a SET of intervals, not one scalar pair, and
#325 is what makes that safe under a growing branch tip.**
`:ingestion/frontier-high` is the LOWEST provisional interval — "the base" —
read and written at its own fixed ident exactly as before. Any additional
provisional interval a run opens above it (a fresh hole opened by new
commits on the tip, or a reload of one from a prior run) persists as its own
entity, ident-keyed via `_interval_ident(anchor_hash)` and carrying a
string-valued `:ident` fact — `_intervals_read_extra` enumerates by binding
`?ident`, since `[?e :entity-type :type/ingest-interval]` alone answers in
UUID space. The two FIXED idents — `:ingestion/frontier-high` and
`:ingestion/frontier-low` — carry NO `:ident` fact and are read by their
fixed name instead; that is exactly what makes the change migration-free,
since a pre-#325 graph's `:ingestion/frontier-high` fact set is untouched
and `_intervals_read_extra`'s query simply returns nothing extra for it. An
extra interval's ident is MINTED ONCE, at creation, from the anchor commit
hash it first claimed, and NEVER re-derived from its current bounds: a
provisional interval grows downward, so keying on `:lo-hash` would recreate
the entity on every claim, and keying on the current `:hi-hash` would rename
it on every merge — #326 already paid for the bounds-keyed version once
(two regions collided onto one entity, `:pos-count` went nondeterministic
through a last-write-wins join, and a retract destroyed the surviving
witness).

**The retain path's `:pos-count` check is mandatory, not defensive, and the
old path was safe only by accident.** `_load_one_interval` retains a
persisted interval iff both bounds resolve in the current linearization,
`lo <= hi`, and the stored `:pos-count` still equals `hi - lo + 1`. Master's
retain test was just `hi == last position` — no count check at all — and
that was safe only because a genuinely new commit forces a new tip: a commit
landing strictly INSIDE the old bounds makes `hi != last`, which pushed the
case onto the DISCARD path, where the count check already lived. #325
retains `hi < last` (that is the whole feature), which removes the
accident: an insertion strictly inside a retained interval is now reachable,
and without the count check it would be silent, permanent loss — the commit
reaches neither the graph nor the index, so `fact_audit`'s two witnesses
agree, both `:introduced-by` checks only examine entities that exist, and
stderr carries nothing. An interval carrying no `:pos-count` at all is not
retained either — "no denominator" and "a denominator that still checks
out" must not be the same branch when the failure mode is silent.

**`claim_low()` is contiguity-bound, not merely ascending, and that is a
corrected mistake in this branch's own earlier design, not an original
choice.** It serves ONLY the hole immediately adjacent to the authoritative
interval's own edge (`gap_lo == authoritative.hi_pos + 1`, or `gap_lo == 0`
with no authoritative interval yet) and returns `None` for every other
hole, including the lowest unclaimed one. The forward stream's real
contract is CONTIGUOUS FROM C0 — `:ingestion/watermark`,
`_preload_known_entities`'s `watermark_pos` bound, and
`:ingestion/lineage-confirmed-through` all read the authoritative
interval's reach as "the graph knows everything up to here." A `claim_low()`
that served the lowest unclaimed position in general — this branch's
original rule, "still strictly ascending" — would let the forward stream
jump over a retained provisional region it has never visited: a later
commit that merely re-touches an entity introduced inside that skipped
territory then reads as new to the forward walk's watermark-bounded preload
and mints a DUPLICATE `:introduced-by`. `TestMultiStreamParityWithForwardOnly`
(`tests/test_mcp_server.py`) caught exactly this end-to-end, ingesting the
same repo once at the shipping ratio and once forward-only and requiring
the two graphs to agree — master could never reach this bug because there
was only ever one gap. `claim_high()` needed no equivalent fix: the reverse
stream carries no contiguity contract, and serving the topmost hole before
falling through to the bulk gap is the entire point of #325.

**The reverse claim floor is now PER-INTERVAL (`rev_claim_floor`, keyed by
target ident), and a merge must carry the absorbed interval's floor to the
survivor.** #326 shipped a single run-global floor scalar, correct only
while the reverse stream made one contiguous descent. #325's allocator can
serve a tip gap and a disjoint bulk gap in the same run (`claim_high()` plus
a merge into a retained interval), so a write failure in the tip gap must
floor only the ident it belongs to — a run-global floor would sit above
every position in the disjoint bulk gap and withhold that gap's bookkeeping
entirely, for no reason, forcing a needless re-walk of work that actually
completed. The harder failure runs the other way: a merging claim's target
is always the SURVIVOR (`_coalesce`'s rule keeps the base, or else the lower
interval), and the survivor need not carry the absorbed interval's own floor
entry — floors are set at write-dispatch time, strictly after every claim in
the pipeline window has already happened, so the absorbed ident's floor
entry may not even exist yet when the merging claim is first allocated.
Checking only `claim_ident` at dispatch reads a floored merge as
unrestricted and persists a union spanning the very gap the failure sits
in, permanently sweeping a genuinely failed write into the graph's own
completion witness. The fix widens the dispatch-time check to
`max(rev_claim_floor[i] for i in [claim_ident, *absorbed_idents] if i in
rev_claim_floor)` — reproduced end-to-end by `TestPerIntervalReverseFloor::
test_merge_does_not_launder_a_floored_positions_failure_through_the_survivor`:
a minted tip interval floored by a failed write is later merged into a
retained base, and without the fix the base's persisted range silently
swallows the floored position. Ablation-proven, not merely asserted:
reverting the dispatch check to `claim_ident` alone reproduces the exact
regression the fix closes — `:ingestion/frontier-high` ends up persisted as
`[2,12]` with position 11 (the write that genuinely failed) inside that
range and no `:commit/...` entity for it (commit `c47e7c4`).

**The one genuine per-commit write-path cost this branch adds: a merging
claim now pays two extra reads.** `_frontier_persist_claim`'s
absorbed-interval loop (`mcp_server.py:7023` `_frontier_read_bounds`,
`:7032` `_frontier_read_pos_count`) reads both per absorbed ident before
discarding it, which master's single-interval version never did — there
was nothing to absorb. This is O(intervals) — bounded by how many idents
one claim's merge absorbs (0 in the common non-merging case, small in
practice, since a merge happens once per interval's lifetime when it first
touches a neighbour) — never O(commits), so it does not change the
per-commit cost SHAPE the rest of this section describes. It is stated here
rather than left to be found only in an ephemeral report, the way the
handle-drop checkpoint cost above is documented in comparable detail.

**The end-of-walk flush refuses a skipped-span persist on a DISJUNCTION,
`lo_pos > floor or len(sources) > 1`, and both halves are load-bearing — do
not describe either as covering the other.** `_frontier_persist_span`'s
union is advance-only and GAP-BLIND: it bridges from a flush target's
EXISTING on-disk `:hi-hash` to the flushed span's hi in one shot, proving
nothing about what lies in between. `lo_pos > floor` catches the shape
where the flushed span's own lo already sits above the floor, so the
ordinary clamp (`max(lo_pos, floor + 1)`) degenerates to a no-op and only
outright refusal still does anything. `len(sources) > 1` catches a SEPARATE
shape `lo_pos > floor` cannot see at all: a fold whose merged span
STRADDLES the floor (`lo_pos <= floor < hi_pos`). There the clamp fires and
looks fine in isolation — the clamped `lo_pos` still sits inside
`[lo_pos, hi_pos]` — but the union is against the fold TARGET's existing
hi, which a fold is exactly what can put far BELOW the floor, so the bridge
crosses the floored position regardless of what `lo_pos` was clamped to.
Only the STRADDLING shape is reproduced end-to-end, by
`tests/test_mcp_server.py::TestFoldedSkipSpanFlushDoesNotLaunderAFloor` —
and both of that class's two tests are FOLDS (`len(sources) == 2` in both),
so `len(sources) > 1` alone already refuses either one on its own. The
shape that would isolate `lo_pos > floor`'s own contribution — call it case
C: no fold (`len(sources) == 1`), span starts above the floor — is NOT
ruled out by needing a fold: within ONE ident, a same-run skip at a HIGHER
position followed by a write failure at a LOWER one produces exactly
`lo_pos > floor` with `sources == {that ident}` and no merge anywhere
(`skipped_span`/`skipped_span_sources` default to the singleton `{ident}`
until a merge unions another ident in, and `_note_incomplete_rev` floors
whichever ident the failing claim already belongs to — nothing here
requires two idents). **An earlier version of this section said case C
"is not constructible under today's allocator" and gave the fold
requirement as the reason — that reason is wrong, and so is the
conclusion it was used to support.** What actually keeps case C hard to
reach today is narrower and unrelated to folds: `_skip_claim` (the thing
that populates `skipped_span` at all) requires a loadable archived
`:type/completed-region` covering the position, and after #325 that only
exists for the narrow divergent-ref-regained case (see "#326's skip fast
path is now VESTIGIAL" above) — so constructing case C requires first
constructing that already-narrow prerequisite. No test isolates
`lo_pos > floor` from `len(sources) > 1`; that half is belt-and-braces
against exercising this narrow same-ident shape, not a demonstrated
requirement. **An earlier
version of this same comment in `mcp_server.py` claimed testing
`lo_pos > floor` alone "needs none of that side reasoning"
`len(sources) > 1` rested on — that claim was the bug it now describes**:
it swapped one case for the other instead of widening the refusal, and the
straddling scenario is the reachable counter-example. Refusing on either
condition costs nothing but a re-walk next run, the same accepted price
#326 established throughout.

**A base-less provisional side is reachable, and #325 repairs it ON DISK,
not just in memory.** If `frontier-high` is discarded on a count break (a
commit landed inside its own bounds) while an extra interval strictly above
it is unaffected and stays retained, nothing in the loaded set carries
`is_base` — `_load_one_interval` only ever passes `is_base=True` for
`:ingestion/frontier-high` itself, and `frontier_registry._extend`'s
`is_base = not any(iv.tag == tag for iv in self._intervals)` cannot
self-heal this once any same-tag interval already exists. Left alone,
`:ingestion/frontier-high` never comes back: `_correction_sweep_select_
position` returns `None` unconditionally on `high_bounds is None`, so Stage
B (the correction sweep) never runs again for the life of the graph,
`:ingestion/lineage-confirmed-through` never advances, and provisional
`:introduced-by` stays provisional forever — on a run that reports
`status: complete`. `_frontier_promote_base_if_missing` (`mcp_server.py`)
fixes this by picking the LOWEST retained extra (the one that would have
become the base under ordinary claiming, had frontier-high not been
discarded out from under it), retracting its facts at its own minted
ident, and re-writing them at the fixed `:ingestion/frontier-high` ident —
copying its stored `:pos-count` VERBATIM, never recomputed, since
recomputing would discard the claim-time origin the retain check itself
depends on. The write happens on disk, not only on the in-memory allocator,
because the write dispatch re-mints a claim's persist target from
`interval.is_base`/`.anchor_pos` every run, so an in-memory-only promotion
would still persist through the OLD minted ident next time.

**Stage B (the correction sweep) declines outright while the provisional
side is fragmented, and that is exactly what lets `:ingestion/correction-
sweep-through` keep its old meaning with no new semantics.**
`_correction_sweep_select_position` returns `None` whenever
`_intervals_read_extra(db)` is non-empty — a hole remains above
`frontier-high`, so Stream 2 could still descend past a position the sweep
would otherwise confirm, exactly the same "gap not yet closed" reasoning
the pre-#325 design already used for the single-interval case. Once
everything coalesces back into one provisional interval, the sweep's
existing gap-closed test is exact again and needs no change.

**#326's skip fast path is now VESTIGIAL for the case it was built for —
corrected in place above, restated here for the reader who lands on this
paragraph first.** After #325, `_load_one_interval` archives a
`:type/completed-region` only on the UNRESOLVABLE-bounds path (a divergent
ref whose hash no longer resolves in this linearization), and
`_completed_regions_load` only ever loads a region whose bounds DO
resolve — mutually exclusive within one run, so nothing this run archives
can be skipped by this same run's own load. **What #326 actually did,
measured rather than assumed: on MASTER, tip growth does NOT force a full
re-walk.** Master discards the off-tip interval on `_frontier_load`, but
archives it as a completed region whose hashes still resolve in the very
same run, and `_skip_claim` intercepts same-run, so master applies only the
newly appended positions. "#326 already removed #325's headline replay
cost" is therefore CORRECT for plain tip growth — what #325 changes is that
the interval is now RETAINED rather than discarded-and-immediately-
re-skipped, which matters for the count-check safety property above, not
for the ordinary-resume cost that #326 had already fixed.

**The `:pos-count` residual is unchanged: it is a CHECKSUM, not a proof of
set identity — do not upgrade that language to "sound."** Equal count does
not imply the same member set; the undemonstrated (not measured) residual
described in the #326 section above still applies verbatim to every
interval #325 retains, extras included.

**No `GRAPH_FORMAT_VERSION` bump, no migration.** #325 only adds facts
(extra interval entities) and changes a retain/discard boundary condition;
a pre-#325 graph has at most one provisional interval on disk
(`:ingestion/frontier-high`) and is read correctly by the new code with no
seeding step.

**The resume census probe (`evals/at_scale/probe_resume_census.py`, #325)
is the only at-scale check that can observe a wrongly-retained interval at
all.** It gates on `census_error is None and repo_vs_graph == 0`
(`resume_ok`), never `collect_commit_census`'s own `ok`
(`ident_collisions`, `walk_vs_graph` always, `repo_vs_walk` when complete).
Two earlier explanations of why were wrong; the mechanism is the one
already measured in the #326 section above (`walk_vs_graph` "is nonzero on
ANY resume that touches already-ingested territory, skip or no skip —
measured 10 with the fast path against 9 without"). That RE-TOUCHED set
includes the #326 same-run skip fast path, #313's torn-position repair
re-walk, and this branch's own below-`rev_claim_floor` re-walk — **all
three are CORRECT behaviour, not degraded resumes**, which is exactly what
makes a nonzero `walk_vs_graph` on any of them a FALSE positive rather than
a real one. `_ingest_progress["processed"]` counts positions RETIRED this
run (skip, extraction failure, or reaching write dispatch — three increment
sites, `mcp_server.py:12949, 13001, 13128`), never commits newly WRITTEN,
and is SEEDED with `prior_ingested = _count_commit_entities(db)` at run
start, so re-touching a position already inside that seed double-counts it
and drives `walk_claimed` — and `walk_vs_graph = walk_claimed -
graph_commit_entities` — positive on a perfectly healthy run.
`collect_commit_census` gates `walk_vs_graph` strictly BEFORE `repo_vs_walk`
(an `elif` chain, `commit_census.py`), so `walk_vs_graph` is the clause that
actually fails such a run. `repo_vs_walk`'s own clause
(`elif complete and deltas["repo_vs_walk"]:`) is a truthiness test that is
only reached once `walk_vs_graph` reads falsy (zero) — at which point
`walk_claimed == graph_commit_entities` forces `repo_vs_walk ==
repo_vs_graph`, zero on an intact graph, so the clause is falsy there too.
`repo_vs_graph` sidesteps both gates because it never routes through
`walk_claimed` at all. The nightly's own `commit_census` (#317) cannot
exercise this failure mode regardless, for a simpler reason than any of
that: it runs once on a fresh graph, which has no interval to retain in the
first place. `retention_engaged`
(`prior_ingested > 0 and processed_this_run < repo_commits`) is the probe's
positive control, RENDERED never gated: without it, a full regression back
to pre-#325 discard-on-tip-growth would re-walk everything on the "resume"
and still land on `repo_vs_graph == 0` (minigraf collapses a re-transacted
commit triple at an identical `commit_ts_iso` rather than duplicating it),
so `ok` alone cannot tell a correct skip apart from a wasteful total
re-walk that happens to land on the same total. It has ZERO MARGIN at the
boundary — a partial regression that re-walks all but one already-ingested
position still reads `True`, so it discriminates a TOTAL regression in the
retention predicate, not a partial one. The nightly step pins
`--branch`/`--truncate-by` to a fixed slice for cost, which is enough to
catch a regression in the retention predicate itself but can NEVER observe
a newly landed commit arriving INSIDE an already-retained interval's
bounds — the exact scenario the `:pos-count` checksum above exists to
catch — because the frozen slice never grows.

**A LOADED provisional set is now coalesced too, and #329 is why that is
not merely tidiness.** `frontier_registry._coalesce` runs only from
`_extend`, `_extend` only from a claim, and `FrontierAllocator.__init__`
stores what it is handed verbatim. So a load producing two contiguous or
overlapping provisional entities **with an already-empty gap** never merged:
no claim ever happens. `_intervals_read_extra` was then permanently
non-empty, and that condition makes both
`_correction_sweep_select_position` and `_should_fold_lineage_watermark`
return early — so Stage B never ran again,
`:ingestion/lineage-confirmed-through` never advanced, and provisional
`:introduced-by` stayed provisional for the life of the graph, on runs
reporting `status: complete` with a clean `divergence` and zero bytes on
stderr. **No detector saw it and it did not self-heal**, which is the
failure profile this arc exists to refuse: graph and index agree (nothing
is missing from either, the lineage is simply never upgraded), both
`:introduced-by` checks only examine entities that EXIST and are
well-formed, `stderr_capture` has nothing to read, and `commit_census`
compares commit counts that are correct.

The merge rule is now the module-level `frontier_registry.coalesce_
intervals`, called from BOTH `_coalesce` and `_frontier_load`'s
`_frontier_coalesce_loaded` — shared, not mirrored, so the load-time merge
cannot drift from the claim-time one. It runs AFTER
`_frontier_promote_base_if_missing`, and that order is load-bearing: the
survivor rule PRESERVES a base but never manufactures one, so merging first
would leave the union at a minted ident while `:ingestion/frontier-high`
stays absent — the very state that function exists to repair.

**The survivor's new `:pos-count` is the merged span, and that is a
CLAIM-TIME denominator, not #326's computed-where-it-is-read trap.** The
difference is which run does the comparing. Both components were validated
against THIS run's linearization moments earlier (`_load_one_interval`
retains only when the STORED claim-time count still equals the current
span), and their adjacency was established in that same linearization — so
the merged count is a fresh assertion about THIS run, compared in a LATER
run against a linearization that may differ. It discriminates. #326's
archive case was different: archiving and loading ran in the SAME run
against the SAME linearization, so the count always agreed. **Accepted
cost:** merging is coarser, so a later commit landing inside what used to be
the upper component now discards the whole union rather than that component
alone — a bigger re-walk, never a loss. The `:pos-count` residual is
unchanged: it stays a CHECKSUM, not a proof of set identity.

**The post-condition's two violations have deliberately different
consequences.** `_frontier_check_load_invariants` RAISES on an adjacent or
overlapping provisional pair — unreachable once the coalesce lands, so it
should never fire, and a raise reaches `_run_ingestion`'s run-level
`except` before any walk starts (`status: error`, traceback on fd 2, so
`stderr_capture`'s `error_signals` and `_exit_code` fail the at-scale
gate). It only WARNS when the base is not the lowest provisional interval:
coalescing does not enforce that — two DISJOINT intervals with a real gap
never merge, and `_intervals_read_extra` carries no positional predicate,
so a below-base extra would load. Nothing produces that state today and it
degrades conservatively, and raising on it would abort every future run on
such a graph forever with no repair path — permanent denial of service,
worse than the state being guarded. Cross-tag overlap is deliberately NOT
checked: it is unreachable (claims are served from `_unclaimed`, the
complement of the interval set), and cross-tag ADJACENCY is the normal
converged state, since the authoritative/provisional boundary is the
lineage frontier itself.

**No `GRAPH_FORMAT_VERSION` bump and no migration.** This changes no fact
shape — it retracts facts that already exist and widens bounds that already
exist, using the attributes `_frontier_persist_claim` already writes. This
is the one case in this arc that DOES self-heal an affected graph, because
the defective state is by definition present at load time and the fix runs
at load time.

## Claude Code Plugin Publishing

The plugin is published via a stub architecture — `install.py` handles all registration automatically.

**Why a stub:** Claude Code's internal copier (`mc$()`) copies the plugin source tree to a versioned cache. REPO_DIR contains `.venv/` (hundreds of MB), causing the copy to fail silently. `install.py` builds a minimal stub at `~/.claude/plugins/stubs/temporal-reasoning-local/` containing only `.claude-plugin/` and `skills/`, which `mc$()` can copy successfully.

**Five files that must be correct** (all written by `install.py`):

1. `~/.claude/plugins/stubs/…/.claude-plugin/marketplace.json` — must have `owner` field; plugin `source: "./"`
2. `~/.claude/plugins/stubs/…/.claude-plugin/plugin.json` — plugin identity and version
3. `~/.claude/settings.json` — `enabledPlugins` + `extraKnownMarketplaces` → stub dir
4. `~/.claude/plugins/installed_plugins.json` — `installPath` → versioned cache dir
5. `~/.claude/plugins/known_marketplaces.json` — **authoritative store**; `source.path` and `installLocation` → stub dir (settings.json changes don't propagate here automatically)

**Version bumps:** canonical version lives in `.claude-plugin/plugin.json`; `install.py` reads it via `PLUGIN_VERSION`. Stale versioned cache dirs are deleted on each run. That read has **no fallback** and exits on failure, deliberately: the version names the cache dir Claude Code is told to copy the stub into, and the same run deletes every cache dir that is not it — so a guessed version deletes the working install and registers a path nothing will populate, while the script prints a tick and exits 0. It used to fall back to a hardcoded `0.3.0`.

**Diagnosing failures:** `claude plugin list` shows per-plugin status and errors. "Plugin X not found in marketplace Y" means marketplace.json failed validation — check the `owner` field and run `claude plugin validate <stub-dir>`.

**Official directory structure** (from docs): the recommended layout separates marketplace root from plugin subdir, with `source: "./plugins/my-plugin"` in marketplace.json. Our stub uses `source: "./"` (root = plugin), which is non-standard but works. A proper separation would let `mc$()` copy without the stub workaround.

**Offline resilience:** set `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` to prevent Claude Code from wiping the marketplace cache when a git pull fails.

**Files outside plugin dir:** plugins are copied to cache, so `../relative-paths` break. Use symlinks if the plugin needs to reference files outside its directory.

## Query Examples

```python
# Basic query
minigraf_query(datalog="[:find ?x :where [?e :attr ?x]]")

# With temporal
minigraf_query(datalog="[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Count
minigraf_query(datalog="[:find (count ?e) :where [?e :description ?d]]")
```
