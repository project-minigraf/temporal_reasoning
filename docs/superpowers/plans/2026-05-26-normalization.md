# Phase 4 — Entity Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add slug canonicalization, closed-world schema validation, schema-aware extraction prompts, alias datom support, and a `vulcan_audit` MCP tool to enforce data quality in graph memory.

**Architecture:** All changes are in `mcp_server.py`. Pure helper functions (`_canonical_ident`, `_keyword_uuid`, `_validate_facts`, `_query_canonical_entities`) are added first, then wired into existing extraction paths and the new `handle_vulcan_audit` tool. SKILL.md gets an Entity Resolution section. TDD throughout — write the failing test, then the implementation.

**Tech Stack:** Python 3.9+, `minigraf` Python binding (`MiniGrafDb`), `uuid` stdlib, `re` stdlib, `pytest`

---

## File Map

| File | Changes |
|------|---------|
| `mcp_server.py` | Add `_canonical_ident`, `_keyword_uuid`, `VULCAN_SCHEMA`, `_validate_facts`, `_query_canonical_entities`; update `heuristic_extract`, `_transact_extracted_facts`, `handle_vulcan_transact`, `_LLM_EXTRACTION_PROMPT`, `_AGENT_SAMPLING_PROMPT`; add `handle_vulcan_audit`, register as 7th tool in `_TOOLS` and `call_tool` |
| `SKILL.md` | Add Entity Resolution section after existing Entity Idents section |
| `tests/test_mcp_server.py` | Add 7 new test classes; update tool count assertion from 6 to 7 |

---

### Task 1: `_canonical_ident` and `_keyword_uuid`

**Files:**
- Modify: `mcp_server.py` (after the `_STOP_WORDS` set, before `heuristic_extract`)
- Modify: `tests/test_mcp_server.py` (append at end of file)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestCanonicalIdent:
    def test_lowercases_value(self):
        import mcp_server
        assert mcp_server._canonical_ident("decision", "Redis") == ":decision/redis"

    def test_replaces_spaces_with_hyphens(self):
        import mcp_server
        assert mcp_server._canonical_ident("preference", "use postgres") == ":preference/use-postgres"

    def test_replaces_underscores(self):
        import mcp_server
        assert mcp_server._canonical_ident("constraint", "must_be_stateless") == ":constraint/must-be-stateless"

    def test_replaces_dots(self):
        import mcp_server
        assert mcp_server._canonical_ident("dependency", "pydantic.v2") == ":dependency/pydantic-v2"

    def test_collapses_consecutive_hyphens(self):
        import mcp_server
        assert mcp_server._canonical_ident("decision", "use  Redis") == ":decision/use-redis"

    def test_strips_leading_trailing_hyphens(self):
        import mcp_server
        assert mcp_server._canonical_ident("decision", " redis ") == ":decision/redis"


class TestKeywordUuid:
    def test_same_keyword_same_uuid(self):
        import mcp_server
        a = mcp_server._keyword_uuid(":decision/redis")
        b = mcp_server._keyword_uuid(":decision/redis")
        assert a == b

    def test_different_keywords_different_uuids(self):
        import mcp_server
        a = mcp_server._keyword_uuid(":decision/redis")
        b = mcp_server._keyword_uuid(":decision/postgres")
        assert a != b

    def test_returns_string(self):
        import mcp_server
        result = mcp_server._keyword_uuid(":decision/redis")
        assert isinstance(result, str)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mcp_server.py::TestCanonicalIdent tests/test_mcp_server.py::TestKeywordUuid -v
```

Expected: `ERROR` — `AttributeError: module 'mcp_server' has no attribute '_canonical_ident'`

- [ ] **Step 3: Add `_canonical_ident` and `_keyword_uuid` to `mcp_server.py`**

Add after the `_STOP_WORDS` set (around line 362, before `heuristic_extract`):

```python
import uuid as _uuid_mod


def _canonical_ident(entity_type: str, value: str) -> str:
    """Slug-canonicalize a value into a Minigraf keyword ident.

    Lowercases, replaces any character outside [a-z0-9-] with a hyphen,
    collapses consecutive hyphens, strips leading/trailing hyphens.
    Ported from _to_kw() in minigraf-examples LlamaIndex integration.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f":{entity_type}/{slug}"


def _keyword_uuid(keyword: str) -> str:
    """Derive a stable UUID from a Minigraf keyword string.

    Same keyword always produces the same UUID — used for entity resolution
    without string-matching. Ported from keyword_entity_id() in
    minigraf-examples minigraf-algorithms crate.
    """
    return str(_uuid_mod.uuid5(_uuid_mod.NAMESPACE_OID, keyword))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestCanonicalIdent tests/test_mcp_server.py::TestKeywordUuid -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): add _canonical_ident and _keyword_uuid"
```

---

### Task 2: `VULCAN_SCHEMA` and `_validate_facts`

**Files:**
- Modify: `mcp_server.py` (after `_keyword_uuid`, before `heuristic_extract`)
- Modify: `tests/test_mcp_server.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestValidateFacts:
    def test_valid_fact_no_violations(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"}]
        assert mcp_server._validate_facts(facts) == []

    def test_missing_required_attribute(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":rationale", "value": "fast"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert ":description" in violations[0]

    def test_unknown_entity_type_rejected(self):
        import mcp_server
        facts = [{"entity": ":service/auth", "entity_type": "service",
                  "attribute": ":description", "value": "auth service"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert "service" in violations[0]

    def test_unknown_attribute_rejected(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"},
                 {"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":unknown-attr", "value": "foo"}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1
        assert ":unknown-attr" in violations[0]

    def test_wrong_value_type_rejected(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": 42}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1

    def test_valid_alias_passes(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"},
                 {"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":alias", "value": "Redis-based cache"}]
        assert mcp_server._validate_facts(facts) == []

    def test_alias_wrong_type_rejected(self):
        import mcp_server
        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"},
                 {"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":alias", "value": 99}]
        violations = mcp_server._validate_facts(facts)
        assert len(violations) == 1

    def test_all_four_entity_types_accepted(self):
        import mcp_server
        for etype in ("decision", "preference", "constraint", "dependency"):
            facts = [{"entity": f":{etype}/x", "entity_type": etype,
                      "attribute": ":description", "value": "test"}]
            assert mcp_server._validate_facts(facts) == [], f"Failed for {etype}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mcp_server.py::TestValidateFacts -v
```

Expected: `ERROR` — `AttributeError: module 'mcp_server' has no attribute '_validate_facts'`

- [ ] **Step 3: Add `VULCAN_SCHEMA` and `_validate_facts` to `mcp_server.py`**

Add after `_keyword_uuid` (before `heuristic_extract`):

```python
VULCAN_SCHEMA: Dict[str, Dict[str, Dict[str, type]]] = {
    "decision": {
        "required": {":description": str},
        "optional": {":rationale": str, ":date": str, ":alias": str},
    },
    "preference": {
        "required": {":description": str},
        "optional": {":rationale": str, ":alias": str},
    },
    "constraint": {
        "required": {":description": str},
        "optional": {":rationale": str, ":alias": str},
    },
    "dependency": {
        "required": {":description": str},
        "optional": {":rationale": str, ":alias": str},
    },
}


def _validate_facts(facts: List[Dict[str, Any]]) -> List[str]:
    """Validate proposed facts against VULCAN_SCHEMA. Returns violation strings.

    Closed-world: unknown entity types and unknown attributes are both violations.
    Pure function — no DB access. Mirrors Schema.validate() from minigraf-schema.
    """
    violations: List[str] = []

    # Group facts by entity to check required attributes across all facts for one entity.
    entity_attrs: Dict[str, Dict[str, Any]] = {}
    entity_types: Dict[str, str] = {}
    for fact in facts:
        entity = fact.get("entity", "")
        entity_type = fact.get("entity_type", "")
        attribute = fact.get("attribute", "")
        value = fact.get("value")
        entity_attrs.setdefault(entity, {})[attribute] = value
        if entity_type:
            entity_types[entity] = entity_type

    for entity, attrs in entity_attrs.items():
        entity_type = entity_types.get(entity, "")

        # Closed-world: unknown entity type is a violation.
        if entity_type not in VULCAN_SCHEMA:
            violations.append(
                f"entity '{entity}' has unknown type '{entity_type}' — "
                f"allowed: {list(VULCAN_SCHEMA)}"
            )
            continue

        schema = VULCAN_SCHEMA[entity_type]
        required = schema["required"]
        optional = schema["optional"]
        allowed = set(required) | set(optional)

        # Check required attributes are present with correct type.
        for attr, expected_type in required.items():
            if attr not in attrs:
                violations.append(
                    f"entity '{entity}' missing required attribute '{attr}'"
                )
            elif not isinstance(attrs[attr], expected_type):
                violations.append(
                    f"entity '{entity}' attribute '{attr}' has wrong type "
                    f"(expected {expected_type.__name__}, got {type(attrs[attr]).__name__})"
                )

        # Check optional attributes, if present, have correct type.
        for attr, value in attrs.items():
            if attr in optional and not isinstance(value, optional[attr]):
                violations.append(
                    f"entity '{entity}' attribute '{attr}' has wrong type "
                    f"(expected {optional[attr].__name__}, got {type(value).__name__})"
                )

        # Closed-world: unknown attributes are violations.
        for attr in attrs:
            if attr not in allowed:
                violations.append(
                    f"entity '{entity}' has unknown attribute '{attr}' — "
                    f"allowed: {sorted(allowed)}"
                )

    return violations
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestValidateFacts -v
```

Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): add VULCAN_SCHEMA and _validate_facts"
```

---

### Task 3: Wire Canonicalization into `heuristic_extract`

**Files:**
- Modify: `mcp_server.py` (line 381 — `entity_ident` construction in `heuristic_extract`)
- Modify: `tests/test_mcp_server.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
class TestHeuristicNormalization:
    def test_ident_uses_canonical_slug(self):
        import mcp_server
        facts = mcp_server.heuristic_extract("We'll use Redis for caching.")
        assert any(f["entity"] == ":decision/redis" for f in facts)

    def test_ident_not_underscore_form(self):
        import mcp_server
        # Old form was `:decision/redis` via `.replace('-', '_')` but that's a no-op
        # for "redis". Test a multi-word value to verify hyphens not underscores.
        facts = mcp_server.heuristic_extract("We'll use postgres-db for storage.")
        matching = [f for f in facts if "postgres" in f["entity"]]
        assert matching, "No fact with postgres found"
        assert "_" not in matching[0]["entity"], f"Underscore found in {matching[0]['entity']}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_mcp_server.py::TestHeuristicNormalization -v
```

Expected: FAIL — the current code uses `.replace('-', '_')` which produces a different form for multi-word values.

- [ ] **Step 3: Update `heuristic_extract` in `mcp_server.py`**

Find line 381 (inside `heuristic_extract`):
```python
            entity_ident = f":{entity_type}/{value.lower().replace('-', '_')}"
```

Replace with:
```python
            entity_ident = _canonical_ident(entity_type, value)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestHeuristicNormalization tests/test_mcp_server.py::TestHeuristicExtract -v
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): use _canonical_ident in heuristic_extract"
```

---

### Task 4: Schema Validation in `_transact_extracted_facts`

**Files:**
- Modify: `mcp_server.py` (`_transact_extracted_facts` function)
- Modify: `tests/test_mcp_server.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestTransactExtractedFactsSchema:
    def test_invalid_entity_type_is_skipped(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "1"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        facts = [{"entity": ":service/auth", "entity_type": "service",
                  "attribute": ":description", "value": "auth service"}]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 0
        # No transact call should have been made
        transact_calls = [c for c in db_instance.execute.call_args_list
                          if "transact" in str(c)]
        assert len(transact_calls) == 0

    def test_valid_fact_is_stored(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "2"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        facts = [{"entity": ":decision/redis", "entity_type": "decision",
                  "attribute": ":description", "value": "use Redis"}]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 1

    def test_mixed_batch_stores_only_valid(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "3"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db_instance.execute.reset_mock()

        facts = [
            {"entity": ":decision/redis", "entity_type": "decision",
             "attribute": ":description", "value": "use Redis"},
            {"entity": ":service/auth", "entity_type": "service",
             "attribute": ":description", "value": "auth service"},
        ]
        stored = mcp_server._transact_extracted_facts(facts)

        assert stored == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mcp_server.py::TestTransactExtractedFactsSchema -v
```

Expected: FAIL — invalid facts currently pass through unchecked.

- [ ] **Step 3: Add validation to `_transact_extracted_facts`**

Inside `_transact_extracted_facts`, add a validation check at the top of the `for fact in facts:` loop, before the `try:` block:

```python
    for fact in facts:
        entity = fact["entity"]
        entity_type = fact.get("entity_type", "")
        attribute = fact["attribute"]
        value = fact["value"]

        # Schema validation — closed-world: skip invalid facts.
        violations = _validate_facts([fact])
        if violations:
            continue

        now_z = _now_utc_ms()
        try:
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestTransactExtractedFactsSchema tests/test_mcp_server.py::TestMemoryFinalizeTurnHeuristic -v
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): validate facts before transact in extraction pipeline"
```

---

### Task 5: Schema Validation in `handle_vulcan_transact`

**Files:**
- Modify: `mcp_server.py` (`handle_vulcan_transact` and new `_parse_transact_facts`)
- Modify: `tests/test_mcp_server.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestParseTransactFacts:
    def test_parses_single_triple(self):
        import mcp_server
        facts = mcp_server._parse_transact_facts(
            '[[:decision/redis :description "use Redis"]]'
        )
        assert len(facts) == 1
        assert facts[0]["entity"] == ":decision/redis"
        assert facts[0]["attribute"] == ":description"
        assert facts[0]["value"] == "use Redis"
        assert facts[0]["entity_type"] == "decision"

    def test_parses_multiple_triples(self):
        import mcp_server
        facts = mcp_server._parse_transact_facts(
            '[[:decision/redis :description "use Redis"] '
            '[:decision/redis :rationale "fast"]]'
        )
        assert len(facts) == 2

    def test_returns_empty_for_non_string_values(self):
        import mcp_server
        # keyword values like :type/decision are not captured (no quotes)
        facts = mcp_server._parse_transact_facts(
            "[[:decision/redis :entity-type :type/decision]]"
        )
        assert facts == []


class TestVulcanTransactSchema:
    def test_rejects_unknown_entity_type(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_transact(
            '[[:service/auth :description "auth service"]]',
            reason="test"
        )

        assert result["ok"] is False
        assert "schema" in result["error"].lower() or "violation" in result["error"].lower()

    def test_accepts_valid_fact(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"tx": "5"})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_transact(
            '[[:decision/redis :description "use Redis"]]',
            reason="test"
        )

        assert result["ok"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mcp_server.py::TestParseTransactFacts tests/test_mcp_server.py::TestVulcanTransactSchema -v
```

Expected: `ERROR` on `_parse_transact_facts`; `handle_vulcan_transact` test passes without validation (wrong).

- [ ] **Step 3: Add `_parse_transact_facts` and update `handle_vulcan_transact`**

Add `_parse_transact_facts` after `_validate_facts`:

```python
def _parse_transact_facts(facts_str: str) -> List[Dict[str, Any]]:
    """Parse a Datalog transact string into fact dicts for schema validation.

    Only captures string-valued triples (quoted values). Keyword values
    like :type/decision are skipped — they are internal type tags, not
    user-authored facts subject to schema validation.
    """
    pattern = r'\[(\S+)\s+(\S+)\s+"([^"]+)"\]'
    result = []
    for match in re.finditer(pattern, facts_str):
        entity, attribute, value = match.groups()
        entity_type = entity.split("/")[0].lstrip(":") if "/" in entity else ""
        result.append({
            "entity": entity,
            "entity_type": entity_type,
            "attribute": attribute,
            "value": value,
        })
    return result
```

In `handle_vulcan_transact`, add validation after the `reason` check:

```python
def handle_vulcan_transact(facts: str, reason: str) -> Dict[str, Any]:
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}

    # Schema validation — closed-world enforcement on parseable string-valued triples.
    parsed = _parse_transact_facts(facts)
    if parsed:
        violations = _validate_facts(parsed)
        if violations:
            return {"ok": False, "error": f"schema violations: {'; '.join(violations)}"}

    _refresh_if_stale()
    # ... rest of function unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestParseTransactFacts tests/test_mcp_server.py::TestVulcanTransactSchema tests/test_mcp_server.py::TestVulcanTransact -v
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): schema validation in handle_vulcan_transact"
```

---

### Task 6: Schema-Aware Extraction Prompts

**Files:**
- Modify: `mcp_server.py` (add `_query_canonical_entities`, update `_LLM_EXTRACTION_PROMPT`, `_AGENT_SAMPLING_PROMPT`, `_llm_extract_and_transact`, `_agent_extract_and_transact`)
- Modify: `tests/test_mcp_server.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestQueryCanonicalEntities:
    def test_returns_empty_string_when_no_entities(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server._query_canonical_entities()
        assert result == ""

    def test_formats_entities_as_lines(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/redis", "use Redis"]]
        })
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server._query_canonical_entities()
        assert ":decision/redis" in result
        assert "use Redis" in result

    def test_caps_at_50_entities(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({
            "results": [[f":decision/item-{i}", f"item {i}"] for i in range(60)]
        })
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server._query_canonical_entities()
        assert result.count(":decision/") == 50

    def test_injected_into_llm_prompt(self, mock_minigraf_db, tmp_path, monkeypatch):
        mock_class, db_instance = mock_minigraf_db
        monkeypatch.setenv("VULCAN_LLM_MODEL", "claude-haiku-4-5-20251001")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        db_instance.execute.return_value = json.dumps({
            "results": [[":decision/redis", "use Redis"]]
        })
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        captured_prompt = {}
        def fake_call_llm(model, prompt):
            captured_prompt["prompt"] = prompt
            return "[]"

        with patch("mcp_server._call_llm", side_effect=fake_call_llm):
            mcp_server._llm_extract_and_transact("User: test\nAgent: ok")

        assert ":decision/redis" in captured_prompt.get("prompt", "")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mcp_server.py::TestQueryCanonicalEntities -v
```

Expected: `ERROR` — `_query_canonical_entities` not defined.

- [ ] **Step 3: Add `_query_canonical_entities` to `mcp_server.py`**

Add after `_parse_transact_facts`:

```python
def _query_canonical_entities() -> str:
    """Query existing canonical entity idents for schema-aware prompt injection.

    Returns a formatted string listing up to 50 entity idents and their
    descriptions. Returns empty string if the graph has no entities — in
    that case the caller omits the section from the prompt entirely.
    """
    try:
        result = handle_vulcan_query("[:find ?e ?desc :where [?e :description ?desc]]")
        rows = result.get("results", [])
    except Exception:
        return ""
    if not rows:
        return ""
    rows = rows[:50]
    lines = [f"  {ident} — {desc}" for ident, desc in rows]
    return "\n".join(lines)
```

- [ ] **Step 4: Update `_LLM_EXTRACTION_PROMPT` and `_AGENT_SAMPLING_PROMPT`**

In `_LLM_EXTRACTION_PROMPT`, add a new section after the opening instructions and before "Conversation:". The prompt currently ends with `Conversation:\n{conversation}`. Change it to:

```python
_LLM_EXTRACTION_PROMPT = """You are a memory extraction assistant for a bi-temporal graph database. Review the conversation below and identify any decisions, preferences, constraints, or dependencies that should be stored in long-term memory.

Return ONLY a Datalog transact expression — a list of triples in this exact format:
[[:entity/ident :attribute "value"]
 [:entity/ident :attribute "value"]]

If nothing worth storing was found, return an empty list: []

Allowed entity type prefixes: :decision/ :preference/ :constraint/ :dependency/
Canonical ident form: lowercase, hyphens only — :decision/redis not :decision/Redis_cache.

{canonical_entities_section}

Use these attributes: :description (required), :rationale (optional), :date (optional), :alias (optional).
No other attributes are valid.

IMPORTANT — entity resolution: if a reference matches an existing canonical ident or alias above,
reuse that exact ident. Only mint a new ident if the entity is genuinely new.

IMPORTANT — bi-temporality: this database is bi-temporal. Facts have both a transaction time
(when they were recorded) and a valid time (when they were true in the world). When the conversation
mentions that something was decided or true at a specific past date, note that date alongside the
fact so the caller can set :valid-at accordingly. Wrap such facts in a comment line:
; valid-at: 2024-03-15
[[:entity/ident :attribute "value"]]

For point-in-time historical queries, always use :as-of N and :valid-at "date" TOGETHER —
using only one gives a partial view.

Conversation:
{conversation}"""
```

In `_llm_extract_and_transact`, build the canonical entities section before formatting the prompt:

```python
def _llm_extract_and_transact(conversation_delta: str) -> Dict[str, Any]:
    try:
        model = os.environ.get("VULCAN_LLM_MODEL", "claude-haiku-4-5-20251001")
        canonical = _query_canonical_entities()
        if canonical:
            canonical_entities_section = (
                "Existing canonical entities (reuse these idents — do not invent synonyms):\n"
                + canonical
            )
        else:
            canonical_entities_section = ""
        prompt = _LLM_EXTRACTION_PROMPT.format(
            conversation=conversation_delta,
            canonical_entities_section=canonical_entities_section,
        )
        raw_facts = _call_llm(model, prompt).strip()
        # ... rest of function unchanged
```

Similarly update `_AGENT_SAMPLING_PROMPT` to include `{canonical_entities_section}` and update `_agent_extract_and_transact` to format it the same way:

```python
_AGENT_SAMPLING_PROMPT = """Review this conversation turn and output ONLY a Datalog transact expression for any decisions, preferences, constraints, or dependencies worth storing in long-term memory.

Allowed entity type prefixes: :decision/ :preference/ :constraint/ :dependency/
Canonical ident form: lowercase, hyphens only — :decision/redis not :decision/Redis_cache.

{canonical_entities_section}

Use these attributes: :description (required), :rationale (optional), :date (optional), :alias (optional).
No other attributes are valid. If an entity matches an existing ident or alias, reuse it exactly.

Format:
[[:entity/ident :attribute "value"]]

Return [] if nothing is worth storing.

{conversation}"""
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestQueryCanonicalEntities tests/test_mcp_server.py::TestLlmStrategy tests/test_mcp_server.py::TestAgentStrategy -v
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): schema-aware extraction prompts with canonical entity injection"
```

---

### Task 7: `handle_vulcan_audit` — 7th MCP Tool

**Files:**
- Modify: `mcp_server.py` (add `handle_vulcan_audit`, add to `_TOOLS`, add to `call_tool`)
- Modify: `tests/test_mcp_server.py` (update tool count + append audit tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestVulcanAudit:
    def test_clean_db_returns_zero_retracted(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        # No entities of any known type
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_audit()

        assert result["ok"] is True
        assert result["retracted"] == 0
        assert result["violations"] == []

    def test_entity_missing_required_attr_is_retracted(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        # First call: entity type query returns one entity
        # Second call: attribute query returns only :rationale (missing :description)
        db_instance.execute.side_effect = [
            json.dumps({"results": [[":decision/redis"]]}),  # type query
            json.dumps({"results": [[":rationale", "fast"]]}),  # attr query
            json.dumps({"tx": "10"}),  # retract call
        ] + [json.dumps({"results": []})] * 10  # remaining type queries

        result = mcp_server.handle_vulcan_audit()

        assert result["ok"] is True
        assert result["retracted"] == 1
        assert len(result["violations"]) == 1

    def test_as_of_reports_violations_without_retracting(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        db_instance.execute.side_effect = [
            json.dumps({"results": [[":decision/redis"]]}),
            json.dumps({"results": [[":rationale", "fast"]]}),
        ] + [json.dumps({"results": []})] * 10

        result = mcp_server.handle_vulcan_audit(as_of=5)

        assert result["ok"] is True
        assert result["retracted"] == 0  # read-only when as_of provided
        assert len(result["violations"]) == 1

    def test_result_shape(self, mock_minigraf_db, tmp_path):
        mock_class, db_instance = mock_minigraf_db
        db_instance.execute.return_value = json.dumps({"results": []})
        import mcp_server
        mcp_server.open_db(str(tmp_path / "t.graph"))

        result = mcp_server.handle_vulcan_audit()

        assert "ok" in result
        assert "audited" in result
        assert "retracted" in result
        assert "violations" in result
        assert isinstance(result["violations"], list)
```

Also find and update the existing tool count test:

```python
# In TestMcpToolWiring.test_list_tools_returns_six_tools:
# Change 6 → 7 and add "vulcan_audit" to the names set
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mcp_server.py::TestVulcanAudit tests/test_mcp_server.py::TestMcpToolWiring -v
```

Expected: `TestVulcanAudit` errors on missing `handle_vulcan_audit`; tool count test fails (6 != 7).

- [ ] **Step 3: Add `handle_vulcan_audit` to `mcp_server.py`**

Add after `handle_vulcan_report_issue`:

```python
def handle_vulcan_audit(as_of: Optional[int] = None) -> Dict[str, Any]:
    """Audit graph entities against VULCAN_SCHEMA.

    Current state (as_of=None): validates all entities and retracts violators.
    Point-in-time (as_of=N): reports violations only — no retractions, since
    modifying past state is not meaningful in the bi-temporal model.

    Ported from Schema.audit_as_of() in minigraf-examples minigraf-schema crate.
    """
    _refresh_if_stale()
    db = get_db()
    audited = 0
    retracted = 0
    all_violations: List[Dict[str, Any]] = []

    as_of_clause = f":as-of {as_of} " if as_of is not None else ""

    for entity_type, schema in VULCAN_SCHEMA.items():
        # Step 1: find all entities of this type.
        type_query = (
            f"[:find ?e {as_of_clause}"
            f":where [?e :entity-type :type/{entity_type}]]"
        )
        try:
            type_result = handle_vulcan_query(type_query)
            entity_rows = type_result.get("results", [])
        except Exception:
            continue

        for row in entity_rows:
            if not row:
                continue
            entity_ident = row[0]
            audited += 1

            # Step 2: fetch all attributes for this entity.
            attr_query = (
                f"[:find ?a ?v {as_of_clause}"
                f":where [{entity_ident} ?a ?v]]"
            )
            try:
                attr_result = handle_vulcan_query(attr_query)
                attr_rows = attr_result.get("results", [])
            except Exception:
                continue

            # Build fact dict for validation.
            fact = {"entity": entity_ident, "entity_type": entity_type}
            for attr_row in attr_rows:
                if len(attr_row) == 2:
                    fact[attr_row[0]] = attr_row[1]

            # Reconstruct as list of per-attribute fact dicts for _validate_facts.
            attr_facts = [
                {"entity": entity_ident, "entity_type": entity_type,
                 "attribute": attr, "value": val}
                for attr, val in fact.items()
                if attr not in ("entity", "entity_type")
            ]

            violations = _validate_facts(attr_facts)
            if violations:
                for v in violations:
                    all_violations.append({"entity": entity_ident, "detail": v})

                if as_of is None:
                    # Retract the invalid entity.
                    reason = f"vulcan_audit: schema violation — {'; '.join(violations)}"
                    try:
                        db.execute(f"(retract [[{entity_ident} :entity-type :type/{entity_type}]])")
                        db.checkpoint()
                        _update_mtime()
                        retracted += 1
                    except MiniGrafError:
                        pass

    return {
        "ok": True,
        "audited": audited,
        "retracted": retracted,
        "violations": all_violations,
    }
```

- [ ] **Step 4: Register `vulcan_audit` in `_TOOLS` and `call_tool`**

Add to `_TOOLS` list (after `memory_finalize_turn`):

```python
    Tool(
        name="vulcan_audit",
        description=(
            "Audit all graph entities against the built-in schema. "
            "Retracts entities with schema violations (missing required attributes, "
            "unknown types, unknown attributes). Run periodically or after heavy write sessions. "
            "Pass as_of (transaction number) for a read-only point-in-time audit without retractions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "integer",
                    "description": "Optional transaction number for point-in-time audit (read-only, no retractions)",
                },
            },
            "required": [],
        },
    ),
```

Add to `call_tool` (before the final `raise ValueError`):

```python
    if name == "vulcan_audit":
        as_of = arguments.get("as_of")
        result = handle_vulcan_audit(as_of=as_of)
        return [TextContent(type="text", text=json.dumps(result))]
```

- [ ] **Step 5: Update tool count test**

In `TestMcpToolWiring.test_list_tools_returns_six_tools`, update:
```python
    assert len(tools) == 7
    names = {t.name for t in tools}
    assert names == {
        "vulcan_query", "vulcan_transact", "vulcan_retract",
        "vulcan_report_issue", "memory_prepare_turn", "memory_finalize_turn",
        "vulcan_audit",
    }
```

Also rename the test method to `test_list_tools_returns_seven_tools`.

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_mcp_server.py::TestVulcanAudit tests/test_mcp_server.py::TestMcpToolWiring -v
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(normalization): add vulcan_audit as 7th MCP tool"
```

---

### Task 8: Update SKILL.md with Entity Resolution Section

**Files:**
- Modify: `SKILL.md` (add Entity Resolution section after existing Entity Idents section)

- [ ] **Step 1: Locate the insertion point**

Find the "Entity Idents and Attribute Names" section in `SKILL.md` (around line 59). The new section goes immediately after it.

- [ ] **Step 2: Add the Entity Resolution section**

After the existing Entity Idents section, add:

```markdown
## Entity Resolution

Before storing a new entity, always check for existing canonical idents and aliases:

```datalog
[:find ?e ?desc :where [?e :description ?desc]]
[:find ?e ?a :where [?e :alias ?a]]
```

If a reference matches an existing ident or alias, reuse that exact ident.
Only mint a new ident if the entity is genuinely new.

Canonical ident form: lowercase, hyphens only — `:decision/redis` not `:decision/Redis_cache`.

Allowed entity types: `:decision/`, `:preference/`, `:constraint/`, `:dependency/`
Required attribute on all types: `:description`
Optional attributes: `:rationale`, `:date`, `:alias`

Run `vulcan_audit` periodically or after a session with heavy writes to detect and retract any schema violations.
```

- [ ] **Step 3: Verify SKILL.md renders correctly**

```bash
grep -n "Entity Resolution" SKILL.md
```

Expected: one match with the section heading.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md
git commit -m "docs(skill): add Entity Resolution section for normalization guidance"
```

---

### Task 9: Full Test Run and Push

- [ ] **Step 1: Run the full test suite**

```bash
pytest -v
```

Expected: all tests pass (previously 59; now 59 + new tests from Tasks 1–7).

- [ ] **Step 2: Push to master**

```bash
git push origin master
```

- [ ] **Step 3: Update temporal memory**

Use `vulcan_transact` to record Phase 4 complete:

```
vulcan_transact(
  facts='[[:phase/normalization :phase/status "complete"] [:phase/normalization :decision/date "2026-05-26"]]',
  reason="Phase 4 entity normalization implemented: _canonical_ident, VULCAN_SCHEMA, _validate_facts, schema-aware prompts, vulcan_audit"
)
```
