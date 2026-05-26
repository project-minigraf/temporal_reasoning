# Phase 4 — Entity Normalization and Schema-Aware Extraction

**Date:** 2026-05-26
**Scope:** `mcp_server.py`, `SKILL.md`, `tests/test_mcp_server.py`

---

## Problem

Without normalization the graph degrades into disconnected synonym clusters across sessions:
- Same entity, different names: "Redis", "Redis-based cache", "the cache layer" → three unconnected entities
- Same attribute, different predicates: `:depends-on` vs `:requires`
- Freeform LLM/agent extraction produces idents that don't conform to any consistent shape

The heuristic strategy does basic lowercasing but no slug normalization, cross-run deduplication, or schema enforcement. The LLM and agent strategies produce entirely freeform triples with no constraints.

---

## Design

### 1. Slug Canonicalization

New function `_canonical_ident(entity_type, value)` in `mcp_server.py`, ported from `_to_kw()` in `minigraf-examples/integrations/llamaindex-python/minigraf_graph_store.py`:

```python
def _canonical_ident(entity_type: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return f":{entity_type}/{slug}"
```

Applied in `heuristic_extract` (replaces the current inline ident construction) and stated as the required canonical form in LLM/agent extraction prompts.

New function `_keyword_uuid(keyword)`, ported from `keyword_entity_id()` in `minigraf-examples/minigraf-algorithms/src/lib.rs`:

```python
import uuid
def _keyword_uuid(keyword: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, keyword))
```

Produces stable, deterministic UUIDs from keyword strings. Used internally by `vulcan_audit` when resolving entity identities during DB queries.

---

### 2. Hardcoded Schema + Pre-Transact Validation

**Closed-world model** — both entity types and attributes are enforced. Unknown entity types are rejected. Unknown attributes on known entities are rejected.

```python
VULCAN_SCHEMA = {
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
```

Pure validation function (no DB access):

```python
def _validate_facts(facts: List[Dict]) -> List[str]:
    """Validate proposed facts against VULCAN_SCHEMA. Returns list of violation strings."""
```

Called inside `_transact_extracted_facts` and `handle_vulcan_transact` before any `db.execute()` call. Invalid facts are dropped and logged; valid facts proceed. This applies to all three extraction strategies and to manual `vulcan_transact` calls.

**Violation types:**
- Entity type not in schema → rejected
- Required attribute missing → rejected
- Attribute present but wrong type → rejected
- Attribute not listed in required or optional → rejected

---

### 3. Schema-Aware Extraction Prompts

Before calling the model, LLM and agent strategies run a preparatory query to fetch existing canonical entity idents:

```python
def _query_canonical_entities() -> str:
    """Query graph for existing entities; format for prompt injection."""
    # [:find ?e ?desc :where [?e :description ?desc]]
    # Returns formatted lines: ":decision/redis — use Redis for caching"
    # Capped at 50 entities (oldest-first truncation if over limit).
    # Returns empty string if graph has no entities.
```

Injected into `_LLM_EXTRACTION_PROMPT` and `_AGENT_SAMPLING_PROMPT` as a new section:

```
Existing canonical entities (reuse these idents — do not invent synonyms):
:decision/redis — use Redis for caching
:constraint/stateless — services must be stateless

Allowed entity type prefixes: :decision/ :preference/ :constraint/ :dependency/
Canonical form: lowercase, hyphens only (no spaces, underscores, dots).
If a reference matches an existing entity, use its ident exactly.
If genuinely new, mint a new slug-form ident.
Check :alias facts before deciding something is new.
```

If the graph has no entities the section is omitted entirely.

---

### 4. Alias Datoms

No new infrastructure. Aliases are plain datoms using `:alias` (already declared as optional `str` in `VULCAN_SCHEMA`):

```
[:decision/redis :alias "Redis-based cache"]
[:decision/redis :alias "the cache layer"]
```

Stored explicitly via `vulcan_transact`. The prepare hook surfaces existing aliases during `memory_prepare_turn` — agents see them as context.

**SKILL.md** gets a new **Entity Resolution** section:

```markdown
## Entity Resolution

Before storing a new entity, always check for existing canonical idents and aliases:

  [:find ?e ?desc :where [?e :description ?desc]]
  [:find ?e ?a :where [?e :alias ?a]]

If a reference matches an existing ident or alias, reuse that ident exactly.
Only mint a new ident if the entity is genuinely new.
Canonical form: lowercase, hyphens only — :decision/redis not :decision/Redis_cache.
Run vulcan_audit periodically or after a session with heavy writes.
```

---

### 5. `vulcan_audit` Tool

New 7th MCP tool, ported from `Schema.audit_as_of()` in `minigraf-examples/minigraf-schema/src/lib.rs`:

```python
def handle_vulcan_audit(as_of: Optional[int] = None) -> Dict[str, Any]:
```

**Procedure:**
1. For each entity type in `VULCAN_SCHEMA`, query all matching entities (with `:as-of N` if `as_of` provided)
2. For each entity, fetch all its attributes
3. Run `_validate_facts` against them
4. If `as_of` is `None` (current state): **retract** each invalid entity with reason `"vulcan_audit: {violation details}"`; retractions are bi-temporal — history is preserved
5. If `as_of` is provided (point-in-time): report violations only, no retractions

**Return shape:**
```python
{
    "ok": True,
    "audited": 12,
    "retracted": 2,
    "violations": [
        {"entity": ":decision/foo", "kind": "MissingRequiredAttribute", "attribute": ":description"},
    ]
}
```

Not wired into hooks — called explicitly by the agent. Agents are instructed via SKILL.md to run it periodically or after heavy write sessions.

---

## Files Changed

| File | Change |
|------|--------|
| `mcp_server.py` | Add `_canonical_ident`, `_keyword_uuid`, `VULCAN_SCHEMA`, `_validate_facts`, `_query_canonical_entities`; update `heuristic_extract`, `_transact_extracted_facts`, `handle_vulcan_transact`, `_LLM_EXTRACTION_PROMPT`, `_AGENT_SAMPLING_PROMPT`; add `handle_vulcan_audit` and wire as 7th tool |
| `SKILL.md` | Add Entity Resolution section |
| `tests/test_mcp_server.py` | Add `TestCanonicalIdent`, `TestKeywordUuid`, `TestValidateFacts`, `TestTransactExtractedFacts` (schema cases), `TestVulcanAudit`, `TestSchemaAwarePrompt`, `TestHeuristicNormalization`; update tool count to 7 |

No other files change.

---

## Non-Goals

- No embedding-based disambiguation (Phase 6 concern, only when entity volume warrants it)
- No user-configurable schema (hardcoded is sufficient at this scale)
- No transparent alias rewriting in `vulcan_query` — aliases are explicit queryable datoms
- No retroactive migration of existing graph data — `vulcan_audit` handles cleanup on next run
