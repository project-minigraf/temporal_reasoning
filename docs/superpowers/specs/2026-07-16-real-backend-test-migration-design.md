# Real-Backend Test Migration — Design

**Date:** 2026-07-16
**Issue:** #133

## Problem

`tests/test_mcp_server.py`'s module docstring claims "All tests mock MiniGrafDb so no live
minigraf install is required." That was true at scaffolding (commit c64b566, 2026-05-02) but
`minigraf>=1.2.1` has been a hard runtime dependency since, not optional, and real-backend
tests already exist and run fast (~40s for the whole suite).

The `mock_minigraf_db` fixture patches `MiniGrafDb` with a `MagicMock` whose `execute()` never
parses or validates the Datalog string passed to it — it just records call arguments and
returns a canned response. This blind spot let a real bug ship for months: every valid-time-
bounded `(transact ...)` call in `mcp_server.py` constructed the command with the facts vector
and options map in the wrong order — `(transact [facts] {options})` instead of minigraf's
documented `(transact {options} [facts])` — silently making minigraf ignore `:valid-from`/
`:valid-to` bounds and stamp every fact with wall-clock "now" instead. Every "closed" entity
(deletions, renames, modifications) never actually became invisible at current time, and
`:valid-at` point-in-time queries never correctly resolved historical state, for the entire
project history. Mocked tests never parse the Datalog string, so an argument-order bug inside
it is invisible to them; the few existing real-backend tests mostly checked "does the fact
exist at default/current time," which behaves like `:any-valid-time` regardless of bounds and
so never exercised the one query shape the bug actually broke. The transact bug itself was
fixed on `fix-git-ingestion-rename-tracking-111-113` (commit `1b2e262`); this issue is about
closing the systemic testing gap that let it go undetected.

## Current state (re-audited 2026-07-16, this session)

The issue's own numbers — "304 of 532 tests (57%) mock the DB" — are stale. Verified via
pytest's fixture-closure introspection (not string grepping, which double-counts): **160 of
582 currently-collected tests (27%)** use the `mock_minigraf_db` fixture. The ratio dropped
because five fixes since the issue was filed (#111/#113, #134, #137, #112, #130) each added
real-backend regression tests following the pattern this issue wants to formalize.

`MiniGrafDb.open_in_memory()` exists in the installed `minigraf` package (confirmed via
`inspect.signature` and a live round-trip: transact + query against real Datalog, no disk I/O)
even though it isn't documented in this repo's `SKILL.md`/`CLAUDE.md`. This makes an
always-real, always-fast test suite practical: no tmp-file-per-test I/O cost, full real
Datalog parsing/schema validation/bi-temporal semantics.

## Goal

Eliminate `mock_minigraf_db` and the `MagicMock`-based faking pattern entirely. Every test
exercises a real minigraf backend and asserts against real query results — not against mock
call arguments. The only mocking that survives is for genuinely external, non-minigraf
network APIs (LLM provider clients, GitHub), and that survives because it isn't minigraf
mocking at all.

## Design

### The `real_db` fixture

Replaces `mock_minigraf_db` as the default fixture used across the file. Monkeypatches
`MiniGrafDb.open` to redirect to the real `MiniGrafDb.open_in_memory()` for the duration of
the test, then calls the real `mcp_server.open_db(path)` — so `_open_db_at`'s actual
production code path runs for real (session-rule registration via `_db_execute`, mtime
tracking via `os.path.getmtime`, which raises `OSError` for the non-existent in-memory "path"
and is already caught with a graceful `_db_mtime = 0.0` fallback — no change needed there).

```python
@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Open a real (non-mocked) in-memory MiniGrafDb for the duration of the test.
    Full Datalog parsing, schema validation, and bi-temporal semantics — just backed
    by open_in_memory() instead of a disk file, so tests stay fast."""
    from minigraf import MiniGrafDb
    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()
```

Verified working end-to-end against the installed minigraf package: `open_db()` returns a live
handle, `_db_mtime` defaults to `0.0` without raising, and `execute()` performs real
transact/query round-trips.

### Assertion style: always verify results

Every migrated test stops asserting on mock call arguments (e.g. `"transact" in str(call)`,
`assert_called_once()`) and instead re-queries `real_db` after the handler runs, asserting on
the actual persisted or returned facts. This is the pattern already proven by the existing
real-backend tests (`test_contains_filter_actually_matches_against_real_graph`,
`test_renamed_to_is_open_ended_against_real_graph`,
`test_removed_field_secondary_attrs_are_closed_against_real_graph`,
`test_submodule_removal_closes_entity_type_and_path_against_real_graph`) — this issue
generalizes it to the rest of the file rather than inventing a new style.

### Special-case clusters — where something other than plain `real_db` is needed

These are the only places a full swap-to-`real_db`-and-rewrite isn't the whole story:

1. **DB lock-retry / self-heal** (`TestGetDbLockRetry` — 6, `TestTryOpenWithSelfHealReuse` —
   1, `TestOpenDbAtWithExtendedRetry` — 4; 11 tests total). Locking is inherently a
   file-on-disk concept, so these keep real file-backed `MiniGrafDb.open()` (not
   `open_in_memory()`) and manufacture genuine lock contention instead of a mocked exception.
   Verified live: a second real `open()` against a path already held open by a subprocess
   raises the exact `MiniGrafError` (`"Database is locked by another process (lock file: ...,
   holder PID: ...)"`) that `_stale_lock_holder_pid`/`_pid_is_alive` parse. A helper spawns a
   subprocess that opens the DB and either holds it (for "retries then succeeds" / "holder
   still alive" cases) or opens-then-exits immediately (to leave a real stale lock with a
   genuinely dead PID, for the self-heal cases). Only `mcp_server.time.sleep` gets
   monkeypatched, to skip real backoff delays — that's test-speed plumbing, not faking
   minigraf, and was confirmed acceptable rather than requiring real multi-second waits.

2. **LLM strategy tests** (`TestLlmStrategyOpenAI` — 2, `TestLlmStrategy` — 2,
   `TestAgentStrategy` — 1; 5 tests). These test `mcp_server`'s own extraction logic given a
   canned LLM response, not minigraf. The OpenAI/Anthropic client mock stays (avoids real
   network calls, API cost, and non-deterministic model output in CI) but the DB fixture
   underneath moves from `mock_minigraf_db` to `real_db`, and assertions about what gets
   transacted move from mock-call inspection to real-query verification.

3. **`TestMinigrafReportIssue`** (3 tests) — the `report_issue` module's GitHub-API-touching
   functions stay mocked for the same external-API reason; DB fixture moves to `real_db`.

4. **`TestGetDbConcurrentResetRace`**'s hand-rolled `FakeDb` class (1 test) — this is a
   home-grown mock in spirit even though it's not `MagicMock`-based, and gets replaced with a
   real in-memory `MiniGrafDb` instance. The test's actual subject (a race in `get_db()`'s
   double-read of the module-level `_db` global, exercised via `sys.settrace`) doesn't care
   what `execute()` returns, so this is a direct swap.

Everything else — roughly 140 tests across `TestOpenDb`, `TestMinigrafQuery`/`Transact`/
`Retract`, the schema-validation classes, `TestIngestionWrites`, the bi-temporal-close/deps
classes, gitlink/submodule tests, index-cache tests, MCP tool-wiring tests, and ingestion
orchestration tests — gets a straightforward fixture swap plus assertion rewrite. No new
special-case handling; `mock_minigraf_db` is deleted once nothing references it.

Not in scope: `unittest.mock.patch("threading.Thread")` in a couple of `TestIndexCache` tests,
which verify a thread constructor wasn't called to prove no redundant rebuild was spawned.
That's inspecting our own concurrency control, not faking minigraf, so it's unaffected by this
migration.

### Bug-discovery handling

If migrating a test surfaces a real correctness bug — plausible, since this is exactly the
class of gap that hid the transact argument-order bug — fix it in the same PR with a
regression test, matching the established precedent from every prior issue in this sequence
(#111/#113, #134, #137, #112, #130 each fixed root causes discovered mid-review rather than
filing separately).

### Documentation

- `tests/test_mcp_server.py`'s module docstring: replace "All tests mock MiniGrafDb so no live
  minigraf install is required" with a description of the `real_db` fixture and the narrow
  external-API exception.
- New `docs/testing-conventions.md`: documents the `real_db` fixture, the "always verify
  results" assertion rule, the lock-retry cluster's subprocess-based real-condition technique
  as a reusable pattern, and the external-API-mocking exception with its rationale. Linked
  from `CLAUDE.md`.

## Scope and sequencing

~150 of 582 tests get rewritten (fixture swap + assertion rewrite, not just re-parameterized)
across a 9,268-line file — a large, mechanical-but-not-trivial diff. Ships as a single PR (per
the pattern every prior issue in this sequence has followed), but the implementation plan
sequences the rewrite by class/cluster with a full-suite run between clusters, so a regression
introduced partway through is caught against the last-known-good cluster rather than surfacing
only at the very end.

## Out of scope

- `tests/test_install.py`, `tests/test_report_issue.py` — neither uses `mock_minigraf_db`;
  not touched.
- Adding `MiniGrafDb.open_in_memory()` documentation to `SKILL.md` for end users — this issue
  is about the test suite, not the public skill surface. (Worth a follow-up note if the
  Python-wrapper docs are ever revisited, but not this issue's job.)
