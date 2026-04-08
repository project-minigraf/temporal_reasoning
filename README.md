# Temporal Reasoning

Persistent bi-temporal graph memory for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.

## Problem Scope

This skill solves a specific problem: **AI coding agents forget context between conversations**.

What it does:
- **Stores** architecture decisions, constraints, and preferences
- **Queries** past state with temporal awareness
- **Persists** memory across sessions

What it is NOT:
- A general-purpose database
- A replacement for version control
- A code search tool

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   AI Coding Agent                        │
│              (Claude Code, OpenCode, Codex)            │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Python Skill Layer                          │
│         (minigraf_tool.py - this repo)                  │
│   - query(), transact() functions                     │
│   - CLI and HTTP modes                                 │
│   - Backup/restore utilities                           │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Minigraf CLI (>= 0.18.0)                   │
│         (https://github.com/adityamukho/minigraf)       │
│   - Bi-temporal Datalog database                      │
│   - Transaction time + Valid time                      │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│              Graph File                                  │
│     ~/.local/share/temporal-reasoning/memory.graph      │
│     (persistent, user-specific)                        │
└─────────────────────────────────────────────────────────┘
```

## Install

```bash
# Install minigraf (requires Rust)
cargo install minigraf

# Or use pip (if available)
pip install temporal-reasoning

# Run setup
python install.py
```

### Install In Agent Environments

Claude Code / Codex:
- Install the local skill from this repository as `temporal-reasoning`.
- Use [SKILL.md](<PROJECT_ROOT>/SKILL.md) and [skill.json](<PROJECT_ROOT>/skill.json) as the primary skill files.

OpenCode:
- Run `python install.py` from the repository root.
- This syncs the skill into `.opencode/skills/temporal-reasoning`.

If manual installation is required, include:
- [SKILL.md](<PROJECT_ROOT>/SKILL.md)
- [skill.json](<PROJECT_ROOT>/skill.json)
- [tools/query.json](<PROJECT_ROOT>/tools/query.json)
- [tools/transact.json](<PROJECT_ROOT>/tools/transact.json)
- [tools/report_issue.json](<PROJECT_ROOT>/tools/report_issue.json)

## Quick Start

```python
from minigraf_tool import query, transact

# Store a decision
transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]", 
         reason="Architecture decision for low-latency caching")

# Query decisions
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Storage Location

Default: `~/.local/share/temporal-reasoning/memory.graph`

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

## HTTP Server Mode

An optional Axum HTTP server (`minigraf_server.rs`) exposes the same query/transact API over HTTP.

```bash
# Build and run the server (requires Rust)
rustc minigraf_server.rs -o minigraf_server  # or compile via Cargo
./minigraf_server
# Listens on http://127.0.0.1:8080 by default

# Point the Python wrapper at it
MINIGRAF_MODE=http MINIGRAF_HTTP_URL=http://localhost:8080 python ...
```

Environment variables for the server:

| Variable | Default | Description |
|---|---|---|
| `MINIGRAF_HTTP_ADDR` | `127.0.0.1:8080` | Bind address and port |
| `MINIGRAF_GRAPH_PATH` | `/tmp/minigraf_memory.graph` | Graph file path |
| `MINIGRAF_BIN` | `minigraf` | Path to minigraf binary |

## Files

| File | Purpose |
|------|---------|
| `minigraf_tool.py` | Python CLI wrapper |
| `minigraf_server.rs` | Axum HTTP server |
| `report_issue.py` | GitHub issue reporter |
| `install.py` | Setup script |
| `pyproject.toml` | Python packaging |
| `tools/*.json` | Tool schemas |
| `prompts/*.txt` | Behavioral prompts |
| `tests/test_harness.py` | Validation tests |

## Tools

- **minigraf_query** — Query memory with Datalog
- **minigraf_transact** — Store facts (reason required)
- **minigraf_report_issue** — File GitHub issues

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# Temporal query (state at transaction N)
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Aggregation
query("[:find (count ?e) :where [?e :decision/description ?d]]")
```

## Cross-Session Evaluation

The repository includes a deterministic evaluation showing that persisted memory
changes behavior in a later session without restating the original context.

Run:

```bash
pytest tests/test_harness.py -q
```

Success means the harness demonstrates all of the following against the same
graph file:
- A decision is stored in an earlier session.
- A later session answers a cache-strategy question using that persisted
  decision.
- A later session derives an action-oriented plan from the same persisted
  decision.

This evaluation is intentionally local and deterministic. It does not depend on
live model output, so it is suitable as repeatable evidence for the skill's
cross-session usefulness claim.

## Usefulness Benchmarks

The harness also reports two explicit benchmark-style metrics so usefulness
claims are tied to measurable output rather than broad narrative assertions.

- Behavior consistency:
  verifies that persisted memory drives both a later answer and a later
  action-oriented plan toward the same stored decision.
- Prompt compression proxy:
  compares a short prompt that relies on memory recall with a longer prompt
  that repeats the same decision context inline.

Run:

```bash
python tests/test_harness.py
```

The prompt-compression metric uses a simple whitespace word count as a stable
local proxy for prompt size. It does not claim model-token exactness; it only
shows that recalling stored context can reduce repeated prompt text in a later
session.

## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — HTTP server, write policy, report_issue, install ✓
- **Phase 3** — WASM bindings, MCP integration (future)
