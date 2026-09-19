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
            # No explicit open: handle_memory_prepare_turn answers from the
            # sqlite fact index -- memory facts and the navigation nudge's
            # "is this graph ingested?" gate alike (#353) -- and never opens
            # the graph. The one exception is an index that needs backfill,
            # which is rebuilt from the graph under a lease released before
            # it returns (#255), once per index lifetime.
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
