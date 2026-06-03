# Eval 10 — Without Skill
## Tool calls
- Glob: **/*.py
- Glob: **/requirements*.txt
- Read: mcp_server.py (first 50 lines)
- Glob: **/pyproject.toml
- Read: pyproject.toml

## Response to user
The API layer uses the **MCP (Model Context Protocol)** framework. Specifically, the project depends on `mcp>=1.27.0` (from `pyproject.toml`) and the server is implemented using `mcp.server.Server` and `mcp.server.stdio.stdio_server` (visible in `mcp_server.py`).

MCP is a stdio-based protocol for exposing tools to AI coding agents. The server runs as a persistent stdio MCP server, and all interactions (queries, transactions, ingestion, etc.) are exposed as MCP tools.
