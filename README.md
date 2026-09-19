# Temporal Reasoning

**Perfect memory. Exact reasoning. Complete history.**

Temporal Reasoning gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.

## Questions Only Temporal Reasoning Can Answer

These queries are impossible with git log, vector search, or key-value memory:

```datalog
; What did the dependency graph look like before the auth refactor?
[:find ?caller ?callee
 :as-of 30
 :where [?caller :calls ?callee]]

; When did this coupling first appear — and what decision caused it?
[:find ?reason
 :where [:dependency/service-a :depends-on :dependency/service-b]
        [?d :motivated-by ?c]
        [?c :description ?reason]]

; Which modules were coupled to the payment service when we made the DB decision?
[:find ?desc
 :as-of 15
 :where [?module :depends-on :dependency/payment]
        [?module :description ?desc]]
```

This is the only tool where both the decision and the structural change live as datoms in the same graph and can be joined in a single query. See [Phase 5](ROADMAP.md) for code structure evolution from git history.

## Why Temporal Reasoning?

Most memory tools for agents are key-value stores or vector databases. They answer "what do you know now?" Temporal Reasoning answers a harder question: **"what did you know then, and what changed?"**

**Time travel.** Every write is stamped with a transaction number. You can query the graph as it existed at any past transaction:

```
# Decision made in session 1, transaction 3
minigraf_transact(facts='[[:decision/db :description "PostgreSQL"]]',
                  reason="Initial choice")

# Changed in session 4, transaction 11
minigraf_retract(facts='[[:decision/db :description "PostgreSQL"]]',
                 reason="Switching to CockroachDB for geo-distribution")
minigraf_transact(facts='[[:decision/db :description "CockroachDB"]]',
                  reason="Switching to CockroachDB for geo-distribution")

# Later: what did we think the database was before session 4?
minigraf_query(datalog='[:find ?d :as-of 10 :where [:decision/db :description ?d]]')
# → "PostgreSQL"

# What do we think now?
minigraf_query(datalog='[:find ?d :where [:decision/db :description ?d]]')
# → "CockroachDB"
```

**Retraction with preserved history.** Changing your mind doesn't erase the record. Retracted facts stay in the bi-temporal log and remain queryable at their original transaction time. This means the agent can always reconstruct *why* a decision changed, not just *what* the current state is.

**Exact Datalog queries, not fuzzy search.** Results are deterministic and reproducible — no embedding model, no similarity threshold, no hallucinated retrievals. A query either matches or it doesn't.

**Graph traversal.** Entities are first-class nodes — not isolated key-value blobs. Store service-calls-service as a real graph edge (`:calls :dependency/auth-service`) and traverse it with Datalog joins. Fixed-depth transitive queries (2-hop, 3-hop) are expressed as multi-hop joins. Rules unify multiple edge types under a single named relation.

**Local and offline.** An embedded engine and a file on disk. No API key, no network dependency, no cloud service to go down.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        AI Coding Agent                            │
│                 (Claude Code, OpenCode, Codex)                   │
└──────────┬───────────────────────────────────────┬───────────────┘
           │ MCP tool calls                        │ per-turn hooks
           │ (minigraf_query, minigraf_transact, …)    │ (UserPromptSubmit / Stop)
           ▼                                       ▼
┌──────────────────────────┐         ┌─────────────────────────────┐
│   MCP Server             │         │   Hook scripts              │
│   mcp_server.py          │◄────────│   prepare_hook.py           │
│   (persistent stdio)     │         │   finalize_hook.py          │
└──────────┬───────────────┘         └─────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│              MiniGrafDb Python binding (minigraf package)         │
│              https://github.com/project-minigraf/minigraf              │
│   - Bi-temporal Datalog engine                                   │
│   - Transaction time + Valid time                                │
└──────────┬───────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│              Graph File                                           │
│              memory.graph  (current working directory)           │
└──────────────────────────────────────────────────────────────────┘
```

## Install

Two tiers. The difference is auto-memory, not the tool set.

| | MCP server only | Full install |
|---|---|---|
| Clone required | no | yes |
| MCP tools (`minigraf_query`, `minigraf_transact`, …) | ✓ | ✓ |
| `SKILL.md` synced into the project | — | ✓ |
| Per-turn auto-memory hooks | — | ✓ |
| Graph pinned to the project directory | — | ✓ |

### MCP server only

Point any MCP-capable agent at the published package. No clone and no virtualenv — `uvx` fetches it:

```json
{
  "mcpServers": {
    "temporal-reasoning": {
      "type": "stdio",
      "command": "uvx",
      "args": ["temporal-reasoning[git-ingestion]"]
    }
  }
}
```

`[git-ingestion]` is not decorative. A bare `uvx temporal-reasoning` resolves none of the tree-sitter packages, and code-structure extraction then silently does nothing (issue #93).

What this tier leaves out: no skill file, so the agent gets each tool's own description — one example query apiece — rather than `SKILL.md`'s full syntax, entity-type model, and write policy; no hooks, so nothing is remembered unless the agent explicitly calls a tool; and no `MINIGRAF_GRAPH_PATH`, so the graph lands at `memory.graph` in whatever directory the server happened to start in.

### Full install

`install.py` requires an explicit `--harness` so it only touches the files for the agent you're setting up: `claude-code`, `opencode`, or `codex`.

```bash
git clone https://github.com/project-minigraf/temporal_reasoning
cd /your/project
python /path/to/temporal_reasoning/install.py --harness claude-code
```

Run `install.py` from your project root. It creates a virtualenv, installs dependencies, syncs the skill into `.claude/skills/temporal-reasoning` (Claude Code's project-local skill scope), and — for `--harness claude-code` — writes `.mcp.json` and `.claude/settings*.json` into your project directory. That's it.

**Optional — LLM extraction strategy:** `install.py` defaults to heuristic (regex) extraction, which requires no API key. To use LLM-based extraction, set `MINIGRAF_EXTRACTION_STRATEGY=llm` and `ANTHROPIC_API_KEY=<your key>` in `.claude/settings.local.json` after running the script.

**Upgrading from the MCP-only tier:** clone the repo and run `install.py` on top — it rewrites the `temporal-reasoning` block in `.mcp.json` with the same `uvx` command plus explicit `MINIGRAF_GRAPH_PATH` and `MINIGRAF_INDEX_PATH`, and leaves any other MCP server in the file alone. Those paths point at `<your project>/memory.graph`, so if you were already starting the server from your project root it is the same file you were writing to; if you were not, move the old `memory.graph` there first or the existing memory is orphaned.

### OpenCode

```bash
python /path/to/temporal_reasoning/install.py --harness opencode
```

This syncs the skill into `.opencode/skills/temporal-reasoning`. OpenCode's MCP + hook wiring is manual — merge `hooks/opencode.json` into your OpenCode config (see the file for details; auto-memory hooks don't fire in OpenCode, so the agent calls `memory_prepare_turn`/`memory_finalize_turn` explicitly per `SKILL.md`).

### Codex CLI

```bash
python /path/to/temporal_reasoning/install.py --harness codex
```

This syncs the skill into `.agents/skills/temporal-reasoning` — Codex CLI's documented project-local skill scope (it scans `.agents/skills` from the current working directory up to the repository root). Codex's MCP + hook wiring is manual — merge `hooks/codex.toml` into your `config.toml`.

## Quick Start

Everything goes through the MCP tools — there is no Python wrapper module to
import. The agent calls these; you can also drive them from any MCP client.

```
# Store a decision
minigraf_transact(
    facts='[[:decision/cache-strategy :description "use Redis"]]',
    reason="Architecture decision for low-latency caching")

# Query stored descriptions
minigraf_query(datalog='[:find ?d :where [?e :description ?d]]')
```

To read the graph from your own Python without the server, use the `minigraf`
package's `MiniGrafDb` directly:

```python
from minigraf import MiniGrafDb

db = MiniGrafDb.open("memory.graph")
print(db.execute('(query [:find ?d :where [?e :description ?d]])'))
```

Only one `MiniGrafDb` handle may be live per process, and the MCP server holds
one whenever it is running — see "Single-handle invariant" in `CLAUDE.md`.

## Storage Location

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

The memory-retrieval index lives beside it at `<graph_path>.fts.sqlite3`
(override with `MINIGRAF_INDEX_PATH`). It is written from the same triples in
the same transaction boundary but by a different storage engine, which is what
lets `evals/at_scale/fact_audit.py` cross-check the graph against it. Delete
both together when starting a graph over.

## Per-Turn Auto-Memory

When running under Claude Code with the hook configuration in `hooks/claude-code.json`, the system automatically injects relevant memory context before each turn and extracts durable facts after each turn — without the agent explicitly calling any tool.

### Prepare phase (before the turn)

`prepare_hook.py` fires on the `UserPromptSubmit` event. It:

1. Extracts candidate entity tokens from the user's message (stop-word filtered, minimum 4 characters).
2. Queries the graph for facts whose values contain those tokens, using `:valid-at` set to the current UTC timestamp so only currently-valid facts are returned.
3. Falls back to a broad scan (capped by `MINIGRAF_PREPARE_SCAN_LIMIT`, default 50 rows) when no entity-specific results are found.
4. Returns the results as `hookSpecificOutput.additionalContext` (with `hookEventName: "UserPromptSubmit"`), which Claude Code adds to the agent's context for that turn. The nesting is required: a top-level `additionalContext` is silently ignored, which is how this hook's output went unused until #344.

For messages containing temporal signals (e.g. "before", "last week", "as of") with an explicit ISO date, `:valid-at` is set to that date instead (midnight UTC), enabling point-in-time recall.

### Finalize phase (after the turn)

`finalize_hook.py` fires on the `Stop` event. It reads the last user+assistant exchange from the transcript, then runs the configured extraction strategy:

| Strategy | Behaviour |
|----------|-----------|
| `heuristic` (default) | Regex patterns detect decision-signal phrases ("we'll use X", "decided to use X", "always use X", "depends on X", …) and transact the matched tokens as `:decision/`, `:preference/`, `:constraint/`, or `:dependency/` entities. |
| `llm` | Sends the exchange to a lightweight Claude model (`claude-haiku-4-5-20251001` by default) with a structured prompt. The model returns a Datalog `transact` expression; an optional `; valid-at: YYYY-MM-DD` comment sets the fact's valid time. Falls back to the `agent` strategy on error. |
| `agent` | Uses MCP sampling to ask the connected agent itself for a memory block in the same Datalog format. |

### Configuration

**Storage**

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `MINIGRAF_GRAPH_PATH` | `memory.graph` in cwd | Graph file location |
| `MINIGRAF_INDEX_PATH` | `<graph_path>.fts.sqlite3` | Fact-index location |

**Memory extraction and retrieval**

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `MINIGRAF_EXTRACTION_STRATEGY` | `heuristic` | Finalize strategy: `heuristic`, `llm`, or `agent` |
| `MINIGRAF_LLM_MODEL` | `claude-haiku-4-5-20251001` | Model used when the strategy is `llm` |
| `MINIGRAF_LLM_TIMEOUT_SECONDS` | `30` | Per-call timeout for the `llm` strategy |
| `ANTHROPIC_API_KEY` | — | Required for the `llm` strategy with a Claude model |
| `OPENAI_API_KEY` | — | Required for the `llm` strategy when `MINIGRAF_LLM_MODEL` is an OpenAI model (e.g. `gpt-4o-mini`) |
| `MINIGRAF_PREPARE_SCAN_LIMIT` | `50` | Max facts returned by the prepare phase |
| `MINIGRAF_MEMORY_BOOST` | `2.0` | Ranking boost for decision/preference/constraint/dependency facts over ingested code structure |
| `MINIGRAF_HISTORICAL_DISCOUNT` | `0.5` | Ranking discount for historical facts; below 1.0 demotes them, 1.0 is neutral |
| `MINIGRAF_MAX_FACT_VALUE_LENGTH` | `4096` | Cap on a string-valued fact; a longer value is a schema violation |

**Git ingestion**

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `MINIGRAF_NO_AUTO_INGEST` | unset | Set to `1` to suppress the ingestion that auto-starts at server boot |
| `MINIGRAF_GIT_BRANCH` | auto-detected `main`/`master` | Branch to walk, falling back to `HEAD` if neither exists |
| `MINIGRAF_INGEST_IGNORE` | — | Extra comma-separated globs/prefixes to skip, added to the defaults (see also a repo-local `.temporalignore`) |
| `MINIGRAF_INGEST_WORKERS` | `min(32, cpu_count())` | Extraction worker processes |
| `MINIGRAF_INGEST_STREAM_RATIO` | `1:1` | Commits per round for the forward:reverse walk; `1000000:1` is effectively oldest-first |
| `MINIGRAF_INGEST_CHECKPOINT_DUTY` | `0.05` | Fraction of wall clock ingestion may spend on WAL compaction |
| `MINIGRAF_INGEST_TRACE_PATH` | unset | Append one JSON object per applied commit for cost attribution |
| `MINIGRAF_OWNER_HINT_TTL` | `30.0` | Seconds before the `<graph_path>.owner` advisory hint is treated as stale |
| `MINIGRAF_MATCH_MAX_POOL` | `3000` | Cap on the rename matcher's candidate pool |

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | Persistent stdio MCP server — the only runtime interface to the graph |
| `fact_index.py` | SQLite FTS5 fact index behind `memory_prepare_turn` retrieval |
| `frontier_registry.py` | Per-position claim registry for the two ingestion streams |
| `hooks/prepare_hook.py` | Claude Code UserPromptSubmit hook — injects memory context |
| `hooks/finalize_hook.py` | Claude Code Stop hook — extracts and stores facts |
| `hooks/claude-code.json` | Hook + MCP configuration for Claude Code |
| `report_issue.py` | GitHub issue reporter |
| `install.py` / `uninstall.py` | Setup script and its undo |
| `pyproject.toml` | Python packaging |
| `skill.json`, `tools/*.json` | Portable skill manifest and the ten tool schemas, generated from `mcp_server._TOOLS` |

## Tools

- **minigraf_query** — Query memory with Datalog
- **minigraf_transact** — Store facts (reason required)
- **minigraf_retract** — Retract facts (original stays in history)
- **minigraf_rule** — Register a Datalog rule for the server session (recursive traversal)
- **minigraf_report_issue** — File GitHub issues
- **memory_prepare_turn** — Retrieve relevant context for the current user message
- **memory_finalize_turn** — Extract and store memorable facts after a turn
- **minigraf_audit** — Audit all entities against the schema; retracts violators (history preserved)
- **minigraf_ingest_git** — Ingest code structure from git history into the bi-temporal graph (background task)
- **minigraf_ingest_status** — Poll a running git ingestion: this run's work (to_retire/retired), per-stream state and rate, and whether the graph is fully visible and lineage-confirmed; when idle, reports the last completed run's time and branch tip

## Query Examples

Passed as the `datalog` argument to `minigraf_query`.

```datalog
; Basic query
[:find ?x :where [?e :attr ?x]]

; Transaction time — state as of write N
[:find ?x :as-of 5 :where [?e :attr ?x]]

; Valid time — what was true in the world on a date
[:find ?x :valid-at "2026-01-15" :where [?e :attr ?x]]

; Aggregation
[:find (count ?e) :where [?e :description ?d]]

; Single-hop graph traversal — what does api-gateway call?
[:find ?desc :where [:dependency/api-gateway :calls ?svc] [?svc :description ?desc]]

; Two-hop join — what depends on key-store, directly or via one intermediate?
[:find ?desc
 :where [?mid :depends-on :dependency/key-store]
        [?svc :depends-on ?mid]
        [?svc :description ?desc]]

; Decision traceability — why did we choose asyncio?
[:find ?reason :where [:decision/asyncio :motivated-by ?c] [?c :description ?reason]]

; Typed entity query — list every stored component
[:find ?desc :where [?e :entity-type :type/dependency] [?e :description ?desc]]
```

Entities carry `:description`, not `:name` — `:name` is not a registered
attribute on any hand-written type, so `minigraf_audit` treats it as a schema
violation and retracts the entity. The canonical types are `:type/decision`,
`:type/dependency`, `:type/constraint` and `:type/preference`; there is no
`:type/component`. See `SKILL.md` for the full schema and the git-ingested
code-structure types.

## Skill Benchmarks

Twelve evals run in isolated sandboxes measure how the skill changes behavior versus a no-skill baseline. Each eval uses a fresh graph with pre-seeded state where relevant. Latest run: **iteration-9**, 2026-09-02, on `claude-sonnet-5` (pinned).

| Eval | What it tests | With Skill | Without Skill |
|------|--------------|-----------|---------------|
| 1 — Decision storage | Persists architectural decisions with correct naming + reasons | 5/5 | 0/5 |
| 2 — Memory retrieval | Queries memory and cites stored facts by name | 5/5 | 0/5 |
| 3 — Cross-session preference | Discovers and applies a constraint never stated in the current conversation | 3/4 | 0/4 |
| 4 — Conflict detection | Surfaces architectural conflicts before silently overriding decisions | 4/4 | 0/4 |
| 5 — Entity reference storage | Stores relationships as traversable graph edges, not dead-end strings | 5/5 | 0/5 |
| 6 — Transitive impact analysis | Traverses a multi-hop dependency chain to find all affected services | 5/5 | 3/5 |
| 7 — Decision traceability | Follows a `:motivated-by` edge to surface the constraint behind a decision | 5/5 | 0/5 |
| 8 — Git ingestion | Checks status before starting ingestion; moves on without polling | 6/6 | 0/6 |
| 9 — Ingest status | Reports idle/running/complete accurately; surfaces errors | 5/5 | 2/5 |
| 10 — Memory prepare-turn | Injects relevant context before the agent responds | 5/5 | 0/5 |
| 11 — Audit | Runs the schema audit and reports what it found | 4/5 | 1/5 |
| 12 — Already running | Does not re-trigger ingestion when already in progress | 3/5 | 2/5 |
| **Total** | | **55/59 (93%)** | **8/59 (14%)** |

The cross-session preference eval is the most discriminating for memory recall: the prompt says "make sure it fits with how we do things" with no hint that a relevant constraint exists. The skill queries memory and surfaces a stored no-mocks preference the baseline never sees.

The transitive impact eval is the most discriminating for graph traversal: given "key-store is being replaced — what breaks?" the skill executes a 3-hop Datalog join and returns the full impact chain, ranked by distance from the change.

Eval 10 is the sharpest illustration of why memory matters. Asked which framework the API layer uses, the skill answers **FastAPI** from a stored decision. The baseline reads `mcp_server.py`'s imports and answers **"the MCP Python SDK"** — confident, sourced from real code, and wrong.

Eval 8's baseline is worth noting separately: with no ingestion tool it sets about building the index by hand and was killed by the harness timeout twice, at 420 s and again at 900 s, without ever answering.

Two caveats on the baseline column, both recorded in full in [`evals/benchmark.md`](evals/benchmark.md). The sandbox runs in this repository, so a baseline agent can read `evals/evals.json` — prompts, seed data and expectations alike; 5 of 11 baseline runs did, and eval 6's 3/5 is the answer key rather than traversal. And evals 3, 11 and 12 no longer fully exercise what they were written for: evals 3 and 6 presuppose an application this repo does not contain, eval 11's seed no longer carries schema violations, and eval 12's SETUP cannot establish its own "already running" precondition.

See [`evals/benchmark.md`](evals/benchmark.md) for full results, per-eval breakdowns, and the four harness defects fixed before this iteration ran.

## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — Write policy, report_issue, install, skill benchmarks ✓
- **Phase 3** — MCP server, per-turn auto-memory hooks ✓
- **Phase 4** — Entity normalization, schema-aware extraction, minigraf_audit ✓
- **Phase 5** — Code structure ingestion from git history, minigraf_ingest_git ✓
- **Phase 5.5** — Ingestion hardening: rename tracking, vendored-path ignore, async startup, persisted on-disk retrieval index ✓ (see [ROADMAP.md](ROADMAP.md) for at-scale re-validation status)
- **Phase 6** — Observability and trust for automatic memory (planned)
