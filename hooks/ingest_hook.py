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
    import os
    try:
        import asyncio
        from pathlib import Path
        import mcp_server
        mcp_server.open_db()
        # Run ingestion directly — the hook's 2-second timeout will kill the process
        # if it takes too long (acceptable for large first-time ingestion; fast for incremental)
        asyncio.run(mcp_server._run_ingestion(str(Path.cwd()), "HEAD"))
    except Exception:
        pass  # Never block the turn on ingestion errors

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
