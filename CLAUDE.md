# Temporal Reasoning — AI Coding Agent Memory

Temporal Reasoning provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill
python install.py

# Use in code
from minigraf import query, transact

transact("[[:decision/cache :decision/description \"use Redis\"]]", reason="Caching strategy")
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Key Files

- `mcp_server.py` - Persistent MCP server (primary interface)
- `minigraf.py` - Python wrapper for direct use outside MCP
- `SKILL.md` - Skill definition with all query syntax
- `install.py` - Setup script (runs weekly updates)
- `hooks/claude-code.json` - Claude Code MCP + auto-memory hook config

## Graph Storage

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

## Claude Code Plugin Publishing

The plugin is published via a stub architecture — `install.py` handles all registration automatically.

**Why a stub:** Claude Code's internal copier (`mc$()`) copies the plugin source tree to a versioned cache. REPO_DIR contains `.venv/` (hundreds of MB), causing the copy to fail silently. `install.py` builds a minimal stub at `~/.claude/plugins/stubs/temporal-reasoning-local/` containing only `.claude-plugin/` and `skills/`, which `mc$()` can copy successfully.

**Five files that must be correct** (all written by `install.py`):

1. `~/.claude/plugins/stubs/…/.claude-plugin/marketplace.json` — must have `owner` field; plugin `source: "./"`
2. `~/.claude/plugins/stubs/…/.claude-plugin/plugin.json` — plugin identity and version
3. `~/.claude/settings.json` — `enabledPlugins` + `extraKnownMarketplaces` → stub dir
4. `~/.claude/plugins/installed_plugins.json` — `installPath` → versioned cache dir
5. `~/.claude/plugins/known_marketplaces.json` — **authoritative store**; `source.path` and `installLocation` → stub dir (settings.json changes don't propagate here automatically)

**Version bumps:** canonical version lives in `.claude-plugin/plugin.json`; `install.py` reads it via `PLUGIN_VERSION`. Stale versioned cache dirs are deleted on each run.

**Diagnosing failures:** `claude plugin list` shows per-plugin status and errors. "Plugin X not found in marketplace Y" means marketplace.json failed validation — check the `owner` field and run `claude plugin validate <stub-dir>`.

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# With temporal
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Count
query("[:find (count ?e) :where [?e :decision/description ?d]]")
```
