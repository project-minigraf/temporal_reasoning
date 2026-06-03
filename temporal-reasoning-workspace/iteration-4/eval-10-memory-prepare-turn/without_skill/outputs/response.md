# Eval 10 — Without Skill Response

## Tool calls (if any)

1. `ls /home/aditya/workspaces/pycharm/temporal_reasoning/` — listed project root to understand structure
2. Read first 60 lines of `mcp_server.py` — identified imports and framework
3. Read `pyproject.toml` — confirmed dependencies

## Response to user

The API layer uses the **MCP (Model Context Protocol)** framework, specifically the `mcp` Python library (version `>=1.27.0` as declared in `pyproject.toml`).

In `mcp_server.py`, the server is built using:

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
```

It runs as a persistent stdio MCP server — meaning it communicates with MCP clients (like Claude Code) over standard input/output using the MCP protocol. There is no HTTP REST or GraphQL layer; all tool calls come in through the MCP stdio transport.
