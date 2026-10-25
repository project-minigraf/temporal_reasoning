"""Codex lifecycle registration and real-process memory round trip."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import install

try:
    import tomllib
except ImportError:
    from pip._vendor import tomli as tomllib


def test_installs_hooks_without_losing_existing_groups(tmp_path):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    existing = ('# keep\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n'
                'type = "command"\ncommand = "echo keep"\n')
    config.write_text(existing)
    assert install.setup_codex_config(str(tmp_path))
    first = config.read_text()
    assert first.startswith(existing)
    parsed = tomllib.loads(first)
    for event, mode, timeout in [('UserPromptSubmit', 'prepare', 30), ('Stop', 'finalize', 60)]:
        handlers = [h for g in parsed['hooks'][event] for h in g['hooks']]
        ours = [h for h in handlers if 'codex_hook.py' in h['command']]
        assert len(ours) == 1
        assert ours[0]['timeout'] == timeout
        args = shlex.split(ours[0]['command'])
        assert args == [install.VENV_PYTHON, str(Path(install.REPO_DIR) / 'hooks/codex_hook.py'),
                        mode, '--config', str(config)]
    assert install.setup_codex_config(str(tmp_path))
    assert config.read_text() == first


def test_existing_server_still_gets_hooks(tmp_path):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('[mcp_servers."temporal-reasoning"]\ncommand = "custom"\n')
    assert install.setup_codex_config(str(tmp_path))
    data = tomllib.loads(config.read_text())
    assert data['mcp_servers']['temporal-reasoning']['command'] == 'custom'
    assert set(data['hooks']) == {'UserPromptSubmit', 'Stop'}


def run_hook(config, mode, payload, cwd):
    script = Path(install.REPO_DIR) / 'hooks/codex_hook.py'
    result = subprocess.run([sys.executable, str(script), mode, '--config', str(config)],
                            input=json.dumps(payload), text=True, capture_output=True,
                            cwd=cwd, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('shape', ['response_item', 'event_msg'])
def test_codex_stop_stores_and_prepare_retrieves_from_same_graph(tmp_path, shape):
    project = tmp_path / 'project with spaces'
    project.mkdir()
    config = project / '.codex/config.toml'
    assert install.setup_codex_config(str(project))
    # Exercise a custom, relative graph and fact-index configuration.
    text = config.read_text().replace(str(project / 'memory.graph'), 'custom.graph')
    text = text.replace('MINIGRAF_EXTRACTION_STRATEGY = "heuristic"',
                        'MINIGRAF_EXTRACTION_STRATEGY = "heuristic"\nMINIGRAF_INDEX_PATH = "custom.fts.sqlite3"')
    config.write_text(text)
    prompt = 'We decided to use Redis for caching.'
    if shape == 'response_item':
        records = [
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}]}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'I will use Redis.'}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'ignored', 'arguments': '{}'}},
        ]
    else:
        records = [
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': prompt}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'I will use Redis.'}},
        ]
    transcript = project / 'rollout.jsonl'
    transcript.write_text('\n'.join(json.dumps(r) for r in records))
    subdir = project / 'nested'
    subdir.mkdir()
    assert run_hook(config, 'finalize', {'hook_event_name': 'Stop', 'transcript_path': str(transcript),
                                      'last_assistant_message': 'I will use Redis.'}, subdir) == {}
    out = run_hook(config, 'prepare', {'hook_event_name': 'UserPromptSubmit', 'prompt': 'Redis caching'}, subdir)
    assert 'Redis' in out['hookSpecificOutput']['additionalContext']
    assert out['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    assert (project / 'custom.graph').exists()
    assert (project / 'custom.fts.sqlite3').exists()
    assert not (subdir / 'memory.graph').exists()


def test_hook_bad_config_fails_open(tmp_path):
    assert run_hook(tmp_path / 'missing.toml', 'prepare', {'prompt': 'Redis'}, tmp_path) == {}


def test_latest_exchange_ignores_tool_output_and_duplicate_stream(tmp_path):
    from hooks.codex_hook import _read_transcript_delta
    records = [
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Old preference'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Old answer'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'New preference'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'New preference'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Interim answer'}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Fake preference'}},
    ]
    transcript = tmp_path / 'rollout.jsonl'
    transcript.write_text('\n'.join(json.dumps(r) for r in records) + '\n{"partial":')
    assert _read_transcript_delta(transcript, 'Final answer') == 'User: New preference\nAssistant: Final answer'
    assert _read_transcript_delta(None, 'Final answer') == ''


@pytest.mark.parametrize('server', ['command = "python"\nenabled = false', 'url = "https://example.invalid/mcp"'])
def test_disabled_or_remote_server_does_not_create_local_memory(tmp_path, server):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('[mcp_servers."temporal-reasoning"]\n' + server)
    assert run_hook(config, 'prepare', {'prompt': 'Redis'}, tmp_path) == {}
    assert not (tmp_path / 'memory.graph').exists()


def test_existing_json_hook_is_not_duplicated(tmp_path):
    assert install.setup_codex_config(str(tmp_path))
    config = tmp_path / '.codex/config.toml'
    text = config.read_text()
    data = tomllib.loads(text)
    (config.parent / 'hooks.json').write_text(json.dumps({'hooks': data['hooks']}))
    without_inline = text[:text.index('[[hooks.')]
    config.write_text(without_inline)
    assert install.setup_codex_config(str(tmp_path))
    assert config.read_text() == without_inline


def test_invalid_hooks_do_not_overwrite_configuration(tmp_path):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('hooks = "invalid"')
    assert not install.setup_codex_config(str(tmp_path))
    assert config.read_text() == 'hooks = "invalid"'


def test_newest_user_wins_when_rollout_formats_change(tmp_path):
    from hooks.codex_hook import _read_transcript_delta
    transcript = tmp_path / 'rollout.jsonl'
    records = [
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Old Redis decision'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'New SQLite decision'}},
    ]
    transcript.write_text('\n'.join(json.dumps(r) for r in records))
    assert _read_transcript_delta(transcript, 'SQLite it is') == 'User: New SQLite decision\nAssistant: SQLite it is'


@pytest.mark.parametrize('explicit', [False, True])
def test_hook_respects_forwarded_paths_and_explicit_env_precedence(tmp_path, monkeypatch, explicit):
    from hooks.codex_hook import _configure
    import os
    monkeypatch.chdir(tmp_path)
    # monkeypatch restores environment values that _configure mutates.
    monkeypatch.setenv('MINIGRAF_GRAPH_PATH', str(tmp_path / 'forwarded.graph'))
    monkeypatch.setenv('MINIGRAF_INDEX_PATH', str(tmp_path / 'forwarded.sqlite3'))
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    text = ('[mcp_servers."temporal-reasoning"]\ncommand = "python"\n'
            'env_vars = ["MINIGRAF_GRAPH_PATH", "MINIGRAF_INDEX_PATH"]\n')
    if explicit:
        text += '[mcp_servers."temporal-reasoning".env]\nMINIGRAF_GRAPH_PATH = "explicit.graph"\nMINIGRAF_INDEX_PATH = "explicit.sqlite3"\n'
    config.write_text(text)
    assert _configure(config)
    assert os.environ['MINIGRAF_GRAPH_PATH'] == ('explicit.graph' if explicit else str(tmp_path / 'forwarded.graph'))
    assert os.environ['MINIGRAF_INDEX_PATH'] == ('explicit.sqlite3' if explicit else str(tmp_path / 'forwarded.sqlite3'))
