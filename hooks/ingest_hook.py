#!/usr/bin/env python3
"""
UserPromptSubmit hook — formerly triggered git ingestion at session start.

Ingestion is now auto-started by the MCP server on startup (see main() in
mcp_server.py), so this hook is a no-op kept for backward compatibility with
existing claude-code.json configurations.

Usage (hooks/claude-code.json):
  "command": "python PATH_TO_REPO/hooks/ingest_hook.py"
"""
import json


def main() -> None:
    # Ingestion is now auto-started by the MCP server on startup.
    # This hook intentionally does not open the database — doing so from a
    # subprocess that Claude Code may kill after its timeout can leave a stale
    # lock file (memory.graph.lock), blocking all subsequent DB access.
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
