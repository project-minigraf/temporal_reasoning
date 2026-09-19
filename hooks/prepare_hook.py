#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook — inject memory context before each turn.

Claude Code calls this script with the user's message on stdin (JSON). Any
memory context goes out as ``hookSpecificOutput.additionalContext`` with
``hookEventName: "UserPromptSubmit"``, which Claude Code adds to the model's
context for this turn. It must be nested there: a top-level
``additionalContext`` is silently dropped, and this hook emitted exactly that
shape until #344, so its context never reached the model at all.

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

    prompt = data.get("message", "") or data.get("prompt", "")
    context = ""

    if prompt:
        try:
            import mcp_server
            # No explicit open: on the common path, handle_memory_prepare_turn
            # takes NO lease at all -- it answers from the sqlite fact index
            # (fact_index.query_facts), never opening the graph. It only
            # takes a lease (db_lease(), released before it returns so the
            # next turn's hook process can acquire the file lock) when the
            # prompt looks navigation-shaped and the nav-nudge check runs
            # (#255).
            context = mcp_server.handle_memory_prepare_turn(prompt)
        except Exception:
            pass  # Never block the turn on memory errors

    out = {"continue": True}
    if context:
        out["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
