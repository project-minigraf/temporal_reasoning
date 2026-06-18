"""
Isolated eval runner for temporal-reasoning evals.

Each eval runs in a temporary directory with its own memory.graph,
preventing cross-contamination with the live project graph or other evals.

with_skill variant:
    claude --print --bare --output-format stream-json --verbose
           --mcp-config <isolated MCP config pointing to temp graph>
           --append-system-prompt-file <SKILL.md>
           "<prompt>"

without_skill variant:
    claude --print --bare --output-format stream-json --verbose
           "<prompt>"
    (no MCP config, no skill — clean baseline with no minigraf tools available)

Usage:
    python evals/run_isolated.py                           # all evals, both variants
    python evals/run_isolated.py --eval-id 1               # single eval, both variants
    python evals/run_isolated.py --variant with_skill      # all evals, one variant
    python evals/run_isolated.py --eval-id 1 --variant with_skill
    python evals/run_isolated.py --iteration 7             # override iteration number
    python evals/run_isolated.py --concurrency 3           # parallel eval runs
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.resolve()
EVALS_JSON = Path(__file__).parent / "evals.json"
WORKSPACE_DIR = Path(__file__).parent / "workspace"
SKILL_MD = REPO_ROOT / "skills" / "temporal-reasoning" / "SKILL.md"
MCP_SERVER_PY = REPO_ROOT / "mcp_server.py"

PYTHON = sys.executable
CLAUDE = "claude"

VARIANTS = ("with_skill", "without_skill")


# ---------------------------------------------------------------------------
# Iteration detection
# ---------------------------------------------------------------------------


def _detect_next_iteration() -> int:
    """Return the next iteration number based on existing workspace directories."""
    if not WORKSPACE_DIR.exists():
        return 1
    existing = [
        int(m.group(1))
        for d in WORKSPACE_DIR.iterdir()
        if d.is_dir() and (m := re.fullmatch(r"iteration-(\d+)", d.name))
    ]
    return (max(existing) + 1) if existing else 1


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------


def _write_isolated_mcp_config(workdir: Path, graph_path: Path) -> Path:
    """Write an MCP server config that points to an isolated memory.graph."""
    config = {
        "mcpServers": {
            "temporal-reasoning": {
                "type": "stdio",
                "command": PYTHON,
                "args": [str(MCP_SERVER_PY)],
                "env": {
                    "MINIGRAF_GRAPH_PATH": str(graph_path),
                    "MINIGRAF_EXTRACTION_STRATEGY": "heuristic",
                    # Prevent the server from auto-starting ingestion so
                    # eval-9 / ingest-status sees a clean idle state.
                    "MINIGRAF_NO_AUTO_INGEST": "1",
                },
            }
        }
    }
    config_path = workdir / "mcp_isolated.json"
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


# ---------------------------------------------------------------------------
# Graph seeding
# ---------------------------------------------------------------------------


def _seed_graph(graph_path: Path, seed_blocks: list[str]) -> None:
    """Seed an isolated memory.graph using MiniGrafDb directly."""
    # Import here to avoid hard dependency when running without the package
    sys.path.insert(0, str(REPO_ROOT))
    from minigraf import MiniGrafDb  # pylint: disable=import-outside-toplevel

    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    db = MiniGrafDb.open(str(graph_path))
    for block in seed_blocks:
        db.execute(f'(transact {block} {{:valid-from "{now_ms}"}})')
    db.checkpoint()


# ---------------------------------------------------------------------------
# Claude subprocess
# ---------------------------------------------------------------------------


def _build_command(
    prompt: str,
    variant: str,
    workdir: Path,
    graph_path: Path,
) -> list[str]:
    """Build the claude CLI command for a given variant."""
    cmd = [
        CLAUDE,
        "--print",
        "--bare",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if variant == "with_skill":
        mcp_config = _write_isolated_mcp_config(workdir, graph_path)
        cmd += ["--mcp-config", str(mcp_config)]
        cmd += ["--append-system-prompt-file", str(SKILL_MD)]
    cmd.append(prompt)
    return cmd


def _run_claude(
    prompt: str,
    variant: str,
    workdir: Path,
    graph_path: Path,
    timeout_secs: int = 300,
) -> str:
    """Run claude and return the raw stdout (JSONL stream)."""
    cmd = _build_command(prompt, variant, workdir, graph_path)
    env = {**os.environ}
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_secs,
        env=env,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        # Include stderr in the raw output so we can surface it in the transcript
        return result.stdout + f"\n[STDERR]\n{result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Stream-JSON parsing
# ---------------------------------------------------------------------------


def _parse_stream_json(raw: str) -> dict[str, Any]:
    """Parse JSONL stream from claude --output-format stream-json --verbose.

    Returns a dict with:
        tool_calls: list of {name, input, result} dicts
        response_text: final assistant text
        cost_usd: float
        error: str or None
    """
    tool_calls: list[dict] = []
    pending_tool_use: dict[str, dict] = {}  # tool_use_id -> {name, input}
    response_text = ""
    cost_usd = 0.0
    error = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        ev_type = ev.get("type")

        if ev_type == "assistant":
            content = ev.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    pending_tool_use[block["id"]] = {
                        "name": block["name"],
                        "input": block.get("input", {}),
                        "result": None,
                    }
                elif block.get("type") == "text":
                    response_text = block.get("text", "")

        elif ev_type == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_result":
                    tu_id = block.get("tool_use_id", "")
                    if tu_id in pending_tool_use:
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Extract text from content blocks
                            result_content = " ".join(
                                b.get("text", "") for b in result_content
                                if isinstance(b, dict)
                            )
                        pending_tool_use[tu_id]["result"] = result_content
                        tool_calls.append(pending_tool_use.pop(tu_id))

        elif ev_type == "result":
            cost_usd = ev.get("total_cost_usd", 0.0)
            if ev.get("subtype") == "error" or ev.get("is_error"):
                error = ev.get("result", "unknown error")
            elif not response_text:
                response_text = ev.get("result", "")

    return {
        "tool_calls": tool_calls,
        "response_text": response_text,
        "cost_usd": cost_usd,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------


def _compute_metrics(parsed: dict[str, Any]) -> dict[str, Any]:
    """Compute tool-call counts and totals."""
    tool_counts: dict[str, int] = {}
    for tc in parsed["tool_calls"]:
        name = tc["name"]
        tool_counts[name] = tool_counts.get(name, 0) + 1
    return {
        "tool_calls": tool_counts,
        "total_tool_calls": len(parsed["tool_calls"]),
        "cost_usd": round(parsed.get("cost_usd", 0.0), 6),
        "error": parsed.get("error"),
    }


def _fmt_input(inp: Any) -> str:
    """Format tool input for display."""
    if isinstance(inp, dict):
        parts = []
        for k, v in inp.items():
            v_str = repr(v) if not isinstance(v, str) else v
            if len(v_str) > 200:
                v_str = v_str[:200] + "…"
            parts.append(f"`{k}` = {v_str}")
        return ", ".join(parts)
    return repr(inp)


def _fmt_result(result: Any) -> str:
    """Format tool result for display, truncating large outputs."""
    if result is None:
        return "(no result captured)"
    s = str(result)
    if len(s) > 500:
        return s[:500] + "\n…(truncated)"
    return s


def _build_transcript_md(
    eval_id: int,
    eval_name: str,
    variant: str,
    parsed: dict[str, Any],
) -> str:
    """Generate a human-readable transcript for graders."""
    label = "WITH Skill" if variant == "with_skill" else "WITHOUT Skill"
    lines = [
        f"# Eval {eval_id} — {eval_name.replace('-', ' ').title()} {label}: Transcript",
        "",
        "## Tool Calls",
        "",
    ]

    if not parsed["tool_calls"]:
        lines.append("*(no tool calls)*")
        lines.append("")
    else:
        for i, tc in enumerate(parsed["tool_calls"], 1):
            lines.append(f"### {i}. `{tc['name']}`")
            lines.append(f"**Input:** {_fmt_input(tc['input'])}")
            lines.append("")
            lines.append("**Result:**")
            lines.append("```")
            lines.append(_fmt_result(tc["result"]))
            lines.append("```")
            lines.append("")

    lines += [
        "---",
        "",
        "## Final Response",
        "",
        parsed["response_text"] or "*(no text response)*",
        "",
    ]

    if parsed.get("error"):
        lines += ["---", "", f"**Error:** {parsed['error']}", ""]

    return "\n".join(lines)


def _save_outputs(
    outputs_dir: Path,
    transcript_md: str,
    response_text: str,
    metrics: dict[str, Any],
) -> None:
    """Write transcript.md, response.md, and metrics.json."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / "transcript.md").write_text(transcript_md)
    (outputs_dir / "response.md").write_text(response_text or "")
    (outputs_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n"
    )


# ---------------------------------------------------------------------------
# Run one eval
# ---------------------------------------------------------------------------


def run_one(
    eval_config: dict[str, Any],
    variant: str,
    iteration: int,
    timeout_secs: int = 300,
) -> dict[str, Any]:
    """Orchestrate a single eval run and save outputs.

    Returns a summary dict for reporting.
    """
    eval_id = eval_config["id"]
    eval_name = eval_config["name"]
    prompt = eval_config["prompt"]
    seed_blocks = eval_config.get("seed", [])

    eval_dir_name = f"eval-{eval_id}-{eval_name}"
    eval_dir = WORKSPACE_DIR / f"iteration-{iteration}" / eval_dir_name / variant
    outputs_dir = eval_dir / "outputs"
    eval_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [eval-{eval_id} {variant}] starting…")

    with tempfile.TemporaryDirectory(prefix=f"minigraf-eval-{eval_id}-") as tmpdir:
        graph_path = Path(tmpdir) / "memory.graph"

        # Seed the isolated graph if seed blocks provided
        if seed_blocks and variant == "with_skill":
            _seed_graph(graph_path, seed_blocks)

        # Run claude
        raw = _run_claude(prompt, variant, eval_dir, graph_path, timeout_secs)

    # Parse and save
    parsed = _parse_stream_json(raw)
    metrics = _compute_metrics(parsed)
    transcript_md = _build_transcript_md(eval_id, eval_name, variant, parsed)
    _save_outputs(outputs_dir, transcript_md, parsed["response_text"], metrics)

    status = "error" if metrics.get("error") else "ok"
    print(
        f"  [eval-{eval_id} {variant}] done — "
        f"{metrics['total_tool_calls']} tool calls, "
        f"${metrics['cost_usd']:.4f}, "
        f"status={status}"
    )

    return {
        "eval_id": eval_id,
        "eval_name": eval_name,
        "variant": variant,
        "status": status,
        "total_tool_calls": metrics["total_tool_calls"],
        "cost_usd": metrics["cost_usd"],
        "outputs_dir": str(outputs_dir),
        "error": metrics.get("error"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_evals(eval_id: int | None = None) -> list[dict[str, Any]]:
    data = json.loads(EVALS_JSON.read_text())
    evals = data["evals"]
    if eval_id is not None:
        evals = [e for e in evals if e["id"] == eval_id]
        if not evals:
            raise ValueError(f"No eval with id={eval_id}")
    return evals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run temporal-reasoning evals in isolated sandboxes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python evals/run_isolated.py
              python evals/run_isolated.py --eval-id 2
              python evals/run_isolated.py --variant with_skill
              python evals/run_isolated.py --eval-id 4 --variant without_skill
              python evals/run_isolated.py --iteration 8 --concurrency 4
        """),
    )
    parser.add_argument("--eval-id", type=int, help="Run a single eval by id")
    parser.add_argument(
        "--variant",
        choices=VARIANTS,
        help="Run only this variant (default: both)",
    )
    parser.add_argument(
        "--iteration",
        type=int,
        help="Override iteration number (default: auto-detect next)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of parallel eval runs (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per eval run in seconds (default: 300)",
    )
    args = parser.parse_args()

    iteration = args.iteration or _detect_next_iteration()
    variants = [args.variant] if args.variant else list(VARIANTS)
    evals = _load_evals(args.eval_id)

    jobs = [(ev, variant) for ev in evals for variant in variants]
    total = len(jobs)

    print(f"Iteration {iteration} — {total} eval runs")
    print(f"  evals: {[e['id'] for e in evals]}")
    print(f"  variants: {variants}")
    print(f"  concurrency: {args.concurrency}")
    print(f"  workspace: {WORKSPACE_DIR / f'iteration-{iteration}'}")
    print()

    results = []
    errors = []

    if args.concurrency == 1:
        for ev, variant in jobs:
            try:
                r = run_one(ev, variant, iteration, args.timeout)
                results.append(r)
                if r.get("error"):
                    errors.append(r)
            except Exception as exc:  # pylint: disable=broad-except
                msg = f"eval-{ev['id']} {variant}: {exc}"
                print(f"  ERROR: {msg}")
                errors.append({"eval_id": ev["id"], "variant": variant, "error": str(exc)})
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(run_one, ev, variant, iteration, args.timeout): (ev, variant)
                for ev, variant in jobs
            }
            for future in as_completed(futures):
                ev, variant = futures[future]
                try:
                    r = future.result()
                    results.append(r)
                    if r.get("error"):
                        errors.append(r)
                except Exception as exc:  # pylint: disable=broad-except
                    msg = f"eval-{ev['id']} {variant}: {exc}"
                    print(f"  ERROR: {msg}")
                    errors.append({"eval_id": ev["id"], "variant": variant, "error": str(exc)})

    print()
    print(f"Done — {len(results)}/{total} successful, {len(errors)} errors")

    total_cost = sum(r.get("cost_usd", 0.0) for r in results)
    print(f"Total cost: ${total_cost:.4f}")

    if errors:
        print("\nFailed runs:")
        for e in errors:
            print(f"  eval-{e['eval_id']} {e['variant']}: {e['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
