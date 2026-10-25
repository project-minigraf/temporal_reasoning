# Temporal Reasoning Repository

Persistent bi-temporal graph memory for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints alongside code structure ingested from git history.

## Architecture

```
[ Agent (Claude Code / OpenCode / Codex) ]
        ↓  MCP tool calls, per-turn hooks
[ mcp_server.py ]                   ← this repo; persistent stdio MCP server
        ↓  MiniGrafDb Python binding
[ minigraf package ]                ← pip dependency, embedded engine (no CLI)
        ↓
[ memory.graph + memory.graph.fts.sqlite3 ]
```

There is no `minigraf` CLI on `PATH`, and no `minigraf.py` wrapper in this repo. `minigraf` is an embedded engine reached through its Python binding, and `mcp_server.py` is the only thing that holds a handle to it.

## Dependencies

- **minigraf >= 2.0.0, < 3.0.0** — pip-installed by `install.py` into the project venv. `pyproject.toml` is canonical; `install.py` (`_MINIGRAF_SPEC`) and `skill.json` mirror it. The upper bound is deliberate: with no cap, CI silently resolved 2.0.0 the day it shipped (#286). A major bump must be a decision, not a resolver outcome.
- **mcp >= 1.27.0, < 2.0.0** — capped until `mcp_server.py` migrates off the `@server.list_tools()` handler API.
- **Python 3.10+**

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | Persistent stdio MCP server — the only runtime interface to the graph |
| `fact_index.py` | SQLite FTS5 fact index — retrieval, and the graph's only independent witness (#302) |
| `frontier_registry.py` | Per-position claim registry for the two ingestion streams |
| `report_issue.py` | GitHub issue reporter |
| `install.py` / `uninstall.py` | Setup script and its undo |
| `SKILL.md` | Skill definition — query syntax, schema, write policy |
| `skill.json`, `tools/*.json` | Portable manifest and the ten tool schemas, generated from `mcp_server._TOOLS` |
| `hooks/` | Per-harness MCP + auto-memory hook config templates |
| `docs/testing-conventions.md` | Real-backend-only test conventions |

## Install

`--harness` is required — it decides which harness's files are touched.

```bash
python install.py --harness claude-code   # or: opencode, codex
```

Run it from your project root. It creates a virtualenv, pip-installs the dependencies, syncs the skill into the harness's project-local skill directory, and — for `claude-code` — writes `.mcp.json` and `.claude/settings*.json`. For `codex`, it registers the server in project-local `.codex/config.toml` while preserving existing settings; restart Codex in the trusted project. Codex memory hooks remain explicit per-turn tool calls.

## Usage

Everything goes through the MCP tools. There are ten:

| Tool | Purpose |
|------|---------|
| `minigraf_query` | Datalog query, with `:as-of` (transaction time) and `:valid-at` (valid time) |
| `minigraf_transact` | Store facts; `reason` required |
| `minigraf_retract` | Retract facts; the original stays in history |
| `minigraf_rule` | Register a Datalog rule for the server session |
| `minigraf_audit` | Audit entities against the schema, retracting violators |
| `minigraf_ingest_git` | Ingest code structure from git history (background task) |
| `minigraf_ingest_status` | Poll ingestion progress |
| `memory_prepare_turn` | Retrieve relevant context before a turn |
| `memory_finalize_turn` | Extract and store facts after a turn |
| `minigraf_report_issue` | File a structured GitHub issue |

```
minigraf_transact(
    facts='[[:decision/cache-strategy :description "use Redis"]]',
    reason="Architecture decision for low-latency caching")

minigraf_query(datalog='[:find ?d :where [?e :description ?d]]')
```

To read a graph outside the server, open a `MiniGrafDb` directly — subject to the single-handle invariant (see `CLAUDE.md`):

```python
from minigraf import MiniGrafDb

db = MiniGrafDb.open("memory.graph")
print(db.execute('(query [:find ?d :where [?e :description ?d]])'))
```

## Key Conventions

- **QUERY before answering**: always query memory before answering questions about past decisions, architecture, dependencies
- **TRANSACT with reason**: every write includes a reason explaining why it's worth keeping
- **Only store durable facts**: decisions, architecture, dependencies, constraints, user preferences — not transient observations
- **Use registered entity types**: `:decision/`, `:preference/`, `:constraint/`, `:dependency/` for hand-written facts; `:module/`, `:function/`, `:class/`, `:variable/`, `:field/` for ingested code structure; `:commit/`, `:tag/`, `:ingestion/` are system-only. Anything else is rejected as an unknown type — there is no `:component/`, `:file/`, `:arch/`, `:user/`, `:task/` or `:fact/`.
- **`:description` is the required attribute** on every type; `:rationale`, `:date` and `:alias` are the optional ones. `:name` is not a registered attribute and `minigraf_audit` retracts entities carrying it.

See `SKILL.md` for the full schema, the git-ingested code-structure types, and the query reference.
