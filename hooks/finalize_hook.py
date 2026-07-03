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
            # get_db() retries with backoff and self-heals a stale lock left by
            # a crashed background-ingestion or hook subprocess.
            mcp_server.get_db()
            asyncio.run(mcp_server.handle_memory_finalize_turn(conversation_delta))
        except Exception:
            pass  # Never block on memory errors

    print(json.dumps({}))


if __name__ == "__main__":
    main()
