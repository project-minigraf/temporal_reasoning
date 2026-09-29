#!/usr/bin/env python3
"""
Claude Code Stop hook — extract and store facts after each turn.

Claude Code calls this script after the agent stops responding. The hook reads
the transcript to reconstruct the last user+assistant exchange, then calls
memory_finalize_turn to extract and store durable facts.

Usage (hooks/claude-code.json):
  "command": "python PATH_TO_REPO/hooks/finalize_hook.py"
"""
import asyncio
import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)


def _read_transcript_delta(transcript_path: str) -> str:
    """Read the last user+assistant exchange from the JSONL transcript."""
    try:
        with open(transcript_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
    except Exception:
        return ""

    delta_parts = []
    for msg in reversed(lines):
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        delta_parts.append(f"{role.title()}: {content}")
        if len(delta_parts) >= 2:
            break

    return "\n".join(reversed(delta_parts))


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        data = {}

    transcript_path = data.get("transcript_path", "")
    conversation_delta = _read_transcript_delta(transcript_path) if transcript_path else ""

    if conversation_delta:
        try:
            import mcp_server
            # Poll back to back to a deadline instead of the server's gapped
            # retry schedule, or ingestion's 0.1 s lease-window releases are
            # missed and this turn's facts are silently lost (#366).
            mcp_server.use_hook_lease_deadline()
            # No explicit open: handle_memory_finalize_turn takes its own
            # lease (db_lease_async(), conditional on
            # MINIGRAF_EXTRACTION_STRATEGY), which carries the same
            # retry/backoff, and releases when it returns so the next
            # turn's hook process can acquire the file lock (#255). The
            # stale-lock self-heal this used to mention was deleted in #284:
            # minigraf's lock is now held by the kernel and released on
            # process exit, so there is nothing stale left to heal.
            asyncio.run(mcp_server.handle_memory_finalize_turn(conversation_delta))
        except Exception:
            pass  # Never block on memory errors

    print(json.dumps({}))


if __name__ == "__main__":
    main()
