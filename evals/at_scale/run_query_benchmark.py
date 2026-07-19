# evals/at_scale/run_query_benchmark.py
"""Query correctness + latency benchmark against hand-verified ground truth
(#120, Part B). Reuses Part A's ingestion harness to populate a graph, then
runs each ground-truth entry's Datalog query and its git-command baseline,
timing both and comparing the Datalog result to the recorded expected value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess as _subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale.run_ingestion_benchmark import run_ingestion_benchmark  # noqa: E402


async def run_query_benchmark(
    repo_path: str,
    graph_path: Path,
    ground_truth_path: Path,
) -> list[dict[str, Any]]:
    import mcp_server

    ground_truth = json.loads(ground_truth_path.read_text())
    pinned_ref = ground_truth.get("pinned_commit") or "HEAD"

    await run_ingestion_benchmark(repo_path, pinned_ref, graph_path, poll_interval=0.05)

    results: list[dict[str, Any]] = []
    for entry in ground_truth["entries"]:
        if "seed" in entry:
            db = mcp_server.get_db()
            # NOTE: the minigraf grammar is `(transact {opts} facts)` -- opts
            # dict FIRST, facts second (confirmed against mcp_server._transact
            # and every real call site in mcp_server.py/tests/test_mcp_server.py).
            # The reverse order parses without error but silently drops
            # :valid-from (verified empirically: a query with :valid-at before
            # "now" no longer finds the fact), so getting this order right is
            # load-bearing for the "seed" entries' bi-temporal semantics.
            db.execute(
                f'(transact {{:valid-from "{entry["seed_valid_from"]}"}} {entry["seed"]})'
            )

        if "expected" not in entry:
            # Multi-query manual-diff entries (see the entry's "note") are
            # documented, not scored -- no single query/expected pair exists.
            results.append({
                "id": entry["id"],
                "category": entry["category"],
                "passed": None,
                "actual": None,
                "expected": None,
                "minigraf_latency_seconds": 0.0,
                "baseline_latency_seconds": 0.0,
            })
            continue

        t0 = time.perf_counter()
        query_result = mcp_server.handle_minigraf_query(entry["datalog"])
        minigraf_latency = time.perf_counter() - t0
        actual = query_result.get("results")

        t0 = time.perf_counter()
        _subprocess.run(
            entry["baseline_cmd"], shell=True, cwd=repo_path,
            capture_output=True, text=True,
        )
        baseline_latency = time.perf_counter() - t0

        results.append({
            "id": entry["id"],
            "category": entry["category"],
            "passed": actual == entry["expected"],
            "actual": actual,
            "expected": entry["expected"],
            "minigraf_latency_seconds": minigraf_latency,
            "baseline_latency_seconds": baseline_latency,
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the at-scale query benchmark (#120).")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument(
        "--ground-truth",
        default=str(REPO_ROOT / "evals" / "at_scale" / "query_ground_truth.json"),
    )
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-query-") as tmpdir:
        graph_path = Path(tmpdir) / "bench.graph"
        results = asyncio.run(
            run_query_benchmark(args.repo_path, graph_path, Path(args.ground_truth))
        )

    from evals.at_scale.report import append_query_report

    report_path = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
    append_query_report(results, report_path)
    print(json.dumps(results, indent=2))
    print(f"\nAppended to {report_path}")


if __name__ == "__main__":
    main()
