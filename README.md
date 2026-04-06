# Temporal Reasoning

Persistent bi-temporal graph memory skill for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.

## Quick Start

```python
from minigraf_tool import query, transact

transact("[[:decision/cache-strategy :decision/description \"use Redis\"]]", reason="Architecture decision")
result = query("[:find ?desc :where [?e :decision/description ?desc]]")
```

## Architecture

```
[ Agent (Claude Code / OpenCode / Codex) ]
        ↓
[ Python Skill Layer ]
        ↓
[ Minigraf CLI ] (>= 0.13.0)
        ↓
[ .graph file on disk ]
```

## Install

```bash
# Install minigraf
cargo install --git https://github.com/adityamukho/minigraf

# Run setup (checks dependencies, syncs skill from GitHub)
python install.py
```

## Starting a Session

```bash
# Before starting work, run install.py to check for updates
python install.py

# Then start OpenCode
opencode .
```

**Note:** `install.py` checks for updates weekly. Run it manually to force an immediate update.

## Files

| File | Purpose |
|------|---------|
| `minigraf_tool.py` | Python CLI wrapper |
| `minigraf_server.rs` | Axum HTTP server (Phase 2) |
| `report_issue.py` | GitHub issue reporter (Phase 2) |
| `install.py` | One-command setup (Phase 2) |
| `tools/*.json` | Tool schemas |
| `prompts/*.txt` | Behavioral prompts |
| `tests/test_harness.py` | Validation tests |

## Tools

- **minigraf_query** — Query memory with Datalog
- **minigraf_transact** — Store facts (reason required)
- **minigraf_report_issue** — File GitHub issues on failures

## Phases

- **Phase 1** — Python skill layer ✓
- **Phase 2** — HTTP server, write policy, report_issue, install ✓
- **Phase 3** — WASM bindings, MCP integration (future)
