---
name: temporal-reasoning
description: Use when persisting decisions, preferences, or architectural facts across coding sessions, or querying what was previously decided, built, or constrained in this project.
---

# Temporal Reasoning

Perfect memory. Exact reasoning. Complete history.

Temporal Reasoning gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.

## The Core Idea

Every session starts from zero — you ask questions already answered, write code that contradicts decisions already made, and miss constraints established weeks ago. Temporal Reasoning fixes this: a persistent bi-temporal graph store you write to and query at any time, so context survives across sessions.

**The two habits this skill builds:**
- **Write immediately** when the user establishes something worth keeping (decision, preference, constraint)
- **Read before acting** when the user asks about the past, or when you're about to modify something where past decisions might apply

## When to Write (minigraf_transact)

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

## When to Read (minigraf_query)

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

**Two things to know before writing your first query:**

1. **Filter predicates require square brackets.** `(contains? ?v "text")` silently returns nothing. Always write `[(contains? ?v "text")]`.

2. **To discover what attribute names exist on an entity**, query it directly — you don't need to know them in advance:
   ```python
   query("[:find ?a ?v :where [:decision/postgres ?a ?v]]")
   # Returns all attribute/value pairs for that entity
   ```
   To scan all stored attributes across the graph: `[:find ?e ?a :where [?e ?a ?v]]`

## When to Retract (minigraf_retract)

Retract when:
- The user explicitly says "remove", "delete", "retract", "forget", "that's no longer true"
- A fact has been superseded by a newer decision
- A fact was stored incorrectly

After retraction, say: "I've removed that from memory (the original is preserved in history)."

## What NOT to Store

Skip transient observations, intermediate reasoning, raw code snippets, and restatements of what the user just said. Store durable, cross-session facts only: decisions, preferences, constraints, dependencies, architecture.

## Entity Idents and Attribute Names

Facts are stored as triples: `[entity attribute value]`. The entity ident is the organizing key — it carries all the identity and namespacing you need. Use flat, descriptive attribute names.

**Entity idents** should be meaningful and namespaced: `:decision/postgres`, `:preference/no-db-mocks`, `:constraint/python-version`

**Attribute names** should be flat and self-explanatory: `:description`, `:rationale`, `:date`, `:alias`, `:entity-type`, `:calls`, `:depends-on`, `:motivated-by`, `:governs`

```
[:decision/postgres :description "PostgreSQL 15 — primary database"]
[:decision/postgres :rationale "ACID compliance + JSON support; tradeoff: lower write throughput"]
[:preference/no-db-mocks :description "always use real DB connections in tests"]
[:preference/no-db-mocks :rationale "mock/prod divergence caused silent migration failure"]
```

To retrieve all facts for an entity, query by ident directly — no need to know attribute names in advance:
```python
query("[:find ?a ?v :where [:decision/postgres ?a ?v]]")
```

Before adding new facts about an entity, query it first to find existing attributes and avoid duplication.

## Entity Resolution

Before storing a new entity, always check for existing canonical idents and aliases:

```datalog
[:find ?e ?desc :where [?e :description ?desc]]
[:find ?e ?a :where [?e :alias ?a]]
```

If a reference matches an existing ident or alias, reuse that exact ident.
Only mint a new ident if the entity is genuinely new.

Canonical ident form: lowercase, hyphens only — `:decision/redis` not `:decision/Redis_cache`.

Allowed entity types: `:decision/`, `:preference/`, `:constraint/`, `:dependency/`, `:module/`, `:function/`, `:class/`, `:variable/`, `:field/` (code structure — auto-ingested); `:commit/`, `:tag/`, `:ingestion/` are system-only (written by `minigraf_ingest_git`), do not write to them directly
Required attribute on all types: `:description`
Optional attributes: `:rationale`, `:date`, `:alias`

Run `minigraf_audit` periodically or after a session with heavy writes to detect and retract any schema violations.

## Entity Types and Graph Relationships

### Typing entities with `:entity-type`

Assign a type to every entity so you can query across categories without knowing individual entity names:

```python
transact("""[[:dependency/auth-service :description "AuthService"]
             [:dependency/auth-service :entity-type :type/dependency]
             [:constraint/python-version :description "must support Python 3.8 minimum"]
             [:constraint/python-version :entity-type :type/constraint]]""",
         reason="Dependency and constraint with types")
```

Use these canonical type keywords:
- `:type/dependency` — service, module, library, or system component
- `:type/decision` — architecture or design decision
- `:type/constraint` — rule, requirement, or invariant
- `:type/preference` — user preference or style choice

Query all constraints: `[:find ?e ?desc :where [?e :entity-type :type/constraint] [?e :description ?desc]]`

**Do not create root entities for namespaces** (no `:project`, `:preference`, `:rules` entities). The namespace in the entity ident already encodes category implicitly. `:entity-type` covers the cases where you need typed cross-category queries.

### Entity references (not strings) for relationships

When a value refers to another entity in memory, store it as an entity keyword — **never as a string**. This is what makes the graph traversable.

```datalog
; WRONG — string dead-end, cannot traverse
[:dependency/auth-service :calls "jwt-module"]

; CORRECT — entity reference, edge is traversable
[:dependency/auth-service :calls :dependency/jwt-module]
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

Extraction strategy is controlled by `MINIGRAF_EXTRACTION_STRATEGY` (env var):
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

### minigraf_transact
```python
from minigraf import transact

transact("""[[:decision/postgres :description "PostgreSQL 15 — primary database"]
             [:decision/postgres :entity-type :type/decision]
             [:decision/postgres :rationale "ACID compliance + JSON support; tradeoff: lower write throughput"]]""",
         reason="Database choice finalized — JSON support required for analytics queries")
```

Or via CLI (from project directory):
```bash
python minigraf.py transact '[...]' --reason "why this is worth keeping"
```

### minigraf_query
```python
from minigraf import query

# All facts for a known entity
query("[:find ?a ?v :where [:decision/postgres ?a ?v]]")

# Broad scan of everything in memory
query("[:find ?e ?a ?v :where [?e ?a ?v]]")

# Search stored values by content (useful when entity ident is unknown)
query('[:find ?e ?a ?v :where [?e ?a ?v] (contains? ?v "Redis")]')
query('[:find ?e ?v :where [?e :rationale ?v] (starts-with? ?v "chosen")]')

# Temporal — state at transaction N
query("[:find ?a ?v :as-of 5 :where [:decision/postgres ?a ?v]]")
```

### minigraf_retract
```python
from minigraf import retract
retract("[[:dependency/old-service :description \"obsolete\"]]",
        reason="Service decommissioned")
```

### minigraf_rule

Register a Datalog rule for the current server session. Rules enable recursive graph traversal and edge-type aliasing. Re-register after server restart (or add to `SESSION_RULES` in `mcp_server.py` for permanence).

```python
# Base case (register first)
minigraf_rule("[(ancestor ?a ?d) [?a :parent ?d]]")
# Recursive case
minigraf_rule("[(ancestor ?a ?d) [?a :parent ?m] (ancestor ?m ?d)]")

# Now query using the rule
minigraf_query("[:find ?anc :where (ancestor :commit/abc123def456 ?anc) [?anc :subject ?s]]")
```

Returns `{"ok": true, "rule": "..."}` on success, or `{"ok": false, "error": "..."}` if the rule has a syntax error or creates a negative cycle.

### minigraf_ingest_git

Start background ingestion of code structure from git history into the bi-temporal graph. Returns immediately — ingestion runs as an asyncio background task.

**Always call `minigraf_ingest_status` first.** Only call `minigraf_ingest_git` if status shows `idle`. If status is `starting`, `running`, or `complete`, do not start a new ingestion — report the current status to the user instead.

```python
# Step 1: check status
minigraf_ingest_status()
# → {"ok": true, "status": "idle", ...}

# Step 2: start only if idle
minigraf_ingest_git(repo_path="/path/to/repo", branch="HEAD")
# → {"ok": true, "job_id": "git-ingest", "message": "Ingestion started for /path/to/repo"}

# If already running:
# → {"ok": false, "error": "ingestion already in progress"}

# If another live process already owns the graph lock:
# → {"ok": false, "error": "ingestion already owned by live process (pid 12345)", "owner_pid": 12345}
```

Once started, inform the user that ingestion is running in the background and move on — do not poll or wait for completion.

Auto-started at MCP server startup — the server creates a background asyncio task that calls `_run_ingestion(cwd, "HEAD")` immediately. Set `MINIGRAF_NO_AUTO_INGEST=1` to suppress this (useful in eval sandboxes). Incremental: reads the `:ingestion/watermark` entity to determine the last ingested commit, then only processes new commits.

Vendored/third-party/generated paths are skipped for AST extraction by default (`3rdParty/`, `third_party/`, `vendor/`, `node_modules/`, `dist/`, `build/`, `*.min.js`, `*.map`) — no per-file entities are created for them, and any in-repo import resolving into an ignored path is tagged `:type/external-dependency` instead of an internal module dependency. Extend the ignore list with `MINIGRAF_INGEST_IGNORE` (comma-separated globs/prefixes, e.g. `MINIGRAF_INGEST_IGNORE=generated/,*.pb.go`) and/or a repo-local `.temporalignore` file (one pattern per line, `#` comments allowed) — both add to the defaults, they don't replace them. Ignore config is resolved once when ingestion starts and applies uniformly across all historical commits; it does not retroactively remove entities from a graph that was already ingested before the ignore list was added.

Do not write to `:ingestion/watermark` or any `:ingestion/` entity directly.

### minigraf_ingest_status

Poll the current git ingestion progress.

```python
minigraf_ingest_status()
# → {"ok": true, "status": "running", "processed": 21717, "processed_this_run": 2,
#    "total": 47, "current_commit": "a3f2bc...", "error": null}
```

`status` is one of: `idle`, `starting`, `running`, `complete`, `error`, `stopped`, `skipped`.
`starting` means a background ingestion task has been created (auto-started at
server boot, or via `minigraf_ingest_git`) but hasn't finished its preload phase
(re-scanning already-known entities/dependencies) yet, so `processed`/`total`
aren't populated — a subsequent `minigraf_ingest_git` call will still be
rejected with "already in progress" during this window, same as `running`.
`stopped` means a graceful shutdown (session end) paused ingestion between commits —
not a failure; the next `minigraf_ingest_git` call (or server auto-start)
resumes from the watermark automatically. `skipped` means another live process
already owns the graph lock (its PID is in `owner_pid`) — this server will not
attempt ingestion on its own; call `minigraf_ingest_git` again later to retry.
For `error` and `skipped`, a `stale` field may be present: `stale: true` means the
process that caused this state is no longer alive, so a `minigraf_ingest_git` retry
is likely to succeed now — check it before assuming a cached error is still accurate.
`error` also includes `error_at`, the timestamp the failure occurred. `processed` is the
cumulative count of durably persisted commits (seeded from the true
`:type/commit` entity count at run start, so it stays accurate even after a
prior run was interrupted mid-way — e.g. by lock contention). `processed_this_run`
is how many commits *this* run-attempt has ingested, useful for distinguishing
fresh progress from work already persisted by earlier runs. When idle,
`total_ingested` similarly reflects the true persisted count, not a
potentially stale watermark.

### Git-Ingested Data Schema

`minigraf_ingest_git` writes the following entity types. All relationship attributes (`:parent`, `:introduced-by`, `:modified-in`, `:contains`, `:depends-on`, `:tagged-commit`, `:resolves-to`) are stored as keyword entity references — they bypass string-value schema validation by design and are directly traversable in queries.

**Ident slugging:** non-alphanumeric characters in paths and names are replaced with hyphens and consecutive hyphens collapsed. Examples: `src/auth.py` → `:module/src-auth-py`; function `login` in `src/auth.py` → `:function/src-auth-py-login`.

#### `:type/commit` — one per git commit
Ident: `:commit/<first-12-chars-of-hash>`

| Attribute | Notes |
|---|---|
| `:description` | commit subject (truncated to 120 chars) |
| `:hash` | full 40-char hash |
| `:author` | author email |
| `:subject` | commit subject (truncated to 200 chars) |
| `:date` | ISO 8601 UTC timestamp, e.g. `"2026-05-26T14:32:00Z"` |
| `:parent` (keyword ref) | parent commit(s); merge commits have two |

#### `:type/module` — one per source file, written on the commit that introduces it
Ident: `:module/<slugified-file-path>`

| Attribute | Notes |
|---|---|
| `:description` | file path, e.g. `"src/auth.py"` |
| `:path` | file path |
| `:introduced-by` (keyword ref) | commit that first added this file |
| `:modified-in` (keyword ref) | one edge per subsequent modifying commit |
| `:contains` (keyword ref) | functions, classes, variables, and fields defined in this file |
| `:depends-on` (keyword ref) | modules this file imports — tracked per-commit with full valid-time bounds: `:valid-from` = commit that introduced the import, `:valid-to` = commit that removed it (open-ended if still present) |
| `:renamed-from` / `:renamed-to` (keyword ref) | rename/move continuity — see below |

#### `:type/function` — one per top-level function or method
Ident: `:function/<slugified-path-name>` (file path + `::` + function name, slugified together)

| Attribute | Notes |
|---|---|
| `:description` | function name |
| `:file` | source file path |
| `:introduced-by` (keyword ref) | commit that first defined this function |
| `:modified-in` (keyword ref) | one edge per subsequent modifying commit |
| `:renamed-from` / `:renamed-to` (keyword ref) | rename/move continuity — see below |

#### `:type/class` — one per class, struct, or type definition (same ident convention as function)

| Attribute | Notes |
|---|---|
| `:description` | class or struct name |
| `:file` | source file path |
| `:introduced-by` (keyword ref) | commit that first defined this class |
| `:modified-in` (keyword ref) | one edge per subsequent modifying commit |
| `:renamed-from` / `:renamed-to` (keyword ref) | rename/move continuity — see below |

#### `:type/variable` — one per module-level global (same ident convention as function)

| Attribute | Notes |
|---|---|
| `:description` | variable name |
| `:file` | source file path |
| `:introduced-by` (keyword ref) | commit that first defined this global |
| `:modified-in` (keyword ref) | one edge per subsequent modifying commit |
| `:renamed-from` / `:renamed-to` (keyword ref) | rename/move continuity — see below |

#### `:type/field` — one per class/struct member, instance or static (ident convention: file path + `::` + `<ClassName>.<fieldName>`, slugified together)

| Attribute | Notes |
|---|---|
| `:description` | `"<ClassName>.<fieldName>"` |
| `:file` | source file path |
| `:class` (keyword ref) | the owning class/struct entity |
| `:static` | `true`/`false` — whether this is a static (class-level) field vs. an instance field |
| `:introduced-by` (keyword ref) | commit that first defined this field |
| `:modified-in` (keyword ref) | one edge per subsequent modifying commit |
| `:renamed-from` / `:renamed-to` (keyword ref) | rename/move continuity — see below |

**Rename/move continuity (`:renamed-from` / `:renamed-to`):** all five code entity types (module, function, class, variable, field) can carry these. When ingestion detects a rename — a file rename/move (via git's own `-M` similarity detection) or a function/class/global/field rename (via a custom AST-based matcher tolerant of local-variable renames) — the old entity is closed as usual but also gets `:renamed-to` pointing at the new entity's ident, and the new entity gets `:renamed-from` pointing back at the old one. This lets a query traverse continuous history across a rename that would otherwise look like an unrelated deletion followed by an unrelated creation.

**Supported languages for `:type/variable`/`:type/field` extraction:** the same full language list as function/class extraction — see "Supported languages for AST extraction" below.

#### `:type/tag` — one per git tag (system-only, not audited)
Ident: `:tag/<slugified-tag-name>`

| Attribute | Notes |
|---|---|
| `:description` | `"git tag <name>"` |
| `:name` | original tag name |
| `:date` | tag creation date (if available) |
| `:tagged-commit` (keyword ref) | the commit this tag points to |

#### `:type/external-dependency` — real git submodules and genuinely-unresolved imports
Ident: `:module/<slugified-path-or-import-name>` (shares the module ident namespace — only `:entity-type` distinguishes internal from external)

| Attribute | Notes |
|---|---|
| `:description` | submodule's declared name from `.gitmodules` if resolvable, else raw path (submodules); raw import specifier (unresolved imports) |
| `:path` | submodule's repo path (submodules only; absent for unresolved-import placeholders — they have no path) |
| `:pinned-commit` | pinned commit SHA the submodule currently points to (submodules only); bi-temporally closed and reopened on every bump — point-in-time queries see the SHA pinned at that time |
| `:submodule-name` / `:submodule-url` | from `.gitmodules`, when parseable (submodules only) |
| `:introduced-by` (keyword ref) | commit that first introduced this dependency |
| `:modified-in` (keyword ref) | one edge per commit that bumped a submodule's pinned commit |
| `:resolves-to` (keyword ref, unresolved-import placeholders only) | points to the real submodule entity when this placeholder's import path falls under a known `.gitmodules`/gitlink path — bridges the two ident schemes (`_canonical_ident` from the raw import specifier vs. `_code_ident` from the submodule's declared path), which otherwise never produce the same ident string even once both entities exist. Set regardless of which one was ingested first. |

Vendored-in-tree code checked in as regular files (not a git submodule) is parsed as ordinary `:type/module`/`:function`/`:class` entities like any first-party code — only real gitlinks (mode `160000`) and genuinely-unresolved imports get the external marker.

**Supported languages for AST extraction:** Python, JavaScript, TypeScript (+ TSX/JSX), Rust, Go, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Scala, Haskell, Lua, Elixir. Files in other languages are tracked as modules (with `:introduced-by`/`:modified-in`) but yield no function, class, variable, or field entities.

**Pre-registered SESSION_RULES** — these are always available; no `minigraf_rule` call needed:

| Rule | Traverses | Use for |
|---|---|---|
| `(ancestor ?child ?anc)` | `:parent` (recursive) | commit graph ancestry |
| `(reachable ?a ?b)` | `:depends-on`, `:calls`, `:contains` (recursive) | transitive code reachability |
| `(linked ?a ?b)` | `:depends-on`, `:calls`, `:contains` (single hop) | direct cross-edge-type queries |

### Code Structure Query Examples

Once ingestion is complete, query code structure with `:valid-at` for point-in-time views:

```datalog
; All functions in auth.py as of today
[:find ?fn :valid-at "2026-05-26"
 :where [:module/src-auth-py :contains ?e] [?e :description ?fn]]

; All modules that depend on auth.py
[:find ?caller :valid-at "2026-05-26"
 :where [?e :depends-on :module/src-auth-py] [?e :description ?caller]]

; All modules transitively reachable from src/auth.py (via :contains and :depends-on)
[:find ?dep :valid-at "2026-05-26"
 :where (reachable :module/src-auth-py ?d) [?d :description ?dep]]
```

Cross-layer queries joining code structure with agent decisions:
```datalog
; What dependency changes happened after a specific date?
; Run two queries and diff:
; Q1 (before): [:find ?m ?d :valid-at "2024-12-01" :where [?e :depends-on ?f] [?e :description ?m] [?f :description ?d]]
; Q2 (after):  [:find ?m ?d :valid-at "2026-05-26" :where [?e :depends-on ?f] [?e :description ?m] [?f :description ?d]]
; Rows in Q2 absent from Q1 = new dependencies since the date
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

### Rules and recursive traversal
Register rules with `minigraf_rule` before querying. Rules support full recursion via semi-naive fixed-point evaluation — cycles are handled safely.

```datalog
; Base case — register first
(rule [(ancestor ?a ?d) [?a :parent ?d]])
; Recursive case — register second
(rule [(ancestor ?a ?d) [?a :parent ?m] (ancestor ?m ?d)])

; Now use in a query
[:find ?anc :where (ancestor :commit/abc123 ?anc)]
```

Unify multiple edge types under one name:
```datalog
(rule [(linked ?a ?b) [?a :depends-on ?b]])
(rule [(linked ?a ?b) [?a :calls ?b]])
[:find ?desc :where (linked :dependency/api-gateway ?svc) [?svc :description ?desc]]
```

Rules registered via `minigraf_rule` persist for the server session. After a server restart, re-register them or add them to `SESSION_RULES` in `mcp_server.py` to make them permanent.

### Multi-hop joins (fixed-depth, no rule needed)
When you know the exact depth, explicit joins are simpler than registering a rule:
```datalog
; 2-hop: api-gateway → auth-service → jwt-validator
[:find ?desc
 :where [:dependency/api-gateway :calls ?mid]
        [?mid :depends-on ?leaf]
        [?leaf :description ?desc]]
```

### String and numeric comparisons
`>`, `<`, `>=`, `<=`, `=`, `!=` work for both numbers and ISO-8601 date strings (which sort lexicographically). Always wrap in `[...]`:
```datalog
[(> ?date "2026-04-01T00:00:00Z")]   ; date after threshold
[(< ?count 10)]                       ; numeric less-than
[(= ?status "active")]               ; equality
```

**Common mistake**: `(contains? ?v "text")` returns empty — the predicate must be inside square brackets: `[(contains? ?v "text")]`.

Full Datalog grammar: https://github.com/project-minigraf/minigraf/wiki/Datalog-Reference

## Graph Storage

Default: `memory.graph` in the current working directory. Run all commands from the same project root to ensure consistent graph access.

## Dependencies

- **Minigraf >= 0.19.0** — run `python install.py` to download the correct pre-built binary for your platform automatically. Falls back to `cargo install minigraf` only on unsupported platforms.
- **Python 3** — for the wrapper

## Examples

### Storing a tech stack decision
User: "We're using FastAPI over Flask — async support is critical for our Redis calls."
```python
transact("""[[:decision/api-layer :description "use FastAPI over Flask"]
             [:decision/api-layer :entity-type :type/decision]
             [:decision/api-layer :rationale "async support required for Redis calls; rejected Flask"]]""",
         reason="API framework finalized")
```

### Storing a component relationship (entity reference, not string)
User: "The auth service calls the JWT module for token validation."
```python
transact("""[[:dependency/auth-service :description "AuthService"]
             [:dependency/auth-service :entity-type :type/dependency]
             [:dependency/auth-service :calls :dependency/jwt-module]
             [:dependency/jwt-module :description "JWTModule"]
             [:dependency/jwt-module :entity-type :type/dependency]]""",
         reason="Component dependency for impact analysis")
```
`:calls` holds the entity ident `:dependency/jwt-module` — not the string `"jwt-module"`. This makes the edge traversable.

### Decision motivated by a constraint
User: "We chose asyncio over threading because of the GIL."
```python
transact("""[[:constraint/gil :description "Python GIL limits true thread parallelism"]
             [:constraint/gil :entity-type :type/constraint]
             [:decision/asyncio :description "use asyncio over threading"]
             [:decision/asyncio :entity-type :type/decision]
             [:decision/asyncio :motivated-by :constraint/gil]]""",
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

For fixed-depth traversal, use explicit joins:
```python
# Direct dependents (1 hop)
query("[:find ?desc :where [?svc :depends-on :dependency/key-store] [?svc :description ?desc]]")

# 2-hop: also find services that depend on those services
query("""[:find ?desc
          :where [?mid :depends-on :dependency/key-store]
                 [?svc :depends-on ?mid]
                 [?svc :description ?desc]]""")
```

For unbounded transitive traversal, register a recursive rule first:
```python
# Register once per server session
minigraf_rule("[(reachable ?a ?b) [?a :depends-on ?b]]")
minigraf_rule("[(reachable ?a ?b) [?a :depends-on ?m] (reachable ?m ?b)]")

# Then query — finds all transitive dependents at any depth
query("[:find ?desc :where (reachable ?svc :dependency/key-store) [?svc :description ?desc]]")
```

Use rules to unify multiple edge types when scanning across mixed relationships:
```python
minigraf_rule("[(linked ?a ?d) [?a :depends-on ?d]]")
minigraf_rule("[(linked ?a ?d) [?a :calls ?d]]")
query("""[:find ?desc
          :where (linked :dependency/auth-service ?svc)
                 [?svc :description ?desc]]""")

### Find all entities of a given type
```python
# All dependencies/components
query("[:find ?desc :where [?e :entity-type :type/dependency] [?e :description ?desc]]")

# All constraints that govern the auth service
query("""[:find ?desc
          :where [?c :governs :dependency/auth-service]
                 [?c :description ?desc]]""")
```

### Retrieving facts for a known entity
```python
query("[:find ?a ?v :where [:decision/api-layer ?a ?v]]")
# Returns: :description "use FastAPI over Flask", :rationale "async support..."
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
result = query("[:find ?a ?v :where [:decision/postgres ?a ?v]]")
# result["results"] == []
```
Response: "Let me check memory... Memory doesn't have anything recorded about a database choice. If you share the decision, I'll store it for future sessions."

### Surfacing a constraint conflict
User: "Help me set up a MySQL connection."
```python
result = query("[:find ?e ?a ?v :where [?e ?a ?v]]")
# Finds [:decision/postgres :description "PostgreSQL 15 — primary database"]
```
Response: "Before we proceed — memory shows we're using PostgreSQL 15 as the primary database. Is this a new secondary database, or has the decision changed? If it's changed, I'll update memory to reflect that."

### Storing a preference with context
User: "I hate mocks in DB tests — we got burned when mocked tests passed but the migration failed."
```python
transact("""[[:preference/no-db-mocks :description "always use real database connections in tests"]
             [:preference/no-db-mocks :rationale "mock/prod divergence caused silent migration failure"]]""",
         reason="Strong team preference — backed by production incident")
```

### Changing a decision — retraction with preserved history
User: "We're dropping PostgreSQL, switching to CockroachDB for geo-distribution."

```python
# 1. Check what's currently stored
result = query("[:find ?a ?v :where [:decision/db ?a ?v]]")
# → :description "PostgreSQL 15 — primary database", :rationale "ACID + JSON support"

# 2. Retract the old facts (they stay in history — still queryable with :as-of)
retract("""[[:decision/db :description "PostgreSQL 15 — primary database"]
            [:decision/db :rationale "ACID + JSON support"]]""",
        reason="Switching to CockroachDB for geo-distribution")

# 3. Store the new decision
transact("""[[:decision/db :description "CockroachDB — primary database"]
             [:decision/db :rationale "geo-distribution requirement"]]""",
         reason="Switching to CockroachDB for geo-distribution")

# 4. Old decision is still in history — what did we know at transaction 3?
query("[:find ?desc :as-of 3 :where [:decision/db :description ?desc]]")
# → "PostgreSQL 15 — primary database"
```

This is the key difference from a simple key-value store: changing your mind doesn't erase the record. The agent can always reconstruct what was decided and when.

## Error Responses

All functions return `{"ok": bool, ...}`. Common errors:
- `minigraf not found` — install via `cargo install minigraf`
- `No graph file at <path>` — call `transact()` first
- `as_of requires :as-of clause` — include `:as-of N` in query
- `reason is required for all writes` — provide non-empty reason

If an error persists after checking syntax and installation, use `minigraf_report_issue` to file a structured bug report with the failing query and error message:

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
| `minigraf.py` | Python wrapper (import or CLI — for direct use outside MCP) |
| `report_issue.py` | GitHub issue reporter for errors |
| `hooks/claude-code.json` | Claude Code settings fragment (MCP server + auto-memory hooks) |
| `hooks/prepare_hook.py` | UserPromptSubmit hook script for Claude Code |
| `hooks/finalize_hook.py` | Stop hook script for Claude Code |
| `hooks/ingest_hook.py` | Git ingestion trigger hook (fires at session start) |
| `hooks/opencode.json` | OpenCode MCP config (degraded mode — no hook support yet) |
| `hooks/openclaw.json` | OpenClaw MCP config (degraded mode — issue #28596) |
| `hooks/codex.toml` | Codex CLI MCP config with commented hook stubs |
| `hooks/hermes.yaml` | Hermes MCP config with commented hook stubs |
| `tools/query.json` | Tool schema for minigraf_query |
| `tools/transact.json` | Tool schema for minigraf_transact |
| `tools/retract.json` | Tool schema for minigraf_retract |
| `tools/rule.json` | Tool schema for minigraf_rule |
| `tools/report_issue.json` | Tool schema for minigraf_report_issue |
| `tools/memory_prepare_turn.json` | Tool schema for memory_prepare_turn |
| `tools/memory_finalize_turn.json` | Tool schema for memory_finalize_turn |
| `install.py` | Setup script |
| `ROADMAP.md` | Project roadmap |
