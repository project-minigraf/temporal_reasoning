# #uuid-Tagged Entity Boost Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix #194 — a `#uuid`-tagged update to a `:decision/`/`:preference/`/`:constraint/`/`:dependency/` entity indexes under the raw UUID instead of the entity's keyword form, so it never gets the fact index's memory-fact BM25 boost.

**Architecture:** Two coordinated changes in `mcp_server.py`. (1) `_transact`/`_retract`'s default fact-index deriver resolves any `#uuid`-tagged entity to its stored `:ident` fact before indexing, falling back to the raw UUID when no `:ident` exists (mirrors `handle_minigraf_audit`'s existing resolution pattern). (2) `handle_minigraf_transact` auto-writes a self-referencing `:ident` fact, query-gated to avoid duplicate history rows, the first time a keyword entity under a memory prefix is created — closing the gap for the common case where an ordinary decision/preference entity never got an explicit `:ident` in the first place.

**Tech Stack:** Same as the rest of the codebase — stdlib only, real `minigraf`/`sqlite3` backends in tests, no new dependencies.

## Global Constraints

- Design source of truth: `docs/superpowers/specs/2026-07-22-uuid-ident-boost-resolution-design.md` — re-read it if any task here seems to contradict it; the spec wins.
- **Always use `.venv/bin/pytest` / `.venv/bin/python3` explicitly, never bare `python3`/`pytest`.**
- Current clean baseline: `.venv/bin/pytest tests/test_mcp_server.py -q` → 688 passed, 0 failed.
- Testing convention (`docs/testing-conventions.md`): every test uses a real `MiniGrafDb` via the `real_db` fixture — never mocked.
- Confirmed empirically (see spec): minigraf treats an identical `(entity, attribute, value, valid_from)` tuple as idempotent, but a **different** `valid_from` for the same triple creates a genuine second live row, not a no-op. Any code that writes `:ident` automatically must be gated on an existence check, never unconditional.
- Every commit follows this repo's established convention: small, TDD (RED before implementation), real backend, frequent commits.

---

## File Structure

- **Modify:** `mcp_server.py` — new `_query_ident` and `_resolved_facts_triples` helpers; `_transact`/`_retract` switch their default fact-index deriver; new `_ensure_memory_idents` helper; `handle_minigraf_transact` calls it after a successful write.
- **Modify:** `tests/test_mcp_server.py` — new `TestQueryIdent` and `TestUuidIdentBoostResolution` classes.

---

### Task 1: Resolve `#uuid`-tagged entities to `:ident` at index-write time

**Files:**
- Modify: `mcp_server.py` — add `_query_ident` and `_resolved_facts_triples` after `_parse_facts_block` (currently ends at mcp_server.py:3355, right before `_index_write` at mcp_server.py:3358); modify `_transact` (mcp_server.py:3443-3476) and `_retract` (mcp_server.py:3479-3498) to use `_resolved_facts_triples` as their default deriver.
- Test: `tests/test_mcp_server.py` — new `TestQueryIdent` class (insert after `TestMinigrafRetract`, which currently ends at line 995, before `class TestParseTxResult:` at line 997) and new tests inside a new `TestUuidIdentBoostResolution` class in the same location.

**Interfaces:**
- Produces: `_query_ident(db: Any, entity_ref: str) -> Optional[str]` — `entity_ref` is either a bare keyword literal (e.g. `":decision/x"`) or a `#uuid "..."`-tagged literal string (e.g. `'#uuid "b14d54ed-..."'`). Returns the stored `:ident` value, or `None` if absent or the query fails. Never raises.
- Produces: `_resolved_facts_triples(facts_str: str, db: Any) -> List[Tuple[str, str, str]]` — drop-in replacement for `_parse_facts_block(facts_str)` as `_transact`/`_retract`'s default deriver; identical output except any triple whose entity came from a `#uuid`/`#inst` tag is resolved to its `:ident` when one exists.
- Consumes: existing `_parse_facts_block`, `_db_execute`, `_parse_query_result` (all already defined above `_transact` in mcp_server.py).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`, right after `class TestMinigrafRetract:` ends (after line 995, before `class TestParseTxResult:`):

```python
class TestQueryIdent:
    def test_returns_ident_when_present(self, real_db):
        import mcp_server
        real_db.execute('(transact {} [[:decision/x :ident ":decision/x"]])')
        assert mcp_server._query_ident(real_db, ":decision/x") == ":decision/x"

    def test_returns_none_when_absent(self, real_db):
        import mcp_server
        real_db.execute('(transact {} [[:decision/x :description "hello"]])')
        assert mcp_server._query_ident(real_db, ":decision/x") is None

    def test_returns_none_on_malformed_uuid_ref(self, real_db):
        """A non-UUID string wrapped in #uuid "..." fails minigraf's EDN
        parse with a MiniGrafError -- _query_ident must catch that and
        return None, not raise (confirmed empirically: minigraf raises
        MiniGrafError.Other(msg='Invalid UUID') for this input)."""
        import mcp_server
        assert mcp_server._query_ident(real_db, '#uuid "not-a-uuid"') is None

    def test_resolves_via_uuid_tagged_ref(self, real_db):
        import mcp_server
        real_db.execute('(transact {} [[:decision/x :ident ":decision/x"]])')
        queried = json.loads(real_db.execute(
            '(query [:find ?e :where [?e :ident ":decision/x"]])'
        ))
        entity_uuid = queried["results"][0][0]
        assert mcp_server._query_ident(real_db, f'#uuid "{entity_uuid}"') == ":decision/x"


class TestUuidIdentBoostResolution:
    """#194: a #uuid-tagged transact/retract against an entity that already
    has an :ident fact must resolve to the keyword form for fact-index
    purposes, so it stays eligible for fact_index._MEMORY_PREFIXES' BM25
    boost."""

    def test_transact_uuid_tagged_entity_resolves_to_ident_for_boost(self, real_db):
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/x :description "hello"] [:decision/x :ident ":decision/x"]]',
            reason="test",
        )
        queried = mcp_server.handle_minigraf_query(
            '[:find ?e :where [?e :description "hello"]]'
        )
        entity_uuid = queried["results"][0][0]

        result = mcp_server.handle_minigraf_transact(
            f'[[#uuid "{entity_uuid}" :status "reviewed"]]', reason="test2"
        )
        assert result["ok"] is True

        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(
            index_path, "reviewed", top_n=10, boost=2.0, historical_discount=1.0
        )
        matching = [r for r in results if r[1] == ":status" and r[2] == "reviewed"]
        assert matching
        assert matching[0][0] == ":decision/x"

    def test_transact_uuid_tagged_entity_without_ident_still_falls_back_to_uuid(self, real_db):
        """No :ident fact anywhere for this entity -- must behave exactly
        as before this change (index under the raw UUID), not raise or
        drop the fact."""
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:service/auth :description "auth service"]]', reason="test"
        )
        queried = mcp_server.handle_minigraf_query(
            '[:find ?e :where [?e :description "auth service"]]'
        )
        entity_uuid = queried["results"][0][0]

        result = mcp_server.handle_minigraf_transact(
            f'[[#uuid "{entity_uuid}" :status "reviewed"]]', reason="test2"
        )
        assert result["ok"] is True

        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(
            index_path, "reviewed", top_n=10, boost=2.0, historical_discount=1.0
        )
        matching = [r for r in results if r[1] == ":status" and r[2] == "reviewed"]
        assert matching
        assert matching[0][0] == entity_uuid

    def test_retract_uuid_tagged_entity_removes_resolved_ident_row(self, real_db):
        """Retract-side symmetry: the row _transact indexed under the
        resolved keyword ident must actually be the row _retract deletes --
        otherwise the delete targets a nonexistent raw-UUID row and leaves
        a stale entry behind."""
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/x :description "hello"] [:decision/x :ident ":decision/x"]]',
            reason="test",
        )
        queried = mcp_server.handle_minigraf_query(
            '[:find ?e :where [?e :description "hello"]]'
        )
        entity_uuid = queried["results"][0][0]
        mcp_server.handle_minigraf_transact(
            f'[[#uuid "{entity_uuid}" :status "reviewed"]]', reason="test2"
        )

        result = mcp_server.handle_minigraf_retract(
            f'[[#uuid "{entity_uuid}" :status "reviewed"]]', reason="cleanup"
        )
        assert result["ok"] is True

        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(
            index_path, "reviewed", top_n=10, boost=2.0, historical_discount=1.0
        )
        assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k "TestQueryIdent or TestUuidIdentBoostResolution"`

Expected: `TestQueryIdent` tests FAIL with `AttributeError: module 'mcp_server' has no attribute '_query_ident'`. `test_transact_uuid_tagged_entity_resolves_to_ident_for_boost` and the retract symmetry test FAIL on `assert matching[0][0] == ":decision/x"` / `assert results == []` (currently indexes/deletes under the raw UUID instead). `test_transact_uuid_tagged_entity_without_ident_still_falls_back_to_uuid` should already PASS (it documents current behavior, unchanged by this task) — confirm it passes both before and after this task's implementation step.

- [ ] **Step 3: Implement `_query_ident` and `_resolved_facts_triples`**

In `mcp_server.py`, insert immediately after `_parse_facts_block` ends (after line 3355, before `def _index_write` at line 3358):

```python
def _query_ident(db: Any, entity_ref: str) -> Optional[str]:
    """Look up the :ident fact for entity_ref -- a bare keyword literal
    (e.g. ':decision/x') or a #uuid "..."-tagged literal -- returning the
    stored keyword ident string, or None if no :ident fact exists or the
    query fails. Never raises: a caller resolving an entity for fact-index
    purposes must fall back to the raw entity_ref on any failure, not break
    a write that has already committed by the time this runs (#194).
    """
    try:
        raw = _db_execute(db, f'(query [:find ?v :where [{entity_ref} :ident ?v]])')
        result = _parse_query_result(raw)
        if result.get("ok"):
            for row in result.get("results", []):
                if row and isinstance(row[0], str):
                    return row[0]
    except Exception as e:
        print(f"[fact_index] ident lookup failed for {entity_ref}: {e}", file=sys.stderr)
    return None


def _resolved_facts_triples(facts_str: str, db: Any) -> List[Tuple[str, str, str]]:
    """Parse facts_str via _parse_facts_block, then resolve any #uuid/#inst
    -tagged entity (identified post-unwrap by not starting with ':') to its
    stored keyword :ident via _query_ident, falling back to the raw
    UUID/timestamp text when no :ident fact exists (#194) -- without this,
    a fact transacted against a #uuid-tagged reference to an existing
    memory-category entity indexes under the opaque UUID and never gets
    fact_index._MEMORY_PREFIXES' BM25 boost, even though it's a fact about
    that same entity. Resolutions are cached per call so a UUID referenced
    by multiple triples in one transact/retract only queries once.

    This is the default deriver _transact/_retract use when the caller
    doesn't pass index_triples explicitly. A caller that already has a
    resolved ident more cheaply available (see handle_minigraf_audit)
    should keep passing index_triples to skip these queries entirely.
    """
    triples = _parse_facts_block(facts_str)
    cache: Dict[str, Optional[str]] = {}
    resolved = []
    for entity, attribute, value in triples:
        if not entity.startswith(":"):
            if entity not in cache:
                cache[entity] = _query_ident(db, f'#uuid "{entity}"')
            entity = cache[entity] or entity
        resolved.append((entity, attribute, value))
    return resolved
```

Then modify `_transact` (mcp_server.py:3473) — change:

```python
    triples_3 = index_triples if index_triples is not None else _parse_facts_block(datalog_facts)
```

to:

```python
    triples_3 = index_triples if index_triples is not None else _resolved_facts_triples(datalog_facts, db)
```

And `_retract` (mcp_server.py:3495) — change the identical line the same way.

Update `_transact`'s docstring (mcp_server.py:3459-3467), replacing:

```
    index_triples defaults to auto-parsing datalog_facts via
    _parse_facts_block() (which returns 3-tuples (entity, attribute, value)
    -- the window is appended here, not inside that function, since
    _parse_facts_block has no way to know valid_from/valid_to); pass
    index_triples explicitly when the Datalog string's own entity reference
    isn't the searchable identity (e.g. handle_minigraf_audit's #uuid-tagged
    retracts, whose index_triples must use the resolved keyword ident
    instead) -- in that case pass 3-tuples too, the window is still appended
    here uniformly.
```

with:

```
    index_triples defaults to auto-deriving via _resolved_facts_triples()
    (which returns 3-tuples (entity, attribute, value), resolving any
    #uuid-tagged entity to its stored :ident when one exists, #194 -- the
    window is appended here, not inside that function, since it has no way
    to know valid_from/valid_to); pass index_triples explicitly when a
    caller already has a resolved keyword ident more cheaply available than
    a fresh query would provide (e.g. handle_minigraf_audit, which already
    fetched the entity's attributes including :ident) -- in that case pass
    3-tuples too, the window is still appended here uniformly.
```

Similarly update `_retract`'s docstring (mcp_server.py:3485-3492), replacing the sentence `"index_triples overrides auto-derivation when the Datalog entity reference isn't the searchable identity"` with `"index_triples overrides auto-derivation (_resolved_facts_triples, which resolves #uuid-tagged entities to their :ident when available, #194) when a caller already has a resolved ident more cheaply available"`.

Also update `_parse_facts_block`'s own docstring (mcp_server.py:3338-3345), which currently ends with:

```
    #uuid/#inst-tagged entity references and
    values are also captured, with the tag stripped and the raw UUID/
    timestamp text kept as the indexed entity/value (#177) -- this is not
    a keyword ident, so entity identity in the index can fragment across a
    keyword-tagged create and a later #uuid-tagged update of the same
    graph entity; pass index_triples explicitly (see handle_minigraf_audit)
    when a resolved keyword ident is available and identity consistency
    matters more than a mechanical capture.
```

Change the last two sentences to:

```
    #uuid/#inst-tagged entity references and
    values are also captured, with the tag stripped and the raw UUID/
    timestamp text kept as the indexed entity/value (#177) -- this is not
    a keyword ident, so a caller wanting identity-resolved output should use
    _resolved_facts_triples() instead, which wraps this function and
    resolves #uuid-tagged entities to their stored :ident when one exists
    (#194).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k "TestQueryIdent or TestUuidIdentBoostResolution"`

Expected: all PASS.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `.venv/bin/pytest tests/test_mcp_server.py -q`

Expected: 688 + (new test count) passed, 0 failed. Pay particular attention to `TestMinigrafAudit` (mcp_server.py's `handle_minigraf_audit` still passes `index_triples` explicitly and must be unaffected) and the existing `#177` tests (`test_transact_uuid_tagged_entity_is_indexed` at line 809, `test_retract_uuid_tagged_entity_removes_from_fact_index` at line 922) — both use entities with no `:ident` fact, so they must still pass unchanged (fallback-to-raw-UUID path).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Fix #194 (part 1/2): resolve #uuid-tagged entities to :ident at index-write time

_transact/_retract's default fact-index deriver now resolves a #uuid-tagged
entity reference to its stored :ident fact when one exists, mirroring
handle_minigraf_audit's existing pattern, so a later update against a
already-idented decision/preference/constraint/dependency entity stays
eligible for the memory-fact BM25 boost instead of indexing under the
opaque UUID.
EOF
)"
```

---

### Task 2: Auto-write `:ident` for memory-prefixed entities on create

**Files:**
- Modify: `mcp_server.py` — add `_ensure_memory_idents` after `_retract` (mcp_server.py:3479-3498) and before `def handle_minigraf_transact` (mcp_server.py:3501); modify `handle_minigraf_transact` (mcp_server.py:3501-3528) to call it.
- Test: `tests/test_mcp_server.py` — add tests to `TestUuidIdentBoostResolution` (created in Task 1).

**Interfaces:**
- Consumes: `_query_ident(db, entity_ref)` and `_parse_facts_block(facts_str)` from Task 1 / existing code. `fact_index._MEMORY_PREFIXES` (already defined at fact_index.py:19, already imported via `import fact_index` at mcp_server.py:31). `_edn_escape` (mcp_server.py:4300). `_transact(db, datalog_facts, valid_from, ...)` (existing signature, unchanged).
- Produces: `_ensure_memory_idents(db: Any, facts_str: str, valid_from: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `TestUuidIdentBoostResolution` (from Task 1), inside `tests/test_mcp_server.py`:

```python
    def test_transact_auto_writes_ident_for_memory_prefix_entity(self, real_db):
        """#194: a plain keyword-created decision entity must get a
        self-referencing :ident fact so a later #uuid-tagged update against
        it can resolve back to the keyword form for the BM25 boost."""
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/cache :description "use Redis"]]', reason="test"
        )
        queried = mcp_server.handle_minigraf_query(
            '[:find ?v :where [:decision/cache :ident ?v]]'
        )
        assert queried["results"] == [[":decision/cache"]]

    def test_transact_does_not_auto_ident_non_memory_prefix_entity(self, real_db):
        """Scoped to fact_index._MEMORY_PREFIXES only -- an ordinary entity
        like :service/auth must not get an auto-written :ident."""
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:service/auth :description "auth service"]]', reason="test"
        )
        queried = mcp_server.handle_minigraf_query(
            '[:find ?v :where [:service/auth :ident ?v]]'
        )
        assert queried["results"] == []

    def test_transact_ident_write_skipped_when_already_present(self, real_db):
        """The existence-check query must actually gate the write -- once
        :decision/cache has an :ident fact, a later transact against it
        must not issue a second (transact ...) for :ident at all (avoids
        the duplicate-history-row behavior confirmed in the design doc)."""
        import mcp_server
        mcp_server.handle_minigraf_transact(
            '[[:decision/cache :description "use Redis"]]', reason="test"
        )
        with execute_spy() as calls:
            mcp_server.handle_minigraf_transact(
                '[[:decision/cache :status "reviewed"]]', reason="test2"
            )
        ident_transacts = [
            c for c in calls if c.startswith("(transact") and ":ident" in c
        ]
        assert not ident_transacts

    def test_transact_auto_ident_skipped_when_caller_already_wrote_one(self, real_db):
        """If the caller's own facts block already sets :ident for this
        entity, _ensure_memory_idents must not write a second, redundant
        one at a different valid_from."""
        import mcp_server
        with execute_spy() as calls:
            mcp_server.handle_minigraf_transact(
                '[[:decision/cache :description "use Redis"] '
                '[:decision/cache :ident ":decision/cache"]]',
                reason="test",
            )
        ident_transacts = [c for c in calls if ":ident" in c]
        assert len(ident_transacts) == 1

    def test_uuid_tagged_update_against_auto_idented_decision_gets_boost(self, real_db):
        """End-to-end #194 regression: an ordinary minigraf_transact-created
        decision (no explicit :ident from the caller) must still resolve a
        later #uuid-tagged update back to its keyword form, thanks to this
        task's auto-:ident write plus Task 1's resolution at index time."""
        import mcp_server
        import fact_index
        mcp_server.handle_minigraf_transact(
            '[[:decision/cache :description "use Redis for caching"]]', reason="test"
        )
        queried = mcp_server.handle_minigraf_query(
            '[:find ?e :where [?e :description "use Redis for caching"]]'
        )
        entity_uuid = queried["results"][0][0]

        result = mcp_server.handle_minigraf_transact(
            f'[[#uuid "{entity_uuid}" :status "reviewed"]]', reason="test2"
        )
        assert result["ok"] is True

        index_path = fact_index.index_path_for(mcp_server._graph_path)
        results = fact_index.query_facts(
            index_path, "reviewed", top_n=10, boost=2.0, historical_discount=1.0
        )
        matching = [r for r in results if r[1] == ":status" and r[2] == "reviewed"]
        assert matching
        assert matching[0][0] == ":decision/cache"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k "TestUuidIdentBoostResolution"`

Expected: the four new tests in this task FAIL —
`test_transact_auto_writes_ident_for_memory_prefix_entity` on `assert queried["results"] == [[":decision/cache"]]` (currently `[]`); `test_transact_does_not_auto_ident_non_memory_prefix_entity` and the two `_skipped_when_*` tests currently PASS vacuously (nothing writes `:ident` at all yet) — confirm this, then move on since they'll still hold once implemented; `test_uuid_tagged_update_against_auto_idented_decision_gets_boost` FAILS on `assert matching[0][0] == ":decision/cache"` (currently resolves to nothing / falls back to the raw UUID, since no `:ident` exists yet). All tests from Task 1 must still PASS.

- [ ] **Step 3: Implement `_ensure_memory_idents` and wire it into `handle_minigraf_transact`**

In `mcp_server.py`, insert after `_retract` ends (after line 3498, before `def handle_minigraf_transact` at line 3501):

```python
def _ensure_memory_idents(db: Any, facts_str: str, valid_from: str) -> None:
    """After a successful transact, write a self-referencing :ident fact for
    any keyword entity in facts_str whose ident string starts with a
    fact_index._MEMORY_PREFIXES category (:decision/, :preference/,
    :constraint/, :dependency/) and doesn't already have one (#194) --
    without this, an ordinary minigraf_transact-created decision/
    preference/constraint/dependency entity has no way to resolve a later
    #uuid-tagged reference back to its keyword form for the memory-fact
    BM25 boost (see _resolved_facts_triples).

    Query-gated, not unconditional: re-transacting an identical fact at a
    different valid_from creates a new bi-temporal history row every time
    (confirmed empirically; consistent with #156's finding documented in
    _checkpoint_after_write) -- writing :ident on every call would bloat
    history. Never raises: the caller's actual write has already committed
    by the time this runs, and a failure here must not affect that result.
    """
    triples = _parse_facts_block(facts_str)
    already_idented = {e for e, a, v in triples if a == ":ident"}
    candidates = {
        e for e, a, v in triples
        if e.startswith(":") and e.startswith(fact_index._MEMORY_PREFIXES)
    } - already_idented
    for entity in sorted(candidates):
        if _query_ident(db, entity) is not None:
            continue
        try:
            _transact(db, f'[{entity} :ident "{_edn_escape(entity)}"]', valid_from)
        except Exception as e:
            print(f"[fact_index] auto-ident write failed for {entity}: {e}", file=sys.stderr)
```

Then modify `handle_minigraf_transact` (mcp_server.py:3501-3528) — change:

```python
def handle_minigraf_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required.

    :valid-at is set to the current UTC ms timestamp so every agent-initiated
    write has a recorded valid time, enabling correct bi-temporal queries.
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    # Schema validation — closed-world enforcement on parseable string-valued triples.
    # Only string-valued triples are schema-validated. Keyword-valued triples
    # (e.g. relationship edges like [:service/auth :calls :component/jwt]) are
    # not covered by MINIGRAF_SCHEMA and pass through unvalidated by design.
    parsed = _parse_transact_facts(facts)
    if parsed:
        violations = _validate_facts(parsed)
        if violations:
            return {"ok": False, "error": f"schema violations: {'; '.join(violations)}"}
    _refresh_if_stale()
    db = get_db()
    try:
        raw = _transact(db, facts, _now_utc_ms())
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}
    result = _parse_tx_result(raw)
    if result["ok"]:
        result["reason"] = reason
    _checkpoint_after_write(db, "minigraf_transact", result)
    return result
```

to:

```python
def handle_minigraf_transact(facts: str, reason: str) -> Dict[str, Any]:
    """Transact facts into the graph. reason is required.

    :valid-at is set to the current UTC ms timestamp so every agent-initiated
    write has a recorded valid time, enabling correct bi-temporal queries.
    On success, also ensures any memory-category entity (fact_index.
    _MEMORY_PREFIXES) created by this call has a resolvable :ident fact --
    see _ensure_memory_idents (#194).
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    # Schema validation — closed-world enforcement on parseable string-valued triples.
    # Only string-valued triples are schema-validated. Keyword-valued triples
    # (e.g. relationship edges like [:service/auth :calls :component/jwt]) are
    # not covered by MINIGRAF_SCHEMA and pass through unvalidated by design.
    parsed = _parse_transact_facts(facts)
    if parsed:
        violations = _validate_facts(parsed)
        if violations:
            return {"ok": False, "error": f"schema violations: {'; '.join(violations)}"}
    _refresh_if_stale()
    db = get_db()
    valid_from = _now_utc_ms()
    try:
        raw = _transact(db, facts, valid_from)
    except MiniGrafError as e:
        return {"ok": False, "error": str(e)}
    result = _parse_tx_result(raw)
    if result["ok"]:
        result["reason"] = reason
        _ensure_memory_idents(db, facts, valid_from)
    _checkpoint_after_write(db, "minigraf_transact", result)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -v -k "TestUuidIdentBoostResolution or TestQueryIdent"`

Expected: all PASS.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `.venv/bin/pytest tests/test_mcp_server.py -q`

Expected: all passed, 0 failed. Pay particular attention to `TestMinigrafTransact` (the extra `_ensure_memory_idents` call must not change any existing transact's reported `result`, only add facts as a side effect) and `TestMinigrafAudit` (entities audited/retracted there use `:type/*` prefixes, not `:decision/`/`:preference/`/`:constraint/`/`:dependency/`, so they must fall outside `_ensure_memory_idents`'s scope — but audit doesn't call `handle_minigraf_transact` at all, so this should be a non-issue; confirm by reading the diff of any changed assertion counts, not just pass/fail).

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Fix #194 (part 2/2): auto-write :ident for memory-prefixed entities on create

handle_minigraf_transact now writes a self-referencing :ident fact the
first time a keyword entity under :decision/:preference/:constraint/
:dependency/ is created (query-gated to avoid duplicate bi-temporal history
rows on repeat writes), closing the gap where an ordinary decision/
preference entity had no way to resolve a later #uuid-tagged update back to
its keyword form for the memory-fact BM25 boost.
EOF
)"
```

---

## Post-plan verification checklist

- [ ] `grep -n "minigraf_transact\|:ident" SKILL.md` — confirm whether SKILL.md documents what a `minigraf_transact` call writes beyond the caller's own facts block; update it if this change makes an existing description inaccurate (e.g. "transact writes exactly the facts you pass" would now be false for memory-prefixed entities).
- [ ] `.venv/bin/pytest tests/ -q` (full suite, not just `test_mcp_server.py`) — confirm no cross-file regression (e.g. `tests/test_fact_index.py`, ingestion tests).
- [ ] Confirm `handle_minigraf_audit`'s existing explicit `index_triples` usage (mcp_server.py, inside `handle_minigraf_audit`) is byte-for-byte unaffected by Task 1's default-deriver change — it never hits the new `_resolved_facts_triples` path since it always passes `index_triples` explicitly.
- [ ] Close #194 referencing both commits once merged.
