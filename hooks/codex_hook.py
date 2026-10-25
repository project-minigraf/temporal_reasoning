#!/usr/bin/env python3
"""Codex memory hooks, sharing the project MCP server's environment and cwd.

Codex supplies JSON on stdin. UserPromptSubmit returns additionalContext; Stop
reads the latest exchange from Codex's rollout and returns an empty JSON object.
Transcript formats are not a stable Codex API: keep the fixtures in
tests/test_codex_hooks.py in sync when supporting a new format.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _configure(config_path):
    try:
        import tomllib
    except ImportError:  # Python 3.10 installer virtualenv includes pip.
        from pip._vendor import tomli as tomllib
    config_path = Path(config_path).resolve()
    config = tomllib.loads(config_path.read_text(encoding='utf-8'))
    server = config['mcp_servers']['temporal-reasoning']
    if server.get('enabled') is False or 'url' in server:
        return False  # Do not write a local graph for a disabled/remote server.
    project = config_path.parent.parent
    cwd = Path(server.get('cwd', project))
    if not cwd.is_absolute():
        cwd = project / cwd
    os.chdir(cwd)
    forwarded = {}
    for entry in server.get('env_vars', []):
        name = entry if isinstance(entry, str) else entry.get('name')
        if isinstance(entry, dict) and entry.get('source', 'local') != 'local':
            return False  # A local hook cannot resolve a remote executor's env.
        if name in os.environ:
            forwarded[name] = os.environ[name]
    forwarded.update(server.get('env', {}))
    # Hook subprocesses inherit the client environment, not the MCP whitelist.
    for name in list(os.environ):
        if name.startswith('MINIGRAF_') and name not in forwarded:
            del os.environ[name]
    os.environ.update(forwarded)
    os.environ.setdefault('MINIGRAF_GRAPH_PATH', str(cwd / 'memory.graph'))
    os.environ.setdefault('MINIGRAF_INDEX_PATH', os.environ['MINIGRAF_GRAPH_PATH'] + '.fts.sqlite3')
    return True


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part['text'] for part in content
                         if isinstance(part, dict) and isinstance(part.get('text'), str))
    return ''


def _read_transcript_delta(path, last_assistant_message=None):
    """Read Codex event messages, falling back to response_item messages.

    Keep only the latest user exchange; tools/reasoning are never extracted.
    Both streams can describe the same exchange, so never concatenate them.
    """
    exchanges = {'event_msg': [], 'response_item': []}
    latest_user = {'event_msg': -1, 'response_item': -1}
    try:
        with open(path, encoding='utf-8') as stream:
            for position, line in enumerate(stream):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # A partial final record must not lose the turn.
                if not isinstance(record, dict):
                    continue
                kind = record.get('type')
                payload = record.get('payload', {})
                if kind not in exchanges or not isinstance(payload, dict):
                    continue
                if kind == 'event_msg':
                    role = {'user_message': 'user', 'agent_message': 'assistant'}.get(payload.get('type'))
                    text = _text(payload.get('message'))
                else:
                    if payload.get('type') != 'message':
                        continue
                    role = payload.get('role')
                    text = _text(payload.get('content'))
                if role == 'user' and text:
                    exchanges[kind] = [('user', text)]
                    latest_user[kind] = position
                elif role == 'assistant' and text and exchanges[kind]:
                    exchanges[kind].append(('assistant', text))
    except (OSError, TypeError):
        return ''
    exchange = exchanges[max(latest_user, key=latest_user.get)]
    if exchange and isinstance(last_assistant_message, str) and last_assistant_message:
        # Stop's documented field is authoritative for the final assistant text.
        exchange = [exchange[0], ('assistant', last_assistant_message)]
    return '\n'.join(f'{role.title()}: {text}' for role, text in exchange)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'finalize'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    out = {}
    try:
        data = json.load(sys.stdin)
        if _configure(args.config):
            import mcp_server
            mcp_server.use_hook_lease_deadline()
            if args.mode == 'prepare':
                prompt = data.get('prompt', '')
                context = mcp_server.handle_memory_prepare_turn(prompt) if prompt else ''
                if context:
                    out['hookSpecificOutput'] = {
                        'hookEventName': 'UserPromptSubmit', 'additionalContext': context,
                    }
            else:
                delta = _read_transcript_delta(data.get('transcript_path'), data.get('last_assistant_message'))
                if delta:
                    asyncio.run(mcp_server.handle_memory_finalize_turn(delta))
    except Exception:
        pass  # Memory failures must never block the user's turn.
    print(json.dumps(out))


if __name__ == '__main__':
    main()
