# evals/at_scale/run_ingestion_benchmark.py
"""In-process ingestion-performance benchmark harness (#120, Part A).

Drives mcp_server.py's real handlers directly -- no subprocess, no MCP
stdio transport, no LLM -- following this project's real-backend testing
convention (docs/testing-conventions.md) applied to a standalone script
instead of a pytest fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale.metrics import latency_stats, throughput_per_minute  # noqa: E402

_STATUS_QUERY = "[:find (count ?e) :where [?e :entity-type :type/commit]]"


async def _poll_during_ingestion(
    ingest_task: "asyncio.Task[None]",
    poll_interval: float,
) -> tuple[list[float], list[float]]:
    """Poll ingest_status and a cheap query at poll_interval while ingest_task
    runs. Returns (status_latencies, query_latencies) in seconds."""
    import mcp_server

    status_latencies: list[float] = []
    query_latencies: list[float] = []
    while not ingest_task.done():
        t0 = time.perf_counter()
        mcp_server.handle_minigraf_ingest_status()
        status_latencies.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        mcp_server.handle_minigraf_query(_STATUS_QUERY)
        query_latencies.append(time.perf_counter() - t0)

        await asyncio.sleep(poll_interval)
    return status_latencies, query_latencies


async def run_ingestion_benchmark(
    repo_path: str,
    branch: Optional[str],
    graph_path: Path,
    poll_interval: float = 0.5,
    compare_ignore: bool = False,
) -> dict[str, Any]:
    """Run a full git ingestion against repo_path into an isolated graph at
    graph_path, measuring wall-clock, throughput, peak RSS, final graph/index
    size, and MCP responsiveness (status/query latency) while ingestion runs.

    graph_path must not already exist -- each call is a fresh, isolated run.
    """
    import fact_index
    import mcp_server

    mcp_server._db = None
    mcp_server._graph_path = None
    mcp_server.open_db(str(graph_path))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }

    resolved_branch = branch or mcp_server._default_git_branch(repo_path)

    start = time.perf_counter()
    ingest_task = asyncio.create_task(mcp_server._run_ingestion(repo_path, resolved_branch))
    try:
        status_latencies, query_latencies = await _poll_during_ingestion(ingest_task, poll_interval)
        await ingest_task
    except BaseException:
        if not ingest_task.done():
            ingest_task.cancel()
            try:
                await ingest_task
            except (asyncio.CancelledError, Exception):
                pass
        raise
    wall_clock = time.perf_counter() - start

    commits_ingested = mcp_server._ingest_progress["processed"]
    final_status = mcp_server._ingest_progress["status"]
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    index_path = fact_index.index_path_for(str(graph_path))
    graph_size_bytes = os.path.getsize(graph_path) if graph_path.exists() else 0
    index_size_bytes = os.path.getsize(index_path) if os.path.exists(index_path) else 0

    result = {
        "repo_path": repo_path,
        "branch": resolved_branch,
        "commits_ingested": commits_ingested,
        "wall_clock_seconds": wall_clock,
        "throughput_per_minute": throughput_per_minute(commits_ingested, wall_clock),
        "peak_rss_kb": peak_rss_kb,
        "graph_size_bytes": graph_size_bytes,
        "index_size_bytes": index_size_bytes,
        "status_latency": latency_stats(status_latencies),
        "query_latency": latency_stats(query_latencies),
        "final_status": final_status,
    }

    if compare_ignore:
        no_ignore_graph_path = graph_path.parent / f"{graph_path.stem}-no-ignore{graph_path.suffix}"
        original_patterns = mcp_server._DEFAULT_IGNORE_PATTERNS
        mcp_server._DEFAULT_IGNORE_PATTERNS = ()
        try:
            mcp_server._db = None
            mcp_server._graph_path = None
            mcp_server.open_db(str(no_ignore_graph_path))
            mcp_server._ingest_progress = {
                "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
                "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
            }
            await mcp_server._run_ingestion(repo_path, resolved_branch)
        finally:
            mcp_server._DEFAULT_IGNORE_PATTERNS = original_patterns

        without_ignore_size = (
            os.path.getsize(no_ignore_graph_path) if no_ignore_graph_path.exists() else 0
        )
        result["ignore_comparison"] = {
            "with_ignore_graph_size_bytes": graph_size_bytes,
            "without_ignore_graph_size_bytes": without_ignore_size,
            "delta_bytes": without_ignore_size - graph_size_bytes,
        }

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the at-scale ingestion benchmark (#120).")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--compare-ignore", action="store_true")
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-") as tmpdir:
        graph_path = Path(tmpdir) / "bench.graph"
        metrics = asyncio.run(
            run_ingestion_benchmark(
                args.repo_path, args.branch, graph_path, args.poll_interval, args.compare_ignore
            )
        )
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
