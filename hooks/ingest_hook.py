#!/usr/bin/env python3
"""
UserPromptSubmit hook — trigger git ingestion at session start.

Calls vulcan_ingest_git which starts a background asyncio task and returns
immediately. If ingestion is already running (from a previous turn), returns
ok=false which is treated as a no-op.

Usage (hooks/claude-code.json):
  "command": "python PATH_TO_REPO/hooks/ingest_hook.py"
"""
import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)


def main() -> None:
    try:
        import asyncio
        import mcp_server
        mcp_server.open_db()
        # vulcan_ingest_git is async — run it in a new event loop
        asyncio.run(mcp_server.handle_vulcan_ingest_git())
    except Exception:
        pass  # Never block the turn on ingestion errors

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
