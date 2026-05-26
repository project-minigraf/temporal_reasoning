#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook — inject memory context before each turn.

Claude Code calls this script with the user's message on stdin (JSON) and expects
a JSON response with optional additionalContext. The context is prepended to the
agent's working context for this turn.

Usage (hooks/claude-code.json):
  "command": "python PATH_TO_REPO/hooks/prepare_hook.py"
"""
import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        data = {}

    prompt = data.get("prompt", "")
    context = ""

    if prompt:
        try:
            import mcp_server
            # The persistent MCP server releases its DB handle (file lock) after every
            # tool call via call_tool()'s finally block. This hook fires between turns
            # when no tool call is active, so the lock is free and open_db() succeeds.
            mcp_server.open_db()
            context = mcp_server.handle_memory_prepare_turn(prompt)
        except Exception:
            pass  # Never block the turn on memory errors

    print(json.dumps({"continue": True, "additionalContext": context}))


if __name__ == "__main__":
    main()
