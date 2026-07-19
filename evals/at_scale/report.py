"""JSON and Markdown report writers for the at-scale benchmark tier (#120)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

_REPORT_HEADER = "# At-Scale Code-Graph Benchmark\n\nSee issue #120 and `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.\nObservational only -- no pass/fail thresholds.\n"


def _utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json_result(metrics: dict[str, Any], results_dir: Path, prefix: str = "ingestion") -> Path:
    """Write metrics as machine-readable JSON to results_dir/<prefix>-<ts>.json."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{prefix}-{_utc_timestamp()}.json"
    path.write_text(json.dumps(metrics, indent=2) + "\n")
    return path


def append_ingestion_report(metrics: dict[str, Any], report_path: Path) -> None:
    """Append a dated ingestion-run section to report_path, creating it with
    the shared header first if it doesn't exist yet."""
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Ingestion Run — {_utc_timestamp()}",
        "",
        f"- Repo: `{metrics['repo_path']}` @ `{metrics['branch']}`",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Commits ingested | {metrics['commits_ingested']} |",
        f"| Final status | {metrics['final_status']} |",
        f"| Wall-clock | {metrics['wall_clock_seconds']:.2f}s |",
        f"| Throughput | {metrics['throughput_per_minute']:.1f} commits/min |",
        f"| Peak RSS | {metrics['peak_rss_kb']} KB |",
        f"| Graph size | {metrics['graph_size_bytes']} bytes |",
        f"| Fact-index size | {metrics['index_size_bytes']} bytes |",
        f"| Status-query latency (min/p50/p99/max) | "
        f"{metrics['status_latency']['min']*1000:.1f}ms / "
        f"{metrics['status_latency']['p50']*1000:.1f}ms / "
        f"{metrics['status_latency']['p99']*1000:.1f}ms / "
        f"{metrics['status_latency']['max']*1000:.1f}ms |",
        f"| Graph-query latency (min/p50/p99/max) | "
        f"{metrics['query_latency']['min']*1000:.1f}ms / "
        f"{metrics['query_latency']['p50']*1000:.1f}ms / "
        f"{metrics['query_latency']['p99']*1000:.1f}ms / "
        f"{metrics['query_latency']['max']*1000:.1f}ms |",
    ]
    if "ignore_comparison" in metrics:
        comp = metrics["ignore_comparison"]
        lines += [
            f"| Graph size with path-ignore | {comp['with_ignore_graph_size_bytes']} bytes |",
            f"| Graph size without path-ignore | {comp['without_ignore_graph_size_bytes']} bytes |",
            f"| Path-ignore bloat reduction | {comp['delta_bytes']} bytes |",
        ]
    lines.append("")

    with report_path.open("a") as f:
        f.write("\n".join(lines))


def append_query_report(results: list[dict[str, Any]], report_path: Path) -> None:
    """Append a dated query-correctness section to report_path, creating it
    with the shared header first if it doesn't exist yet."""
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Query Correctness Run — {_utc_timestamp()}",
        "",
        "| ID | Category | Result | minigraf latency | baseline latency |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r["passed"] is None:
            status = "SKIPPED (manual diff)"
        elif r["passed"]:
            status = "PASS"
        else:
            status = f"FAIL (expected `{r['expected']}`, got `{r['actual']}`)"
        lines.append(
            f"| {r['id']} | {r['category']} | {status} | "
            f"{r['minigraf_latency_seconds']*1000:.1f}ms | "
            f"{r['baseline_latency_seconds']*1000:.1f}ms |"
        )
    lines.append("")

    with report_path.open("a") as f:
        f.write("\n".join(lines))
