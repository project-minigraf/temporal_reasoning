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
            # This hook opens its own MiniGrafDb handle to the same .graph file that the
            # persistent MCP server process also holds open. minigraf uses file-level locking
            # for writes; concurrent reads are safe. The prepare hook is read-only, so no
            # write conflict occurs here. finalize_hook.py writes — it waits on the lock.
            mcp_server.open_db()
            context = mcp_server.handle_memory_prepare_turn(prompt)
        except Exception:
            pass  # Never block the turn on memory errors

    print(json.dumps({"continue": True, "additionalContext": context}))


if __name__ == "__main__":
    main()
