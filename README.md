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
 :where [:project/service-a :depends-on :project/service-b]
        [?d :motivated-by ?c]
        [?c :description ?reason]]

; Which modules were coupled to the payment service when we made the DB decision?
[:find ?module
 :as-of 15
 :where [?module :depends-on :service/payment]]
```

This is the only tool where both the decision and the structural change live as datoms in the same graph and can be joined in a single query. See [Phase 5](ROADMAP.md) for code structure evolution from git history.

## Why Temporal Reasoning?

Most memory tools for agents are key-value stores or vector databases. They answer "what do you know now?" Temporal Reasoning answers a harder question: **"what did you know then, and what changed?"**

**Time travel.** Every write is stamped with a transaction number. You can query the graph as it existed at any past transaction:

```python
# Decision made in session 1, transaction 3
transact('[[:project/db :name "PostgreSQL"]]', reason="Initial choice")

# Changed in session 4, transaction 11
retract('[[:project/db :name "PostgreSQL"]]', reason="Switching to CockroachDB for geo-distribution")
transact('[[:project/db :name "CockroachDB"]]', reason="Switching to CockroachDB for geo-distribution")

# Later: what did we think the database was before session 4?
query("[:find ?name :as-of 10 :where [:project/db :name ?name]]")
# → "PostgreSQL"

# What do we think now?
query("[:find ?name :where [:project/db :name ?name]]")
# → "CockroachDB"
```

**Retraction with preserved history.** Changing your mind doesn't erase the record. Retracted facts stay in the bi-temporal log and remain queryable at their original transaction time. This means the agent can always reconstruct *why* a decision changed, not just *what* the current state is.

**Exact Datalog queries, not fuzzy search.** Results are deterministic and reproducible — no embedding model, no similarity threshold, no hallucinated retrievals. A query either matches or it doesn't.

**Graph traversal.** Entities are first-class nodes — not isolated key-value blobs. Store service-calls-service as a real graph edge (`:calls :project/auth-service`) and traverse it with Datalog joins. Fixed-depth transitive queries (2-hop, 3-hop) are expressed as multi-hop joins. Rules unify multiple edge types under a single named relation.

**Local and offline.** A single binary and a file. No API key, no network dependency, no cloud service to go down.

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

### Claude Code (plugin — recommended)

Add to your Claude Code `settings.json`:

```json
"extraKnownMarketplaces": {
  "temporal-reasoning": {
    "source": {
      "source": "git",
      "url": "https://github.com/project-minigraf/temporal_reasoning"
    }
  }
}
```

Then enable the `temporal-reasoning` plugin in Claude Code. Once enabled, run once from your project root to finish setup:

```bash
python /path/to/temporal_reasoning/install.py
```

`install.py` installs the `minigraf` pip package, which fetches the platform-appropriate binary automatically (Linux x86_64/aarch64, macOS arm64/x86_64, Windows).

### Manual install

```bash
git clone https://github.com/project-minigraf/temporal_reasoning
```

Then, from your project root:

```bash
cd /your/project
python /path/to/temporal_reasoning/install.py
```

This writes `.mcp.json` and `.claude/settings*.json` into your project directory.

### OpenCode

```bash
python install.py
```

This syncs the skill into `.opencode/skills/temporal-reasoning`.

## Quick Start

```python
from minigraf import query, transact

# Store a decision
transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]", 
         reason="Architecture decision for low-latency caching")

# Query decisions
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Storage Location

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

## Per-Turn Auto-Memory

When running under Claude Code with the hook configuration in `hooks/claude-code.json`, the system automatically injects relevant memory context before each turn and extracts durable facts after each turn — without the agent explicitly calling any tool.

### Prepare phase (before the turn)

`prepare_hook.py` fires on the `UserPromptSubmit` event. It:

1. Extracts candidate entity tokens from the user's message (stop-word filtered, minimum 4 characters).
2. Queries the graph for facts whose values contain those tokens, using `:valid-at` set to the current UTC timestamp so only currently-valid facts are returned.
3. Falls back to a broad scan (capped by `VULCAN_PREPARE_SCAN_LIMIT`, default 50 rows) when no entity-specific results are found.
4. Returns the results as `additionalContext` prepended to the agent's working context for that turn.

For messages containing temporal signals (e.g. "before", "last week", "as of") with an explicit ISO date, `:valid-at` is set to that date instead (midnight UTC), enabling point-in-time recall.

### Finalize phase (after the turn)

`finalize_hook.py` fires on the `Stop` event. It reads the last user+assistant exchange from the transcript, then runs the configured extraction strategy:

| Strategy | Behaviour |
|----------|-----------|
| `heuristic` (default) | Regex patterns detect decision-signal phrases ("we'll use X", "decided to use X", "always use X", "depends on X", …) and transact the matched tokens as `:decision/`, `:preference/`, `:constraint/`, or `:dependency/` entities. |
| `llm` | Sends the exchange to a lightweight Claude model (`claude-haiku-4-5-20251001` by default) with a structured prompt. The model returns a Datalog `transact` expression; an optional `; valid-at: YYYY-MM-DD` comment sets the fact's valid time. Falls back to the `agent` strategy on error. |
| `agent` | Uses MCP sampling to ask the connected agent itself for a memory block in the same Datalog format. |

### Configuration

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `VULCAN_EXTRACTION_STRATEGY` | `heuristic` | Finalize strategy: `heuristic`, `llm`, or `agent` |
| `VULCAN_PREPARE_SCAN_LIMIT` | `50` | Max rows returned by the broad fallback scan in the prepare phase |
| `VULCAN_LLM_MODEL` | `claude-haiku-4-5-20251001` | Model used when `VULCAN_EXTRACTION_STRATEGY=llm` |
| `ANTHROPIC_API_KEY` | — | Required when `VULCAN_EXTRACTION_STRATEGY=llm` and using a Claude model |
| `OPENAI_API_KEY` | — | Required when `VULCAN_EXTRACTION_STRATEGY=llm` and `VULCAN_LLM_MODEL` is an OpenAI model (e.g. `gpt-4o-mini`) |
| `MINIGRAF_GRAPH_PATH` | `memory.graph` | Override the graph file location |

## Files

| File | Purpose |
|------|---------|
| `mcp_server.py` | Persistent stdio MCP server — primary interface to the graph |
| `minigraf.py` | Python CLI wrapper (direct use outside MCP) |
| `hooks/prepare_hook.py` | Claude Code UserPromptSubmit hook — injects memory context |
| `hooks/ingest_hook.py` | Claude Code UserPromptSubmit hook — triggers background git ingestion |
| `hooks/finalize_hook.py` | Claude Code Stop hook — extracts and stores facts |
| `hooks/claude-code.json` | Hook + MCP configuration for Claude Code |
| `report_issue.py` | GitHub issue reporter |
| `install.py` | Setup script |
| `pyproject.toml` | Python packaging |
| `tools/*.json` | Tool schemas |

## Tools

- **minigraf_query** — Query memory with Datalog
- **minigraf_transact** — Store facts (reason required)
- **minigraf_retract** — Retract facts (original stays in history)
- **minigraf_report_issue** — File GitHub issues
- **memory_prepare_turn** — Retrieve relevant context for the current user message
- **memory_finalize_turn** — Extract and store memorable facts after a turn
- **minigraf_audit** — Audit all entities against the schema; retracts violators (history preserved)
- **minigraf_ingest_git** — Ingest code structure from git history into the bi-temporal graph (background task)
- **minigraf_ingest_status** — Poll progress of a running git ingestion; reports wall-clock time and final commit hash of the last completed run (including hook-driven ingestion)

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# Temporal query (state at transaction N)
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Aggregation
query("[:find (count ?e) :where [?e :decision/description ?d]]")

# Single-hop graph traversal — what does api-gateway call?
query("[:find ?name :where [:project/api-gateway :calls ?svc] [?svc :name ?name]]")

# Two-hop join — transitive impact: what depends on key-store (directly or via one intermediate)?
query("""[:find ?svc
          :where [?mid :depends-on :project/key-store]
                 [?svc :depends-on ?mid]]""")

# Decision traceability — why did we choose asyncio?
query("[:find ?reason :where [:decision/asyncio-choice :motivated-by ?c] [?c :description ?reason]]")

# Typed entity query — list all stored components
query("[:find ?name :where [?e :entity-type :type/component] [?e :name ?name]]")
```

## Skill Benchmarks

Seven evals measure how the skill changes behavior versus a no-skill baseline. Each eval is seeded with a specific memory state and tests a distinct capability.

| Eval | What it tests | With Skill | Without Skill |
|------|--------------|-----------|---------------|
| Decision storage | Persists architectural decisions with correct naming + reasons | 6/6 | 0/6 |
| Populated retrieval | Queries memory and cites stored facts by name | 5/5 | 0/5 |
| Cross-session preference | Discovers and applies a constraint never stated in the current conversation | 4/4 | 0/4 |
| Conflict detection | Surfaces architectural conflicts before silently overriding decisions | 4/4 | 0/4 |
| Entity reference storage | Stores relationships as traversable graph edges, not dead-end strings | 5/5 | 0/5 |
| Transitive impact analysis | Traverses a multi-hop dependency chain to find all affected services | 5/5 | 0/5 |
| Decision traceability | Follows a `:motivated-by` edge to surface the constraint behind a decision | 5/5 | 0/5 |
| **Total** | | **34/34 (100%)** | **0/34 (0%)** |

The cross-session preference eval is the most discriminating for memory recall: the prompt says "make sure it fits with how we do things" with no hint that a relevant constraint exists. The skill queries memory, finds a stored no-mocks preference, and writes a test using real database connections.

The transitive impact eval is the most discriminating for graph traversal: given "key-store is being replaced — what breaks?" the skill executes a 2-hop Datalog join and returns a full impact chain; without it, the agent correctly admits it cannot name the affected services.

See [`evals/benchmark.md`](evals/benchmark.md) for full results and per-eval breakdowns.

## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — Write policy, report_issue, install, skill benchmarks ✓
- **Phase 3** — MCP server, per-turn auto-memory hooks ✓
- **Phase 4** — Entity normalization, schema-aware extraction, minigraf_audit ✓
- **Phase 5** — Code structure ingestion from git history, minigraf_ingest_git ✓
- **Phase 6** — Observability and trust for automatic memory (planned)
