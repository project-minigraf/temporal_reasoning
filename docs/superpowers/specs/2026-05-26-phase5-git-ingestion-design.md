# Phase 5 — Code Structure Evolution from Git History

**Date:** 2026-05-26
**Status:** Approved
**Scope:** `mcp_server.py`, `tests/test_mcp_server.py`, `SKILL.md`, `ROADMAP.md`

---

## Problem

The bi-temporal graph stores agent decisions, preferences, constraints, and dependencies — but knows nothing about code structure. Agents with git access can answer simple temporal questions (`git show`, `git diff`, `git blame`), but git cannot answer cross-cutting semantic queries:

- "When did module A first depend on module B?" requires checking out every commit and parsing code at each one — O(commits × parse time), context-window-destroying on real codebases.
- `git blame` breaks on renames; semantic structure (`calls`, `depends-on`) is entity-addressed, not line-addressed.
- Agent-authored decisions and code structural changes live in separate systems with no shared query surface. In the graph, both are datoms and a single Datalog join connects them.

---

## Design

### Overview

A new MCP tool `vulcan_ingest_git` walks git history and transacts code structure (modules, functions, classes, call edges, dependency edges) into the bi-temporal graph. Ingestion runs as a background `asyncio.Task` so the agent is not blocked and `memory_prepare_turn` (prepare_hook) can interleave between commits. A second tool `vulcan_ingest_status` lets the agent poll progress.

Ingested entities use Phase 4 slug canonicalization and resolve against an extended `VULCAN_SCHEMA`. Incremental re-ingestion is supported via a watermark stored in the graph itself.

---

## Schema Extension

Four new entity types added to `VULCAN_SCHEMA` in `mcp_server.py`:

```python
"module": {
    "required": {":description": str},    # file path, e.g. "src/auth.py"
    "optional": {":path": str, ":alias": str},
},
"function": {
    "required": {":description": str},    # function name
    "optional": {":file": str, ":alias": str},
},
"class": {
    "required": {":description": str},    # class name
    "optional": {":file": str, ":alias": str},
},
"ingestion": {
    "required": {":description": str},
    "optional": {":hash": str, ":alias": str},
},
```

`ingestion` is a system-only type. The single entity `:ingestion/watermark` stores the hash of the last successfully ingested commit. Agents are told in `SKILL.md` not to write to this entity directly.

Structural edges are keyword-valued and bypass schema validation by design — consistent with the existing `:calls` and `:depends-on` edges:

| Edge | Meaning |
|------|---------|
| `[:module/foo :contains :function/bar]` | module contains function/class |
| `[:module/foo :depends-on :module/baz]` | import-level dependency |
| `[:function/bar :calls :function/qux]` | call site (best-effort) |

Two new entries added to `SESSION_RULES` at startup, extending `linked`/`reachable` to `:contains`:

```
(rule [(linked ?a ?b) [?a :contains ?b]])
(rule [(reachable ?a ?b) [?a :contains ?b]])
```

---

## New MCP Tools

### `vulcan_ingest_git`

```
vulcan_ingest_git(repo_path?: str, branch?: str) → {ok: bool, job_id: str, message: str}
```

Starts a background `asyncio.Task` and returns immediately. If an ingestion is already running, returns `{ok: false, error: "ingestion already in progress"}`.

- `repo_path`: path to the git repo root. Defaults to cwd.
- `branch`: branch or ref to walk. Defaults to HEAD.
- Range determined automatically: reads `:ingestion/watermark` `:hash` from the graph; if present, starts from the next commit after the watermark; if absent, walks the full history.

### `vulcan_ingest_status`

```
vulcan_ingest_status() → {ok: bool, status: str, processed: int, total: int, current_commit: str, error?: str}
```

Returns current state from module-level `_ingest_progress` dict. `status` is one of `"idle"`, `"running"`, `"complete"`, `"error"`.

---

## Auto-Invocation at Session Start

`vulcan_ingest_git` is added to the `UserPromptSubmit` hook alongside `memory_prepare_turn` in all harness hook configs (`hooks/claude-code.json`, `hooks/codex.toml`, `hooks/hermes.yaml`). It fires at the start of every session.

Since ingestion is async and returns immediately, session start is never blocked — regardless of whether the watermark exists (incremental: only new commits) or not (first-time: full history). The background task runs concurrently with the session; the agent can call `vulcan_ingest_status` to check progress if needed.

If an ingestion is already running when the hook fires (e.g. a very long first-time ingestion that spans multiple turns), `vulcan_ingest_git` returns `{ok: false, error: "ingestion already in progress"}` — the hook treats this as a no-op.

---

## DB Locking

A module-level `_db_lock: asyncio.Lock` is introduced and acquired by **all** DB-touching operations: `vulcan_query`, `vulcan_transact`, `vulcan_retract`, `vulcan_audit`, `memory_prepare_turn`, `memory_finalize_turn`, and the ingestion background task.

The ingestion task acquires and releases the lock **per commit**:

```python
for commit in commits:
    async with _db_lock:
        # parse + transact this commit's facts
        ...
    await asyncio.sleep(0)   # yield to event loop before next commit
```

This is the same pattern as the fix in commit f6d9bde (release file lock after each tool call so `prepare_hook` can read between turns). `memory_prepare_turn` calls from the harness hook suspend on `_db_lock` until the ingestion task releases it at the end of the current commit — a wait bounded by one commit's parse + transact time (typically a few milliseconds). No timeout or failure path is needed; the wait is imperceptible in practice. The one edge case is an unusually large commit (thousands of files in a single changeset); if this becomes a problem, per-file yielding can be introduced in a follow-on change without altering the tool interface.

---

## Ingestion Pipeline

The background task executes this sequence:

### 1. Enumerate commits

```
git log --reverse --format="%H %at %ae %s" <watermark>..HEAD
```

Full history if no watermark. Total count captured upfront and stored in `_ingest_progress["total"]`.

### 2. Per-commit changed files

```
git diff-tree --no-commit-id -r --name-status <hash>
```

Returns file status (`A`dded, `M`odified, `D`eleted) and path. Only changed files are parsed — not the full tree — so cost scales with churn, not repo size.

### 3. Language detection

File extension → language name via a static dict:

```python
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".cs": "c_sharp", ".rb": "ruby", ".php": "php",
    # ...
}
```

Unknown extensions are skipped silently. Grammars loaded lazily on first use via `tree_sitter_languages.get_language(lang)` and cached in a module-level dict.

### 4. AST extraction

For each supported file, `tree_sitter.Parser` extracts:

- **Functions** — name + containing file path
- **Classes** — name + containing file path
- **Imports** — module-level import targets → `:depends-on` edges between module entities
- **Call expressions** — function name at call site → `:calls` edges (best-effort; unresolvable calls skipped)

### 5. Ident canonicalization

All idents follow Phase 4 slug rules via `_canonical_ident`. To avoid collisions between file path segments and function/class names, the value passed to `_canonical_ident` uses `::` as a separator before the name (double-colon is not a valid slug character and gets normalised to a single `-`, providing a stable separator):

```python
_canonical_ident("module", "src/auth.py")            # → ":module/src-auth-py"
_canonical_ident("function", "src/auth.py::login")   # → ":function/src-auth-py-login"
_canonical_ident("class", "src/auth.py::User")       # → ":class/src-auth-py-user"
```

The `::` separator ensures that `src/auth_login.py` (module) and `src/auth.py::login` (function) produce distinct slugs (`src-auth-login-py` vs `src-auth-py-login`).

### 6. Bi-temporal writes

All ingestion writes — both additions and deletions — carry the commit's unix timestamp as `:valid-at`. This is what makes the graph genuinely bi-temporal for code structure: the valid time records when the fact was true in the world (the commit), not when it was recorded in the graph (the ingestion run).

**Additions (A- and M-status files):**

```python
db.execute(f'(transact {facts} {{:valid-at "{commit_ts_ms}"}})')
```

Where `commit_ts_ms` is the commit's unix timestamp in milliseconds.

**Deletions (D-status files and removed functions/classes within M-status files):**

```python
db.execute(f'(retract {facts} {{:valid-at "{commit_ts_ms}"}})')
```

The retract uses the same commit timestamp so that the fact's valid window is closed at the correct wall-clock time. Without `:valid-at` on the retract, minigraf defaults to transaction time (now), which would misdatestamp the removal as happening at ingestion time rather than at the commit that deleted it.

Note: `handle_vulcan_retract` (the agent-facing MCP tool) currently calls `(retract {facts})` without `:valid-at`. The ingestion pipeline uses `db.execute()` directly with the explicit timestamp — it does not go through `handle_vulcan_retract`. Fixing `handle_vulcan_retract` to pass `:valid-at` is a separate follow-on item.

**Watermark update:**

After each commit's transact and retract calls succeed, update `:ingestion/watermark`:

```python
db.execute(f'(transact [[:ingestion/watermark :hash "{commit_hash}"]] {{:valid-at "{commit_ts_ms}"}})')
```

The watermark itself carries the commit timestamp so point-in-time queries on the watermark reflect the correct ingestion boundary.

- `reason` for all writes = `"git:<hash> <author>: <message>"`

### 8. Lock yield

```python
await asyncio.sleep(0)
```

After each commit's lock release, yield to the event loop before acquiring the lock for the next commit.

---

## Queries this Unlocks

Examples to add to `SKILL.md` as fewshots:

Point-in-time queries require both `:as-of` (transaction time) and `:valid-at` (valid time) for a correct bi-temporal view. Using only one gives a partial picture — see `mcp_server.py` `_build_query_clauses` docstring.

```datalog
; All functions in auth.py right now
[:find ?fn :valid-at "now"
 :where [:module/src-auth-py :contains ?e] [?e :description ?fn]]

; Functions that existed in auth.py at a specific commit date (wall-clock valid time)
[:find ?fn :as-of <tx-number> :valid-at "2025-03-01"
 :where [:module/src-auth-py :contains ?e] [?e :description ?fn]]

; All modules that currently depend on auth.py
[:find ?caller :valid-at "now"
 :where [?e :depends-on :module/src-auth-py] [?e :description ?caller]]

; Reachability: all modules transitively reachable from src/auth.py right now
[:find ?dep :valid-at "now"
 :where (reachable :module/src-auth-py ?d) [?d :description ?dep]]

; Cross-layer: which dependency edges appeared after the postgres decision date?
; Run two queries and diff in the application layer:
;   Q1 (before): [:find ?m ?d :valid-at "2024-12-01" :where [?e :depends-on ?f] [?e :description ?m] [?f :description ?d]]
;   Q2 (after):  [:find ?m ?d :valid-at "now"         :where [?e :depends-on ?f] [?e :description ?m] [?f :description ?d]]
;   Rows in Q2 absent from Q1 = dependencies that appeared after the decision
```

---

## Files Changed

| File | Change |
|------|--------|
| `mcp_server.py` | Add `VULCAN_SCHEMA` entries; extend `SESSION_RULES`; add `_db_lock`; add `_ingest_progress`; add ingestion pipeline functions; add `vulcan_ingest_git` and `vulcan_ingest_status` handlers |
| `tests/test_mcp_server.py` | New tests for schema, tools, pipeline, watermark, incremental ingestion, lock interleave |
| `SKILL.md` | Add `vulcan_ingest_git` / `vulcan_ingest_status` tool docs; add code-structure query fewshots; note `ingestion` entity type is system-only |
| `hooks/claude-code.json` | Add `vulcan_ingest_git` to `UserPromptSubmit` hook |
| `hooks/codex.toml` | Add `vulcan_ingest_git` to `UserPromptSubmit` hook |
| `hooks/hermes.yaml` | Add `vulcan_ingest_git` to `pre_llm_call` hook |
| `ROADMAP.md` | Mark Phase 5 in-progress |

---

## Dependencies

| Package | Reason |
|---------|--------|
| `tree-sitter` | AST parser runtime |
| `tree-sitter-languages` | Bundled compiled grammars for 100+ languages; lazy-loaded per extension |

Both added to `install.py` checks and `requirements.txt` (if present).

---

## Out of Scope

- Cross-file call resolution via type inference or import graph traversal — call edges are best-effort name matching only
- Commit entity type in `VULCAN_SCHEMA` — commits are referenced by hash in the write `reason` and as the `:ingestion/watermark` value, but are not first-class graph entities in this phase
- Post-commit git hook (`.git/hooks/post-commit`) — auto-ingesting on every commit is a follow-on configuration task; the MCP tool is the building block
- Multi-repo ingestion — single repo per MCP server session
- Binary/generated file parsing — skipped by extension detection
