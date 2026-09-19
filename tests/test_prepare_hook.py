"""hooks/prepare_hook.py's stdout contract with Claude Code (#344).

Claude Code honours injected context for UserPromptSubmit ONLY under
``hookSpecificOutput.additionalContext`` with ``hookEventName`` set. A
top-level ``additionalContext`` is dropped by its validator without a trace in
the session transcript, so the hook shipped that shape for months while
``memory_prepare_turn`` never reached the model once.

These tests run the hook as Claude Code does -- a separate process fed JSON on
stdin -- against a real graph seeded by another real process, and assert the
exact JSON it prints. Nothing is mocked: the in-process handler was always
correct, and the defect lived entirely in the envelope.
"""
import json
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_DIR, "hooks", "prepare_hook.py")


def _env(graph_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGRAF_")}
    env["MINIGRAF_GRAPH_PATH"] = str(graph_path)
    env["MINIGRAF_NO_AUTO_INGEST"] = "1"
    return env


def _seed(graph_path, facts):
    script = (
        "import sys\n"
        f"sys.path.insert(0, {REPO_DIR!r})\n"
        "import mcp_server\n"
        f"r = mcp_server.handle_minigraf_transact({facts!r}, 'seed')\n"
        "assert r.get('ok'), r\n"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        env=_env(graph_path), check=True, capture_output=True, text=True, timeout=120,
    )


def _run_hook(graph_path, prompt):
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}),
        env=_env(graph_path), check=True, capture_output=True, text=True, timeout=120,
    )
    return json.loads(proc.stdout)


def test_matching_prompt_injects_context_under_hook_specific_output(tmp_path):
    graph = tmp_path / "memory.graph"
    _seed(graph, '[[:decision/use-redis :description "use redis for caching"]]')

    out = _run_hook(graph, "redis caching")

    # Positive control first: a hook that found nothing would pass the shape
    # assertions below vacuously, having nothing to nest.
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "use redis for caching" in ctx, out
    assert out == {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        },
    }


def test_unmatched_prompt_emits_no_context_key_at_all(tmp_path):
    graph = tmp_path / "memory.graph"
    _seed(graph, '[[:decision/use-redis :description "use redis for caching"]]')

    assert _run_hook(graph, "elephants trombone") == {"continue": True}
