# Provisional/Authoritative Lineage Fact Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build sub-phase 2a of #222's phase 2: the provisional/authoritative lineage fact model and candidate-diff persistence primitives. No caller yet — 2b (Stream 2's walk) writes with these, 2c (Stream 1's correction sweep) reads/clears with them, 2d wires concurrency.

**Architecture:** All new persistence functions live in `mcp_server.py`, following the exact query-before-write idempotent pattern `_watermark_update`/`_frontier_persist_claim` already established. Two new entity types (`:type/lineage-marker`, `:type/candidate-diff`) are deliberately unregistered in `MINIGRAF_SCHEMA` — safe from `minigraf_audit`'s sweep since it only iterates known types — but every write MUST go through the internal `_transact`/`_retract` helpers directly, never the public `handle_minigraf_transact`/`handle_minigraf_retract` MCP handlers, whose own write-time validation gate would reject an unregistered type outright. One new watermark entity (`:ingestion/lineage-confirmed-through`) deliberately *is* registered (`:type/ingestion`, same as `:ingestion/watermark`) and must carry the same required `:description` constant.

**Tech Stack:** Python 3, minigraf Datalog (via existing `_db_execute`/`_transact`/`_retract`/`_edn_escape` helpers), pytest.

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-24-lineage-provisional-fact-model-design.md` (revised 4 times after review — every fix in it is binding, not just the final prose). In particular:
  - `:type/lineage-marker` and `:type/candidate-diff` are **not** added to `MINIGRAF_SCHEMA` — do not add them there.
  - Every new persistence function must call internal `_transact`/`_retract` directly — never `handle_minigraf_transact`/`handle_minigraf_retract`.
  - Every new write is idempotent via query-before-write (read current state, no-op if unchanged, retract-then-reassert only if it genuinely differs) — never a blind unconditional transact.
  - `:ingestion/lineage-confirmed-through` uses entity-type `:type/ingestion` (registered, audited) and must carry the same `:entity-type`/`:ident`/`:description` constants pattern `_watermark_update` uses.
- Follow `docs/testing-conventions.md`: every test uses a real `MiniGrafDb` (`real_db` fixture), never mocked.
- Phase 2a only. Do not wire any of these functions into `_run_ingestion`'s actual commit loop or into `_frontier_seed_from_watermark`'s existing migration branch — the one exception is a single new call added to `_frontier_load` (Task 3), which is this sub-phase's only change to existing code.

---

### Task 1: Provisional lineage marker (`_lineage_mark_provisional` / `_lineage_confirm` / `_lineage_is_provisional`)

**Files:**
- Modify: `mcp_server.py` — insert after `_frontier_persist_claim` (currently ends at line 5017, immediately before `_LAST_RUN_KEYWORD_ATTRS` at line 5020).
- Test: `tests/test_mcp_server.py` — new `TestLineageProvisionalMarker` class.

**Interfaces:**
- Consumes: `_db_execute(db, datalog) -> str`, `_transact(db, datalog_facts, valid_from, index_con=None) -> str`, `_retract(db, datalog_facts, index_con=None) -> str` (all pre-existing).
- Produces:
  - `_LINEAGE_MARKER_ENTITY_TYPE: str = ":type/lineage-marker"`
  - `_lineage_marker_ident(entity_ident: str) -> str`
  - `_lineage_mark_provisional(db, entity_ident: str, commit_ts_iso: str, index_con=None) -> None`
  - `_lineage_confirm(db, entity_ident: str, index_con=None) -> None`
  - `_lineage_is_provisional(db, entity_ident: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Add near the top of `tests/test_mcp_server.py`, alongside other module-level imports if not already present (check first — `import frontier_registry` was added in phase 1's Task 3):

```python
import frontier_registry
```

Add this test class (e.g. right after the existing `TestFrontierPersistClaim` class, currently ending around line 6086, before `TestGitCommitsTopoOrder`):

```python
class TestLineageProvisionalMarker:
    def test_unmarked_entity_reads_as_authoritative(self, real_db):
        import mcp_server
        db = real_db
        assert mcp_server._lineage_is_provisional(db, ":function/src-auth-py-login") is False

    def test_mark_then_confirm_round_trip(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        mcp_server._lineage_mark_provisional(db, entity_ident, "2026-01-01T00:00:00Z")
        assert mcp_server._lineage_is_provisional(db, entity_ident) is True

        mcp_server._lineage_confirm(db, entity_ident)
        assert mcp_server._lineage_is_provisional(db, entity_ident) is False

    def test_confirm_already_authoritative_entity_is_a_noop(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        # Never marked -- confirming must not raise or create anything.
        mcp_server._lineage_confirm(db, entity_ident)
        assert mcp_server._lineage_is_provisional(db, entity_ident) is False

    def test_mark_provisional_is_idempotent(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        mcp_server._lineage_mark_provisional(db, entity_ident, "2026-01-01T00:00:00Z")
        mcp_server._lineage_mark_provisional(db, entity_ident, "2026-01-01T00:00:01Z")

        ident = mcp_server._lineage_marker_ident(entity_ident)
        raw = mcp_server._db_execute(db, f"(query [:find (count ?e) :where [{ident} :entity ?e]])")
        assert json.loads(raw)["results"] == [[1]]

    def test_provisional_marker_survives_audit(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"
        mcp_server._transact(
            db,
            f'[[{entity_ident} :entity-type :type/function] '
            f'[{entity_ident} :description "login"] '
            f'[{entity_ident} :file "src/auth.py"]]',
            "2026-01-01T00:00:00Z",
        )

        mcp_server._lineage_mark_provisional(db, entity_ident, "2026-01-01T00:00:00Z")
        result = mcp_server.handle_minigraf_audit()

        assert result["retracted"] == 0
        assert mcp_server._lineage_is_provisional(db, entity_ident) is True
        raw = mcp_server._db_execute(
            db, f'(query [:find (count ?d) :where [{entity_ident} :description ?d]])'
        )
        assert json.loads(raw)["results"] == [[1]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestLineageProvisionalMarker -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_lineage_is_provisional'` (or similar for the other new functions).

- [ ] **Step 3: Add the functions to `mcp_server.py`**

Insert immediately after `_frontier_persist_claim` (line 5017), before `_LAST_RUN_KEYWORD_ATTRS` (line 5020):

```python
_LINEAGE_MARKER_ENTITY_TYPE = ":type/lineage-marker"


def _lineage_marker_ident(entity_ident: str) -> str:
    """Deterministic companion-entity ident for entity_ident's provisional
    marker. Not a public schema type -- see the #222 phase 2a design spec's
    "Schema/audit status of new entity types" section.
    """
    return f":lineage/{entity_ident.lstrip(':').replace('/', '-')}"


def _lineage_mark_provisional(
    db: Any, entity_ident: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Create the :type/lineage-marker companion entity for entity_ident, if
    one doesn't already exist. Query-before-write (mirrors _watermark_update)
    -- a marker already present is a no-op, never a duplicate write. Uses
    internal _transact directly, never handle_minigraf_transact: :type/
    lineage-marker is deliberately unregistered in MINIGRAF_SCHEMA, and the
    public handler's schema gate would reject it outright.
    """
    if _lineage_is_provisional(db, entity_ident):
        return
    ident = _lineage_marker_ident(entity_ident)
    facts = [
        f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
        f"[{ident} :entity {entity_ident}]",
        f"[{ident} :status :provisional]",
    ]
    _transact(db, "[" + " ".join(facts) + "]", commit_ts_iso, index_con=index_con)


def _lineage_confirm(db: Any, entity_ident: str, index_con: Optional[Any] = None) -> None:
    """Retract the :type/lineage-marker companion entity's facts for
    entity_ident if present; no-op if absent, so callers (2c) can call this
    unconditionally without checking first.
    """
    if not _lineage_is_provisional(db, entity_ident):
        return
    ident = _lineage_marker_ident(entity_ident)
    facts = [
        f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
        f"[{ident} :entity {entity_ident}]",
        f"[{ident} :status :provisional]",
    ]
    _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)


def _lineage_is_provisional(db: Any, entity_ident: str) -> bool:
    """True iff a :type/lineage-marker companion entity currently exists for
    entity_ident."""
    ident = _lineage_marker_ident(entity_ident)
    raw = _db_execute(db, f"(query [:find ?e :where [{ident} :entity ?e]])")
    return bool(json.loads(raw).get("results", []))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestLineageProvisionalMarker -v`
Expected: PASS (all 5 tests).

Also run the full existing suite to confirm no regression:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS (no new failures beyond the pre-existing, unrelated ones — see phase 1's plan notes on the ~120 missing-grammar-package failures).

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add provisional lineage marker: _lineage_mark_provisional/_confirm/_is_provisional

Companion-entity marker (:type/lineage-marker, deliberately unregistered
in MINIGRAF_SCHEMA) for #222 phase 2a -- not an attribute on the tracked
code entity itself, since module/function/class/variable/field are
schema-audited and :lineage-status would be silently retracted by a
routine minigraf_audit run. Idempotent by query-before-write, writes via
internal _transact/_retract only. No caller yet (2c wires this in)."
```

---

### Task 2: `lineage-confirmed-through` watermark

**Files:**
- Modify: `mcp_server.py` — insert immediately after Task 1's functions.
- Test: `tests/test_mcp_server.py` — new `TestLineageConfirmedThroughWatermark` class.

**Interfaces:**
- Consumes: `_db_execute`, `_transact`, `_retract`, `_edn_escape` (all pre-existing).
- Produces:
  - `_LINEAGE_CONFIRMED_THROUGH_IDENT: str = ":ingestion/lineage-confirmed-through"`
  - `_lineage_confirmed_through_query(db) -> Optional[str]`
  - `_lineage_confirmed_through_update(db, commit_hash: str, commit_ts_iso: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

Add this test class to `tests/test_mcp_server.py`:

```python
class TestLineageConfirmedThroughWatermark:
    def test_unset_reads_as_none(self, real_db):
        import mcp_server
        assert mcp_server._lineage_confirmed_through_query(real_db) is None

    def test_update_then_query_round_trip(self, real_db):
        import mcp_server
        db = real_db
        mcp_server._lineage_confirmed_through_update(db, "h1", "2026-01-01T00:00:00Z")
        assert mcp_server._lineage_confirmed_through_query(db) == "h1"

        mcp_server._lineage_confirmed_through_update(db, "h2", "2026-01-02T00:00:00Z")
        assert mcp_server._lineage_confirmed_through_query(db) == "h2"

    def test_update_does_not_duplicate_hash_fact(self, real_db):
        import mcp_server
        db = real_db
        mcp_server._lineage_confirmed_through_update(db, "h1", "2026-01-01T00:00:00Z")
        mcp_server._lineage_confirmed_through_update(db, "h2", "2026-01-02T00:00:00Z")

        ident = mcp_server._LINEAGE_CONFIRMED_THROUGH_IDENT
        raw = mcp_server._db_execute(db, f"(query [:find (count ?h) :where [{ident} :hash ?h]])")
        assert json.loads(raw)["results"] == [[1]]

    def test_entity_carries_expected_constants_and_survives_audit(self, real_db):
        import mcp_server
        db = real_db
        mcp_server._lineage_confirmed_through_update(db, "h1", "2026-01-01T00:00:00Z")

        ident = mcp_server._LINEAGE_CONFIRMED_THROUGH_IDENT
        raw = mcp_server._db_execute(
            db, f"(query [:find ?a ?v :where [{ident} ?a ?v]])"
        )
        attrs = dict(json.loads(raw)["results"])
        assert attrs[":entity-type"] == ":type/ingestion"
        assert attrs[":ident"] == ident
        assert isinstance(attrs[":description"], str) and attrs[":description"]

        result = mcp_server.handle_minigraf_audit()
        assert result["retracted"] == 0
        assert mcp_server._lineage_confirmed_through_query(db) == "h1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestLineageConfirmedThroughWatermark -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_lineage_confirmed_through_query'`.

- [ ] **Step 3: Add the functions to `mcp_server.py`**

Insert immediately after Task 1's `_lineage_is_provisional`:

```python
_LINEAGE_CONFIRMED_THROUGH_IDENT = ":ingestion/lineage-confirmed-through"


def _lineage_confirmed_through_query(db: Any) -> Optional[str]:
    """Return the hash of the last commit through which lineage is fully
    confirmed, or None if nothing has been confirmed yet."""
    raw = _db_execute(
        db, f"(query [:find ?h :where [{_LINEAGE_CONFIRMED_THROUGH_IDENT} :hash ?h]])"
    )
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _lineage_confirmed_through_update(
    db: Any, commit_hash: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the last lineage-confirmed commit hash, mirroring
    _watermark_update's retract-only-if-changed pattern. Uses :type/
    ingestion -- the SAME registered/audited entity type :ingestion/
    watermark already uses -- so this entity carries the same required
    :description constant _watermark_update's own entity does.
    """
    current_raw = _db_execute(
        db, f"(query [:find ?a ?v :where [{_LINEAGE_CONFIRMED_THROUGH_IDENT} ?a ?v]])"
    )
    current: Dict[str, str] = dict(json.loads(current_raw).get("results", []))

    def _edn(attr: str, value: str) -> str:
        return value if attr == ":entity-type" else f'"{_edn_escape(value)}"'

    constants = {
        ":entity-type": ":type/ingestion",
        ":ident": _LINEAGE_CONFIRMED_THROUGH_IDENT,
        ":description": "lineage confirmed-through watermark",
    }

    to_retract: List[str] = []
    to_transact: List[str] = []
    for attr, value in constants.items():
        if current.get(attr) == value:
            continue
        if attr in current:
            to_retract.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} {attr} {_edn(attr, current[attr])}]")
        to_transact.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} {attr} {_edn(attr, value)}]")

    if ":hash" in current:
        to_retract.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} :hash {_edn(':hash', current[':hash'])}]")
    to_transact.append(f"[{_LINEAGE_CONFIRMED_THROUGH_IDENT} :hash {_edn(':hash', commit_hash)}]")

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestLineageConfirmedThroughWatermark -v`
Expected: PASS (all 4 tests).

Also run the full existing suite: `python -m pytest tests/test_mcp_server.py -x -q` — expect no new failures.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add lineage-confirmed-through watermark (:ingestion/lineage-confirmed-through)

Mirrors _watermark_update's exact retract-only-if-changed pattern.
Unlike :type/lineage-marker/:type/candidate-diff, this entity uses the
registered/audited :type/ingestion type (same as :ingestion/watermark),
so it carries the same :entity-type/:ident/:description constants and
must survive minigraf_audit by genuinely conforming, not by being
invisible to it. No caller yet."
```

---

### Task 3: Migration catch-up (`_lineage_confirmed_through_migrate`)

**Files:**
- Modify: `mcp_server.py` — add the new function immediately after Task 2's functions; add one call inside the existing `_frontier_load` (currently lines 4950-4978).
- Test: `tests/test_mcp_server.py` — new `TestLineageConfirmedThroughMigration` class.

**Interfaces:**
- Consumes: `_lineage_confirmed_through_query`, `_lineage_confirmed_through_update` (Task 2); `_frontier_read_bounds`, `_FRONTIER_LOW_IDENT` (phase 1, already merged).
- Produces: `_lineage_confirmed_through_migrate(db, run_ts_iso: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

Add this test class to `tests/test_mcp_server.py`:

```python
class TestLineageConfirmedThroughMigration:
    def test_fresh_migration_seeds_from_watermark(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]
        mcp_server._watermark_update(db, "h1", "2026-01-01T00:00:00Z", "seed watermark")

        mcp_server._frontier_load(db, linearization, "2026-01-02T00:00:00Z")

        assert mcp_server._lineage_confirmed_through_query(db) == "h1"

    def test_already_migrated_graph_still_gets_watermark_seeded(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]
        mcp_server._watermark_update(db, "h1", "2026-01-01T00:00:00Z", "seed watermark")

        # Simulate an earlier Phase-1-only run: frontier-low gets created,
        # but lineage-confirmed-through (new in Phase 2a) doesn't exist yet.
        mcp_server._frontier_seed_from_watermark(db, linearization, "2026-01-01T00:00:01Z")
        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT) is not None
        assert mcp_server._lineage_confirmed_through_query(db) is None

        mcp_server._frontier_load(db, linearization, "2026-01-02T00:00:00Z")

        assert mcp_server._lineage_confirmed_through_query(db) == "h1"

    def test_repeated_load_does_not_duplicate_same_value(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]
        mcp_server._watermark_update(db, "h1", "2026-01-01T00:00:00Z", "seed watermark")

        mcp_server._frontier_load(db, linearization, "2026-01-02T00:00:00Z")
        mcp_server._frontier_load(db, linearization, "2026-01-03T00:00:00Z")

        ident = mcp_server._LINEAGE_CONFIRMED_THROUGH_IDENT
        raw = mcp_server._db_execute(db, f"(query [:find (count ?h) :where [{ident} :hash ?h]])")
        assert json.loads(raw)["results"] == [[1]]

    def test_does_not_clobber_a_value_advanced_past_migration_boundary(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]
        mcp_server._watermark_update(db, "h1", "2026-01-01T00:00:00Z", "seed watermark")
        mcp_server._frontier_load(db, linearization, "2026-01-02T00:00:00Z")
        assert mcp_server._lineage_confirmed_through_query(db) == "h1"

        # Simulate phase 2c's real sweep having advanced past the original
        # migration boundary.
        mcp_server._lineage_confirmed_through_update(db, "h2", "2026-01-03T00:00:00Z")

        mcp_server._frontier_load(db, linearization, "2026-01-04T00:00:00Z")

        assert mcp_server._lineage_confirmed_through_query(db) == "h2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestLineageConfirmedThroughMigration -v`
Expected: FAIL — `test_fresh_migration_seeds_from_watermark` and the others fail with `assert None == "h1"` (or similar), since `_frontier_load` doesn't call any lineage-confirmed-through seeding yet.

- [ ] **Step 3: Add `_lineage_confirmed_through_migrate` and wire it into `_frontier_load`**

Insert immediately after Task 2's `_lineage_confirmed_through_update`:

```python
def _lineage_confirmed_through_migrate(
    db: Any, run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """One-time catch-up: if :ingestion/frontier-low exists (this graph has
    an authoritative region, whether freshly migrated by
    _frontier_seed_from_watermark just now or already established by an
    earlier Phase-1-only run) but :ingestion/lineage-confirmed-through is
    unset, seed the watermark from frontier-low's *current* :hi-hash --
    that whole region was ingested by the original single-stream
    forward-only authoritative walk, so it is already fully
    lineage-confirmed. No-op if frontier-low doesn't exist yet, or
    lineage-confirmed-through is already set (so later phases' own sweep
    updates are never clobbered back to a stale value).
    """
    if _lineage_confirmed_through_query(db) is not None:
        return
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is None:
        return
    _, hi_hash = low_bounds
    _lineage_confirmed_through_update(db, hi_hash, run_ts_iso, index_con=index_con)
```

Modify the existing `_frontier_load` (currently lines 4950-4978) — change:

```python
    if (
        _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is None
        and _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT) is None
    ):
        _frontier_seed_from_watermark(db, linearization, run_ts_iso, index_con=index_con)

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
```

to:

```python
    if (
        _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is None
        and _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT) is None
    ):
        _frontier_seed_from_watermark(db, linearization, run_ts_iso, index_con=index_con)
    _lineage_confirmed_through_migrate(db, run_ts_iso, index_con=index_con)

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
```

(This is the only edit to code merged in phase 1 that this whole sub-phase makes — everything else is new functions.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestLineageConfirmedThroughMigration -v`
Expected: PASS (all 4 tests).

Also run the full existing suite, and specifically phase 1's own `TestFrontierLoad` class (since `_frontier_load` was modified) to confirm no regression:

Run: `python -m pytest tests/test_mcp_server.py::TestFrontierLoad tests/test_mcp_server.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _lineage_confirmed_through_migrate, wire into _frontier_load

Seeds the new lineage-confirmed-through watermark from frontier-low's
current :hi-hash whenever it's unset -- covers both a fresh migration
and a graph that already ran Phase 1 standalone before Phase 2a landed
(this project's own actual situation), since the catch-up isn't tied to
_frontier_seed_from_watermark's narrower neither-interval-exists guard.
Guarded so it never clobbers a value phase 2c's real sweep has since
advanced past the migration boundary."
```

---

### Task 4: Candidate-diff persistence (`_candidate_diff_persist` / `_candidate_diff_read` / `_candidate_diff_clear`)

**Files:**
- Modify: `mcp_server.py` — insert immediately after Task 3's `_lineage_confirmed_through_migrate`.
- Test: `tests/test_mcp_server.py` — new `TestCandidateDiff` class.

**Interfaces:**
- Consumes: `_db_execute`, `_transact`, `_retract`, `_edn_escape` (all pre-existing).
- Produces:
  - `_candidate_diff_ident(commit_hash: str, entity_ident: str) -> str`
  - `_candidate_diff_persist(db, commit_hash: str, entity_ident: str, body_hash: str, commit_ts_iso: str, index_con=None) -> None`
  - `_candidate_diff_read(db, commit_hash: str, entity_ident: str) -> Optional[str]`
  - `_candidate_diff_clear(db, commit_hash: str, entity_ident: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

Add this test class to `tests/test_mcp_server.py`:

```python
class TestCandidateDiff:
    def test_read_absent_record_returns_none(self, real_db):
        import mcp_server
        assert mcp_server._candidate_diff_read(real_db, "a" * 40, ":function/foo") is None

    def test_persist_then_read_round_trip(self, real_db):
        import mcp_server
        db = real_db
        commit_hash = "a" * 40
        entity_ident = ":function/src-auth-py-login"

        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash1", "2026-01-01T00:00:00Z")

        assert mcp_server._candidate_diff_read(db, commit_hash, entity_ident) == "hash1"
        # A different (commit, entity) pair must not resolve to this record.
        assert mcp_server._candidate_diff_read(db, "b" * 40, entity_ident) is None
        assert mcp_server._candidate_diff_read(db, commit_hash, ":function/other") is None

    def test_persist_same_hash_twice_does_not_duplicate(self, real_db):
        import mcp_server
        db = real_db
        commit_hash = "a" * 40
        entity_ident = ":function/src-auth-py-login"

        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash1", "2026-01-01T00:00:00Z")
        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash1", "2026-01-01T00:00:01Z")

        ident = mcp_server._candidate_diff_ident(commit_hash, entity_ident)
        raw = mcp_server._db_execute(db, f"(query [:find (count ?h) :where [{ident} :body-hash ?h]])")
        assert json.loads(raw)["results"] == [[1]]

    def test_persist_different_hash_updates_without_duplicating(self, real_db):
        import mcp_server
        db = real_db
        commit_hash = "a" * 40
        entity_ident = ":function/src-auth-py-login"

        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash1", "2026-01-01T00:00:00Z")
        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash2", "2026-01-01T00:00:01Z")

        assert mcp_server._candidate_diff_read(db, commit_hash, entity_ident) == "hash2"
        ident = mcp_server._candidate_diff_ident(commit_hash, entity_ident)
        raw = mcp_server._db_execute(db, f"(query [:find (count ?h) :where [{ident} :body-hash ?h]])")
        assert json.loads(raw)["results"] == [[1]]

    def test_clear_removes_the_record(self, real_db):
        import mcp_server
        db = real_db
        commit_hash = "a" * 40
        entity_ident = ":function/src-auth-py-login"

        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash1", "2026-01-01T00:00:00Z")
        mcp_server._candidate_diff_clear(db, commit_hash, entity_ident)

        assert mcp_server._candidate_diff_read(db, commit_hash, entity_ident) is None
        ident = mcp_server._candidate_diff_ident(commit_hash, entity_ident)
        raw = mcp_server._db_execute(db, f"(query [:find (count ?e) :where [{ident} :entity ?e]])")
        assert json.loads(raw)["results"] == [[0]]

    def test_clear_absent_record_is_a_noop(self, real_db):
        import mcp_server
        db = real_db
        # Must not raise.
        mcp_server._candidate_diff_clear(db, "a" * 40, ":function/never-persisted")

    def test_candidate_diff_survives_audit(self, real_db):
        import mcp_server
        db = real_db
        commit_hash = "a" * 40
        entity_ident = ":function/src-auth-py-login"

        mcp_server._candidate_diff_persist(db, commit_hash, entity_ident, "hash1", "2026-01-01T00:00:00Z")
        result = mcp_server.handle_minigraf_audit()

        assert result["retracted"] == 0
        assert mcp_server._candidate_diff_read(db, commit_hash, entity_ident) == "hash1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCandidateDiff -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_candidate_diff_read'`.

- [ ] **Step 3: Add the functions to `mcp_server.py`**

Insert immediately after Task 3's `_lineage_confirmed_through_migrate`:

```python
def _candidate_diff_ident(commit_hash: str, entity_ident: str) -> str:
    """Deterministic ident for a candidate-diff record. Not a public schema
    type -- see the #222 phase 2a design spec's "Schema/audit status of new
    entity types" section.
    """
    return f":candidate/{commit_hash[:12]}-{entity_ident.lstrip(':').replace('/', '-')}"


def _candidate_diff_persist(
    db: Any,
    commit_hash: str,
    entity_ident: str,
    body_hash: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Mint/update one candidate-diff record for (commit_hash, entity_ident).
    Query-before-write -- no-ops if the persisted body_hash already
    matches, retracts+reasserts only the :body-hash if it genuinely
    changed (the other attributes are all derived from commit_hash/
    entity_ident, which never change for a given record's ident). Uses
    internal _transact/_retract directly, never handle_minigraf_transact:
    :type/candidate-diff is deliberately unregistered in MINIGRAF_SCHEMA.
    """
    ident = _candidate_diff_ident(commit_hash, entity_ident)
    existing = _candidate_diff_read(db, commit_hash, entity_ident)
    if existing == body_hash:
        return
    if existing is None:
        commit_ident = f":commit/{commit_hash[:12]}"
        facts = [
            f"[{ident} :entity-type :type/candidate-diff]",
            f"[{ident} :commit {commit_ident}]",
            f"[{ident} :entity {entity_ident}]",
            f'[{ident} :body-hash "{_edn_escape(body_hash)}"]',
        ]
        _transact(db, "[" + " ".join(facts) + "]", commit_ts_iso, index_con=index_con)
    else:
        _retract(db, f'[[{ident} :body-hash "{_edn_escape(existing)}"]]', index_con=index_con)
        _transact(
            db, f'[[{ident} :body-hash "{_edn_escape(body_hash)}"]]', commit_ts_iso, index_con=index_con
        )


def _candidate_diff_read(db: Any, commit_hash: str, entity_ident: str) -> Optional[str]:
    """Return the persisted body_hash for (commit_hash, entity_ident), or
    None if no candidate record exists for that pair."""
    ident = _candidate_diff_ident(commit_hash, entity_ident)
    raw = _db_execute(db, f"(query [:find ?h :where [{ident} :body-hash ?h]])")
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _candidate_diff_clear(
    db: Any, commit_hash: str, entity_ident: str, index_con: Optional[Any] = None
) -> None:
    """Retract the candidate record once consumed (2c calls this after
    confirming/rejecting), so these scratch facts don't accumulate
    unbounded across a full ingest. No-op if no record exists.
    """
    existing = _candidate_diff_read(db, commit_hash, entity_ident)
    if existing is None:
        return
    ident = _candidate_diff_ident(commit_hash, entity_ident)
    commit_ident = f":commit/{commit_hash[:12]}"
    facts = [
        f"[{ident} :entity-type :type/candidate-diff]",
        f"[{ident} :commit {commit_ident}]",
        f"[{ident} :entity {entity_ident}]",
        f'[{ident} :body-hash "{_edn_escape(existing)}"]',
    ]
    _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestCandidateDiff -v`
Expected: PASS (all 7 tests).

Also run the full existing suite to confirm no regression:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add candidate-diff persistence: _candidate_diff_persist/_read/_clear

One :type/candidate-diff entity per (commit, entity) pair (deliberately
unregistered in MINIGRAF_SCHEMA, same status as :type/lineage-marker),
holding the entity's #221 normalized-body-hash -- lets Stream 1's future
correction sweep (2c) confirm/reject a candidate via hash comparison
without re-invoking git-show + tree-sitter. Idempotent by
query-before-write, writes via internal _transact/_retract only. No
caller yet (2b writes, 2c reads/clears)."
```

## Self-Review Notes

- **Spec coverage:** Provisional marker (Task 1) — covered, including audit safety. `lineage-confirmed-through` watermark (Task 2) — covered, including the registered-type constants/audit test the final review round asked for. Migration catch-up covering both fresh and already-migrated graphs, plus non-clobbering of an advanced value (Task 3) — covered, directly testing the exact scenarios Revision notes #3/#5/#8 called out. Candidate-diff persistence with idempotency and audit safety (Task 4) — covered. The internal-`_transact`-only constraint (Revision note #7) is stated in every new function's docstring and Global Constraints, and no task routes through `handle_minigraf_transact`/`handle_minigraf_retract`.
- **Type consistency:** `entity_ident`/`commit_hash`/`commit_ts_iso`/`index_con` parameter names and types are used identically across all 4 tasks. `_lineage_marker_ident`/`_candidate_diff_ident` naming and signature style match `_frontier_read_bounds`'s existing `Optional[Tuple[str, str]]`-returning-helper pattern from phase 1.
- **No placeholders:** every step has complete, runnable code — no TBD/TODO markers, no "similar to Task N" shorthand.
