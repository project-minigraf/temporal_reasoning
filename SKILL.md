# Temporal Reasoning

Perfect memory. Exact reasoning. Complete history.

Temporal Reasoning gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.

## The Core Idea

Every session starts from zero — you ask questions already answered, write code that contradicts decisions already made, and miss constraints established weeks ago. Temporal Reasoning fixes this: a persistent bi-temporal graph store you write to and query at any time, so context survives across sessions.

**The two habits this skill builds:**
- **Write immediately** when the user establishes something worth keeping (decision, preference, constraint)
- **Read before acting** when the user asks about the past, or when you're about to modify something where past decisions might apply

## When to Write (vulcan_transact)

Write to memory when the user's words signal a durable fact:

| Signal | Examples | What to store |
|--------|----------|--------------|
| Decision language | "we'll use X", "going with Y", "we decided Z" | The decision + what was rejected |
| Preference | "I prefer", "I don't like", "always/never use" | The preference + why (if given) |
| Constraint | "must be", "can't use", "prioritize X over Y" | The constraint + the tradeoff |
| Dependency | "depends on", "requires", "calls into" | The relationship |
| Architecture | system structure, component roles, data flows | The structure + rationale |

Store the *why* when you have it — a reason like "chosen for async support" is far more useful than the bare fact "using FastAPI".

After every write, say: "I've stored that in memory." and summarize what was stored.

## When to Read (vulcan_query)

Query memory before you answer or act, when:
- The user asks about past decisions, architecture, preferences, or constraints
- The user says "what did we...", "how did we...", "why did we...", "what was our..."
- The user references something from "earlier", "before", "last time"
- You're about to write code that touches existing architecture
- There's any ambiguity about what was established before

Say "Let me check memory..." before querying. Then:
- If memory has relevant facts → cite them specifically and ground your answer in them
- If memory is empty or returns nothing relevant → say "Memory doesn't have anything recorded about this" and ask if they'd like to share context you can store

**Query first, answer second.** The reason: a confident answer that contradicts a stored decision is far more damaging than taking a moment to check.

## When to Retract (vulcan_retract)

Retract when:
- The user explicitly says "remove", "delete", "retract", "forget", "that's no longer true"
- A fact has been superseded by a newer decision
- A fact was stored incorrectly

After retraction, say: "I've removed that from memory (the original is preserved in history)."

## What NOT to Store

Skip transient observations, intermediate reasoning, raw code snippets, and restatements of what the user just said. Store durable, cross-session facts only: decisions, preferences, constraints, dependencies, architecture.

## Entity Idents and Attribute Names

Facts are stored as triples: `[entity attribute value]`. The entity ident is the organizing key — it carries all the identity and namespacing you need. Use flat, descriptive attribute names.

**Entity idents** should be meaningful and namespaced: `:project/postgres`, `:preference/no-db-mocks`, `:rules/python-version`

**Attribute names** should be flat and self-explanatory: `:name`, `:role`, `:reason`, `:rejected`, `:description`, `:tradeoff`, `:entity-type`, `:calls`, `:depends-on`, `:motivated-by`, `:governs`

```
[:project/postgres :name "PostgreSQL 15"]
[:project/postgres :role "primary database"]
[:project/postgres :tradeoff "lower write throughput"]
[:preference/no-db-mocks :description "always use real DB connections in tests"]
[:preference/no-db-mocks :reason "mock/prod divergence caused silent migration failure"]
```

To retrieve all facts for an entity, query by ident directly — no need to know attribute names in advance:
```python
query("[:find ?a ?v :where [:project/postgres ?a ?v]]")
```

Before adding new facts about an entity, query it first to find existing attributes and avoid duplication.

## Entity Types and Graph Relationships

### Typing entities with `:entity-type`

Assign a type to every entity so you can query across categories without knowing individual entity names:

```python
transact("""[[:project/auth-service :name "AuthService"]
             [:project/auth-service :entity-type :type/component]
             [:rules/python-version :description "must support Python 3.8 minimum"]
             [:rules/python-version :entity-type :type/constraint]]""",
         reason="Component and constraint with types")
```

Use these canonical type keywords:
- `:type/component` — service, module, library, or system component
- `:type/decision` — architecture or design decision
- `:type/constraint` — rule, requirement, or invariant
- `:type/preference` — user preference or style choice

Query all constraints: `[:find ?e ?desc :where [?e :entity-type :type/constraint] [?e :description ?desc]]`

**Do not create root entities for namespaces** (no `:project`, `:preference`, `:rules` entities). The namespace in the entity ident already encodes category implicitly. `:entity-type` covers the cases where you need typed cross-category queries.

### Entity references (not strings) for relationships

When a value refers to another entity in memory, store it as an entity keyword — **never as a string**. This is what makes the graph traversable.

```datalog
; WRONG — string dead-end, cannot traverse
[:project/auth-service :calls "jwt-module"]

; CORRECT — entity reference, edge is traversable
[:project/auth-service :calls :project/jwt-module]
```

Rule of thumb: if the value names something that IS or WILL BE an entity in memory, use its entity ident keyword.

### Relationship vocabulary

Use these standard attributes for edges between entities:

| Attribute | Meaning | Value type |
|---|---|---|
| `:calls` | component invokes another component | entity ref |
| `:depends-on` | component requires another to function | entity ref |
| `:motivated-by` | decision was driven by a constraint | entity ref |
| `:supersedes` | this decision replaces an older one | entity ref |
| `:governs` | constraint applies to a component | entity ref |

For traversal, use recursive rules (see Quick Reference).

## Auto-Memory (MCP Server)

When the MCP server is configured and hooks are enabled, memory is managed automatically without explicit tool calls:

- **Before each turn** — `memory_prepare_turn` is called with the user's message and the result is injected as `additionalContext`.
- **After each turn** — `memory_finalize_turn` is called with the user+agent exchange; facts are extracted and stored.

Extraction strategy is controlled by `VULCAN_EXTRACTION_STRATEGY` (env var):
- `heuristic` (default) — regex signal detection, zero API calls
- `llm` — Claude Haiku extracts facts; falls back to agent on API failure
- `agent` — MCP sampling asks the connected agent to identify facts

**Without hooks** (OpenCode, OpenClaw, or unconfigured): call the tools explicitly at the start and end of each turn.

### memory_prepare_turn

Call at the **start** of each turn. Returns a context block string with facts relevant to the user's message.

```
memory_prepare_turn(user_message="what database did we decide on?")
# → "Relevant memory context:\n  :name | PostgreSQL 15\n  :role | primary database"
```

### memory_finalize_turn

Call at the **end** of each turn. Extracts durable facts from the completed exchange and stores them.

```
memory_finalize_turn(conversation_delta="User: We'll use Redis for caching.\nAgent: Stored.")
# → {"ok": true, "stored_count": 1, "strategy": "heuristic"}
```

## Tools

### vulcan_transact
```python
from vulcan import transact

transact("""[[:project/postgres :name "PostgreSQL 15"]
             [:project/postgres :role "primary database"]
             [:project/postgres :priority "ACID compliance + JSON support"]
             [:project/postgres :tradeoff "lower write throughput"]]""",
         reason="Database choice finalized — JSON support required for analytics queries")
```

Or via CLI (from project directory):
```bash
python vulcan.py transact '[...]' --reason "why this is worth keeping"
```

### vulcan_query
```python
from vulcan import query

# All facts for a known entity
query("[:find ?a ?v :where [:project/postgres ?a ?v]]")

# Broad scan of everything in memory
query("[:find ?e ?a ?v :where [?e ?a ?v]]")

# Search stored values by content (useful when entity ident is unknown)
query('[:find ?e ?a ?v :where [?e ?a ?v] (contains? ?v "Redis")]')
query('[:find ?e ?v :where [?e :reason ?v] (starts-with? ?v "chosen")]')

# Temporal — state at transaction N
query("[:find ?a ?v :as-of 5 :where [:project/postgres ?a ?v]]")
```

### vulcan_retract
```python
from vulcan import retract
retract("[[:project/old-service :name \"obsolete\"]]",
        reason="Service decommissioned")
```

## Quick Reference

### Aggregations
- `(count ?e)` / `(count-distinct ?e)` / `(sum ?n)` / `(min ?x)` / `(max ?x)`
- Group by: `[:find ?role (count ?e) :where [?e :role ?role]]`

### Bi-temporal
- `:as-of N` — state at transaction N
- `:valid-at "2024-01-01"` — facts valid at date
- `:any-valid-time` — ignore valid-time filter

### Filter predicates (on values)
- `(starts-with? ?v "text")` — value begins with text
- `(ends-with? ?v ".rs")` — value ends with text
- `(contains? ?v "keyword")` — value contains keyword
- `(matches? ?v "^regex$")` — value matches regex

### Negation
- `(not [?e :attr val])` — exclude matches
- `(not-join [?e] [?e :attr ?x])` — existential negation

### Rules (edge-type aliasing)
Rules apply base-case matches and are useful for unifying multiple edge types
under one name. **Recursive rule clauses are not evaluated** — use explicit
multi-hop joins for fixed-depth traversal instead.

```
; Unify :depends-on and :calls into one relation
(rule [(linked ?a ?d) [?a :depends-on ?d]])
(rule [(linked ?a ?d) [?a :calls ?d]])
[:find ?name :where (linked :project/api-gateway ?svc) [?svc :name ?name]]
```

### Multi-hop joins (fixed-depth traversal)
For transitive impact across N hops, write explicit join patterns:
```
; 2-hop: api-gateway → auth-service → jwt-validator
[:find ?name
 :where [:project/api-gateway :calls ?mid]
        [?mid :depends-on ?leaf]
        [?leaf :name ?name]]
```

For advanced syntax: https://github.com/adityamukho/minigraf/wiki/Datalog-Reference

## Graph Storage

Default: `memory.graph` in the current working directory. Run all commands from the same project root to ensure consistent graph access.

## Dependencies

- **Minigraf >= 0.19.0** — run `python install.py` to download the correct pre-built binary for your platform automatically. Falls back to `cargo install minigraf` only on unsupported platforms.
- **Python 3** — for the wrapper

## Examples

### Storing a tech stack decision
User: "We're using FastAPI over Flask — async support is critical for our Redis calls."
```python
transact("""[[:project/api-layer :name "FastAPI"]
             [:project/api-layer :entity-type :type/decision]
             [:project/api-layer :rejected "Flask"]
             [:project/api-layer :reason "async support required for Redis calls"]]""",
         reason="API framework finalized")
```

### Storing a component relationship (entity reference, not string)
User: "The auth service calls the JWT module for token validation."
```python
transact("""[[:project/auth-service :name "AuthService"]
             [:project/auth-service :entity-type :type/component]
             [:project/auth-service :calls :project/jwt-module]
             [:project/jwt-module :name "JWTModule"]
             [:project/jwt-module :entity-type :type/component]]""",
         reason="Component dependency for impact analysis")
```
`:calls` holds the entity ident `:project/jwt-module` — not the string `"jwt-module"`. This makes the edge traversable.

### Decision motivated by a constraint
User: "We chose asyncio over threading because of the GIL."
```python
transact("""[[:rules/gil-constraint :description "Python GIL limits true thread parallelism"]
             [:rules/gil-constraint :entity-type :type/constraint]
             [:project/asyncio-choice :description "use asyncio over threading"]
             [:project/asyncio-choice :entity-type :type/decision]
             [:project/asyncio-choice :motivated-by :rules/gil-constraint]]""",
         reason="Decision traceability — why asyncio was chosen")
```
Query: "Why asyncio?" traverses the edge:
```python
query("""[:find ?reason
          :where [?d :description "use asyncio over threading"]
                 [?d :motivated-by ?c]
                 [?c :description ?reason]]""")
```

### Impact analysis via multi-hop join
User: "What breaks if I change the key-store service?"

Use explicit join patterns for fixed-depth traversal (minigraf rules are
base-case only and do not recurse):
```python
# Direct dependents (1 hop)
query("[:find ?name :where [?svc :depends-on :project/key-store] [?svc :name ?name]]")

# 2-hop: also find services that depend on those services
query("""[:find ?name
          :where [?mid :depends-on :project/key-store]
                 [?svc :depends-on ?mid]
                 [?svc :name ?name]]""")
```

Use rules to unify multiple edge types when scanning across mixed relationships:
```python
query("""(rule [(linked ?a ?d) [?a :depends-on ?d]])
         (rule [(linked ?a ?d) [?a :calls ?d]])
         [:find ?name
          :where (linked :project/auth-service ?svc)
                 [?svc :name ?name]]""")
```

### Find all entities of a given type
```python
# All components
query("[:find ?name :where [?e :entity-type :type/component] [?e :name ?name]]")

# All constraints that govern the auth service
query("""[:find ?desc
          :where [?c :governs :project/auth-service]
                 [?c :description ?desc]]""")
```

### Retrieving facts for a known entity
```python
query("[:find ?a ?v :where [:project/api-layer ?a ?v]]")
# Returns: :name "FastAPI", :rejected "Flask", :reason "async support..."
```

### Searching memory by content (entity ident unknown)
User: "What did we decide about Redis?"
```python
query('[:find ?e ?a ?v :where [?e ?a ?v] (contains? ?v "Redis")]')
# Finds any stored fact whose value mentions Redis
```

### Querying before modifying code
User: "Add connection pooling to the DB layer."
```python
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")
# Scan results for any DB-related decisions before touching anything
```

### Handling empty memory
User: "What database did we decide on?"
```python
result = query("[:find ?a ?v :where [:project/postgres ?a ?v]]")
# result["results"] == []
```
Response: "Let me check memory... Memory doesn't have anything recorded about a database choice. If you share the decision, I'll store it for future sessions."

### Surfacing a constraint conflict
User: "Help me set up a MySQL connection."
```python
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")
# Finds [:project/postgres :name "PostgreSQL 15"] and [:project/postgres :role "primary database"]
```
Response: "Before we proceed — memory shows we're using PostgreSQL 15 as the primary database. Is this a new secondary database, or has the decision changed? If it's changed, I'll update memory to reflect that."

### Storing a preference with context
User: "I hate mocks in DB tests — we got burned when mocked tests passed but the migration failed."
```python
transact("""[[:preference/no-db-mocks :description "always use real database connections in tests"]
             [:preference/no-db-mocks :reason "mock/prod divergence caused silent migration failure"]]""",
         reason="Strong team preference — backed by production incident")
```

### Changing a decision — retraction with preserved history
User: "We're dropping PostgreSQL, switching to CockroachDB for geo-distribution."

```python
# 1. Check what's currently stored
result = query("[:find ?a ?v :where [:project/db ?a ?v]]")
# → :name "PostgreSQL", :role "primary database", :reason "ACID + JSON"

# 2. Retract the old facts (they stay in history — still queryable with :as-of)
retract("""[[:project/db :name "PostgreSQL"]
            [:project/db :reason "ACID + JSON support"]]""",
        reason="Switching to CockroachDB for geo-distribution")

# 3. Store the new decision
transact("""[[:project/db :name "CockroachDB"]
             [:project/db :reason "geo-distribution requirement"]]""",
         reason="Switching to CockroachDB for geo-distribution")

# 4. Old decision is still in history — what did we know at transaction 3?
query("[:find ?name :as-of 3 :where [:project/db :name ?name]]")
# → "PostgreSQL"
```

This is the key difference from a simple key-value store: changing your mind doesn't erase the record. The agent can always reconstruct what was decided and when.

## Error Responses

All functions return `{"ok": bool, ...}`. Common errors:
- `minigraf not found` — install via `cargo install minigraf`
- `No graph file at <path>` — call `transact()` first
- `as_of requires :as-of clause` — include `:as-of N` in query
- `reason is required for all writes` — provide non-empty reason

If an error persists after checking syntax and installation, use `vulcan_report_issue` to file a structured bug report with the failing query and error message:

```python
from report_issue import report_issue
report_issue("parse_error", "query returns unexpected output",
             datalog="[:find ?x :where [?e :a ?x]]",
             error="<error text from result['error']>")
```

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | Persistent MCP server — primary interface via MCP tools |
| `vulcan.py` | Python wrapper (import or CLI — for direct use outside MCP) |
| `report_issue.py` | GitHub issue reporter for errors |
| `hooks/claude-code.json` | Claude Code settings fragment (MCP server + auto-memory hooks) |
| `hooks/prepare_hook.py` | UserPromptSubmit hook script for Claude Code |
| `hooks/finalize_hook.py` | Stop hook script for Claude Code |
| `hooks/opencode.json` | OpenCode MCP config (degraded mode — no hook support yet) |
| `hooks/openclaw.json` | OpenClaw MCP config (degraded mode — issue #28596) |
| `hooks/codex.toml` | Codex CLI MCP config with commented hook stubs |
| `hooks/hermes.yaml` | Hermes MCP config with commented hook stubs |
| `tools/query.json` | Tool schema for vulcan_query |
| `tools/transact.json` | Tool schema for vulcan_transact |
| `tools/retract.json` | Tool schema for vulcan_retract |
| `tools/report_issue.json` | Tool schema for vulcan_report_issue |
| `tools/memory_prepare_turn.json` | Tool schema for memory_prepare_turn |
| `tools/memory_finalize_turn.json` | Tool schema for memory_finalize_turn |
| `install.py` | Setup script |
| `ROADMAP.md` | Project roadmap |
