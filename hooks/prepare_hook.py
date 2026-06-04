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
import time

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

# Retry parameters for acquiring the DB lock when background ingestion is active.
# Total max wait: 0.05 + 0.10 + 0.20 + 0.40 + 0.80 = 1.55s — well within the 5s timeout.
_LOCK_RETRY_MAX = 5
_LOCK_RETRY_BASE = 0.05  # seconds; doubles each attempt


def _open_db_with_retry() -> bool:
    """Open the graph DB, retrying on lock conflicts from background ingestion."""
    import mcp_server
    delay = _LOCK_RETRY_BASE
    for attempt in range(_LOCK_RETRY_MAX):
        try:
            mcp_server.open_db()
            return True
        except Exception as e:
            if "locked" in str(e).lower() and attempt < _LOCK_RETRY_MAX - 1:
                time.sleep(delay)
                delay *= 2
            else:
                return False
    return False


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        data = {}

    prompt = data.get("message", "") or data.get("prompt", "")
    context = ""

    if prompt:
        try:
            import mcp_server
            if _open_db_with_retry():
                context = mcp_server.handle_memory_prepare_turn(prompt)
        except Exception:
            pass  # Never block the turn on memory errors

    print(json.dumps({"continue": True, "additionalContext": context}))


if __name__ == "__main__":
    main()
