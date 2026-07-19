# At-Scale Code-Graph Benchmark Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new, deterministic benchmark tier under `evals/at_scale/` that measures (A) git-ingestion performance and (B) structural/temporal query correctness+latency against this repo's own real history, as a checked-in observational report — closing issue #120.

**Architecture:** A standalone script drives `mcp_server.py`'s handler functions directly in-process (no subprocess, no MCP stdio transport, no LLM) — the same real-backend pattern `docs/testing-conventions.md` establishes for this project's test suite, just run as a script instead of under pytest. Part A starts `mcp_server._run_ingestion` as an asyncio task and concurrently polls `handle_minigraf_ingest_status`/`handle_minigraf_query` to measure responsiveness during ingestion. Part B reuses Part A's harness to populate a graph pinned at a fixed commit, then runs a hand-verified set of ground-truth Datalog queries against it, each paired with an equivalent `git log`/`git diff`/`git blame` baseline command.

**Tech Stack:** Python 3, stdlib only (`asyncio`, `resource`, `subprocess`, `json`, `time`, `argparse`) — no new dependencies. `pytest` + `pytest-asyncio` (already a dependency, `asyncio_mode = "auto"` per `pyproject.toml`) for the harness's own unit/integration tests.

## Global Constraints

- No new third-party dependencies (design explicitly chose `resource.getrusage` over `psutil`).
- Default repo fixture is `temporal_reasoning` itself (this repo); scripts accept `--repo-path`/`--branch` overrides for manual runs against other repos.
- Observational only for this version — no pass/fail thresholds, no CI gate wiring.
- Every run isolates its graph in a fresh temp path — never touches the live project `memory.graph`, following `evals/run_isolated.py`'s isolation approach.
- Follow `docs/testing-conventions.md`: real `MiniGrafDb`/`mcp_server` backend in every test, no `MagicMock` faking of graph behavior.
- Spec: `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.

---

### Task 1: Latency/throughput metrics helper

**Files:**
- Create: `evals/at_scale/metrics.py`
- Test: `tests/test_at_scale_metrics.py`

**Interfaces:**
- Produces: `latency_stats(samples: list[float]) -> dict[str, float]` returning `{"min": ..., "p50": ..., "p99": ..., "max": ...}` (all `0.0` if `samples` is empty); `throughput_per_minute(count: int, elapsed_seconds: float) -> float` (returns `0.0` if `elapsed_seconds <= 0`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_at_scale_metrics.py
from evals.at_scale.metrics import latency_stats, throughput_per_minute


class TestLatencyStats:
    def test_empty_samples_returns_zeros(self):
        assert latency_stats([]) == {"min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0}

    def test_single_sample(self):
        assert latency_stats([0.25]) == {"min": 0.25, "p50": 0.25, "p99": 0.25, "max": 0.25}

    def test_min_and_max_from_multiple_samples(self):
        result = latency_stats([0.1, 0.5, 0.2, 0.9, 0.3])
        assert result["min"] == 0.1
        assert result["max"] == 0.9

    def test_p50_is_median_of_sorted_samples(self):
        result = latency_stats([0.1, 0.2, 0.3, 0.4, 0.5])
        assert result["p50"] == 0.3

    def test_p99_is_near_max_for_large_sample_set(self):
        samples = [i / 1000 for i in range(1, 101)]  # 0.001..0.100
        result = latency_stats(samples)
        assert result["p99"] >= 0.098


class TestThroughputPerMinute:
    def test_zero_elapsed_returns_zero(self):
        assert throughput_per_minute(10, 0.0) == 0.0

    def test_negative_elapsed_returns_zero(self):
        assert throughput_per_minute(10, -1.0) == 0.0

    def test_computes_commits_per_minute(self):
        assert throughput_per_minute(60, 30.0) == 120.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_at_scale_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.at_scale'`

- [ ] **Step 3: Create package markers and write the implementation**

`evals/` has no `__init__.py` today (its scripts are run directly, not imported as a package — see `evals/run_isolated.py`). For `tests/test_at_scale_metrics.py` to `import evals.at_scale.metrics`, add empty `__init__.py` files:

```python
# evals/__init__.py
```

```python
# evals/at_scale/__init__.py
```

```python
# evals/at_scale/metrics.py
"""Pure metric-computation helpers for the at-scale benchmark tier (#120)."""

from __future__ import annotations


def latency_stats(samples: list[float]) -> dict[str, float]:
    """Return {min, p50, p99, max} for a list of latency samples (seconds).

    All-zero dict for an empty list. p50/p99 use nearest-rank on the sorted
    list — simple and sufficient for benchmark reporting, not a statistics
    library dependency.
    """
    if not samples:
        return {"min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(samples)
    n = len(ordered)

    def _percentile(p: float) -> float:
        idx = min(n - 1, int(round(p * (n - 1))))
        return ordered[idx]

    return {
        "min": ordered[0],
        "p50": _percentile(0.50),
        "p99": _percentile(0.99),
        "max": ordered[-1],
    }


def throughput_per_minute(count: int, elapsed_seconds: float) -> float:
    """Return count / (elapsed_seconds / 60), or 0.0 if elapsed_seconds <= 0."""
    if elapsed_seconds <= 0:
        return 0.0
    return count / (elapsed_seconds / 60.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_at_scale_metrics.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add evals/__init__.py evals/at_scale/__init__.py evals/at_scale/metrics.py tests/test_at_scale_metrics.py
git commit -m "feat(evals): add at-scale benchmark metrics helper (#120)"
```

---

### Task 2: Ingestion benchmark orchestration core

**Files:**
- Create: `evals/at_scale/run_ingestion_benchmark.py`
- Test: `tests/test_at_scale_ingestion_benchmark.py`

**Interfaces:**
- Consumes: `evals.at_scale.metrics.latency_stats`, `throughput_per_minute` (Task 1); `mcp_server._run_ingestion(repo_path: str, branch: str) -> None` (async); `mcp_server.open_db(graph_path: str) -> MiniGrafDb`; `mcp_server.handle_minigraf_ingest_status() -> dict`; `mcp_server.handle_minigraf_query(datalog: str) -> dict`; `mcp_server._default_git_branch(repo_path: str) -> str`; `fact_index.index_path_for(graph_path: str) -> str`.
- Produces: `async def run_ingestion_benchmark(repo_path: str, branch: str | None, graph_path: pathlib.Path, poll_interval: float = 0.5) -> dict[str, Any]`. Returned dict has keys: `repo_path`, `branch`, `commits_ingested` (int), `wall_clock_seconds` (float), `throughput_per_minute` (float), `peak_rss_kb` (int), `graph_size_bytes` (int), `index_size_bytes` (int), `status_latency` (dict from `latency_stats`), `query_latency` (dict from `latency_stats`), `final_status` (str, the terminal `_ingest_progress["status"]`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_at_scale_ingestion_benchmark.py
import subprocess as _subprocess

import pytest

from evals.at_scale.run_ingestion_benchmark import run_ingestion_benchmark


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with two commits (mirrors tests/test_mcp_server.py's fixture)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)
    return repo


class TestRunIngestionBenchmark:
    @pytest.mark.asyncio
    async def test_returns_expected_metric_keys(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert set(metrics.keys()) == {
            "repo_path", "branch", "commits_ingested", "wall_clock_seconds",
            "throughput_per_minute", "peak_rss_kb", "graph_size_bytes",
            "index_size_bytes", "status_latency", "query_latency", "final_status",
        }

    @pytest.mark.asyncio
    async def test_ingests_all_commits(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["commits_ingested"] == 2
        assert metrics["final_status"] == "complete"

    @pytest.mark.asyncio
    async def test_wall_clock_and_sizes_are_positive(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["wall_clock_seconds"] > 0
        assert metrics["peak_rss_kb"] > 0
        assert metrics["graph_size_bytes"] > 0
        assert metrics["index_size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_default_branch_resolved_when_none_passed(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        # git_repo has no "main"/"master" branch name set explicitly by `git init`
        # in this sandbox's git config, so branch=None must still resolve to
        # something _run_ingestion can walk without raising.
        metrics = await run_ingestion_benchmark(str(git_repo), None, graph_path, poll_interval=0.05)
        assert metrics["commits_ingested"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_at_scale_ingestion_benchmark.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.at_scale.run_ingestion_benchmark'`

- [ ] **Step 3: Write the implementation**

```python
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
    status_latencies, query_latencies = await _poll_during_ingestion(ingest_task, poll_interval)
    await ingest_task
    wall_clock = time.perf_counter() - start

    commits_ingested = mcp_server._ingest_progress["processed"]
    final_status = mcp_server._ingest_progress["status"]
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    index_path = fact_index.index_path_for(str(graph_path))
    graph_size_bytes = os.path.getsize(graph_path) if graph_path.exists() else 0
    index_size_bytes = os.path.getsize(index_path) if os.path.exists(index_path) else 0

    return {
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the at-scale ingestion benchmark (#120).")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-") as tmpdir:
        graph_path = Path(tmpdir) / "bench.graph"
        metrics = asyncio.run(
            run_ingestion_benchmark(args.repo_path, args.branch, graph_path, args.poll_interval)
        )
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_at_scale_ingestion_benchmark.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/test_at_scale_ingestion_benchmark.py
git commit -m "feat(evals): add in-process ingestion benchmark harness (#120)"
```

---

### Task 3: `--compare-ignore` path-ignore bloat comparison

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py`
- Test: `tests/test_at_scale_ingestion_benchmark.py`

**Interfaces:**
- Consumes: `mcp_server._DEFAULT_IGNORE_PATTERNS` (module-level tuple, monkeypatchable).
- Produces: `run_ingestion_benchmark(..., compare_ignore: bool = False)` — when `True`, the returned dict gains an `"ignore_comparison"` key: `{"with_ignore_graph_size_bytes": int, "without_ignore_graph_size_bytes": int, "delta_bytes": int}`.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_at_scale_ingestion_benchmark.py

class TestCompareIgnore:
    @pytest.mark.asyncio
    async def test_ignore_comparison_present_when_requested(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05, compare_ignore=True
        )
        assert "ignore_comparison" in metrics
        comp = metrics["ignore_comparison"]
        assert comp["with_ignore_graph_size_bytes"] > 0
        assert comp["without_ignore_graph_size_bytes"] > 0
        assert comp["delta_bytes"] == (
            comp["without_ignore_graph_size_bytes"] - comp["with_ignore_graph_size_bytes"]
        )

    @pytest.mark.asyncio
    async def test_ignore_comparison_absent_by_default(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert "ignore_comparison" not in metrics
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_at_scale_ingestion_benchmark.py::TestCompareIgnore -v`
Expected: FAIL with `TypeError: run_ingestion_benchmark() got an unexpected keyword argument 'compare_ignore'`

- [ ] **Step 3: Implement `compare_ignore`**

In `evals/at_scale/run_ingestion_benchmark.py`, change the signature and add the second run after the primary run completes:

```python
async def run_ingestion_benchmark(
    repo_path: str,
    branch: Optional[str],
    graph_path: Path,
    poll_interval: float = 0.5,
    compare_ignore: bool = False,
) -> dict[str, Any]:
```

After the existing `return {...}` block is built (keep it in a local variable instead of returning immediately):

```python
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
```

Also add `--compare-ignore` to the `main()` `argparse` block and thread it through the `run_ingestion_benchmark(...)` call:

```python
    parser.add_argument("--compare-ignore", action="store_true")
```

```python
        metrics = asyncio.run(
            run_ingestion_benchmark(
                args.repo_path, args.branch, graph_path, args.poll_interval, args.compare_ignore
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_at_scale_ingestion_benchmark.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/test_at_scale_ingestion_benchmark.py
git commit -m "feat(evals): add --compare-ignore path-ignore bloat comparison (#120)"
```

---

### Task 4: JSON + Markdown report writers

**Files:**
- Create: `evals/at_scale/report.py`
- Test: `tests/test_at_scale_report.py`
- Modify: `evals/at_scale/run_ingestion_benchmark.py` (wire into `main()`)

**Interfaces:**
- Consumes: the `dict[str, Any]` returned by `run_ingestion_benchmark`.
- Produces: `write_json_result(metrics: dict, results_dir: Path, prefix: str = "ingestion") -> Path` (writes `<prefix>-<UTC timestamp>.json`, returns the path); `append_ingestion_report(metrics: dict, report_path: Path) -> None` (creates `report_path` with a top-level `# At-Scale Code-Graph Benchmark` header if it doesn't exist, then appends a dated `## Ingestion Run — <UTC timestamp>` section with a metrics table).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_at_scale_report.py
import json

from evals.at_scale.report import append_ingestion_report, write_json_result

SAMPLE_METRICS = {
    "repo_path": "/tmp/repo", "branch": "HEAD", "commits_ingested": 2,
    "wall_clock_seconds": 1.234, "throughput_per_minute": 97.2,
    "peak_rss_kb": 45000, "graph_size_bytes": 8192, "index_size_bytes": 4096,
    "status_latency": {"min": 0.001, "p50": 0.002, "p99": 0.004, "max": 0.005},
    "query_latency": {"min": 0.002, "p50": 0.003, "p99": 0.006, "max": 0.008},
    "final_status": "complete",
}


class TestWriteJsonResult:
    def test_writes_valid_json_file(self, tmp_path):
        path = write_json_result(SAMPLE_METRICS, tmp_path)
        assert path.exists()
        assert json.loads(path.read_text()) == SAMPLE_METRICS

    def test_filename_has_prefix_and_timestamp(self, tmp_path):
        path = write_json_result(SAMPLE_METRICS, tmp_path, prefix="ingestion")
        assert path.name.startswith("ingestion-")
        assert path.suffix == ".json"


class TestAppendIngestionReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert text.startswith("# At-Scale Code-Graph Benchmark")

    def test_appends_metrics_table_with_real_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "## Ingestion Run" in text
        assert "2" in text  # commits_ingested
        assert "complete" in text

    def test_second_call_appends_not_overwrites(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        first_len = len(report_path.read_text())
        append_ingestion_report(SAMPLE_METRICS, report_path)
        assert len(report_path.read_text()) > first_len
        assert report_path.read_text().count("## Ingestion Run") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_at_scale_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.at_scale.report'`

- [ ] **Step 3: Write the implementation**

```python
# evals/at_scale/report.py
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
```

Wire it into `evals/at_scale/run_ingestion_benchmark.py`'s `main()` — replace the `print(json.dumps(...))` line with:

```python
    from evals.at_scale.report import append_ingestion_report, write_json_result

    results_dir = REPO_ROOT / "evals" / "at_scale" / "results"
    report_path = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
    json_path = write_json_result(metrics, results_dir)
    append_ingestion_report(metrics, report_path)
    print(json.dumps(metrics, indent=2))
    print(f"\nWrote {json_path}")
    print(f"Appended to {report_path}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_at_scale_report.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/report.py tests/test_at_scale_report.py evals/at_scale/run_ingestion_benchmark.py
git commit -m "feat(evals): add JSON/Markdown report writers for at-scale benchmark (#120)"
```

---

### Task 5: Run Part A for real against this repo, commit the baseline report

This task produces the actual first real numbers — no code changes beyond what Tasks 1-4 already wrote.

- [ ] **Step 1: Run the benchmark against this repo's own history**

Run: `python -m evals.at_scale.run_ingestion_benchmark --repo-path . --branch HEAD`

This ingests `temporal_reasoning`'s own commit history (hundreds of commits, per `git log --oneline | wc -l`) into an isolated temp graph and prints the metrics JSON.

- [ ] **Step 2: Inspect the output for sanity**

Confirm `final_status == "complete"`, `commits_ingested` roughly matches `git log --oneline | wc -l` (may differ slightly if some commits touch no parseable files), and `wall_clock_seconds` is a plausible number of seconds to a few minutes (not hours — this validates the "reproducible, CI-friendly default" design goal).

- [ ] **Step 3: Verify the report and JSON result were written**

Run: `cat evals/at_scale/benchmark.md` and confirm an `## Ingestion Run` section with real values appears. Run: `ls evals/at_scale/results/` and confirm a new `ingestion-<timestamp>.json` file exists.

- [ ] **Step 4: Commit the baseline report**

```bash
git add evals/at_scale/benchmark.md evals/at_scale/results/
git commit -m "chore(evals): record first at-scale ingestion benchmark baseline (#120)"
```

---

### Task 6: Part B ground-truth fixture

**Files:**
- Create: `evals/at_scale/query_ground_truth.json`

All entries below are pinned to `fact_index.py`'s real introduction-through-hardening history in this repo (commits `2a524096b9eb...` through `3f30610f49a4...`, all merged 2026-07-17 as part of #118) — verified against this repo's actual `git log`/`git show`/`git diff` output and this codebase's real ident-canonicalization algorithm (`_canonical_ident` in `mcp_server.py`) before writing this file, not guessed.

- [ ] **Step 1: Write the ground-truth fixture**

```json
{
  "pinned_commit": "3f30610f49a4f39c9e7fce73305fc03b65f45131",
  "entries": [
    {
      "id": 1,
      "category": "point-in-time",
      "question": "How many :type/function entities did fact_index.py contain right after its introduction commit (2a524096b9eb, 2026-07-17T04:30:55Z)?",
      "datalog": "[:find (count ?fn) :valid-at \"2026-07-17T04:31:00Z\" :where [:module/fact-index-py :contains ?fn] [?fn :entity-type :type/function]]",
      "expected": [[8]],
      "baseline_cmd": "git show 2a524096b9eb:fact_index.py | grep -c '^def '"
    },
    {
      "id": 2,
      "category": "delta",
      "question": "Which functions were added to fact_index.py between commit 2a524096b9eb (04:30:55Z) and commit 3f30610f49a4 (06:33:01Z)?",
      "datalog": "[:find ?desc :valid-at \"2026-07-17T06:34:00Z\" :where [:module/fact-index-py :contains ?fn] [?fn :entity-type :type/function] [?fn :description ?desc]]",
      "expected_new_since_2026-07-17T04:31:00Z": ["_tokenize", "_fts5_match_query", "query_facts", "rebuild_index"],
      "note": "Run this query twice (:valid-at \"2026-07-17T04:31:00Z\" and :valid-at \"2026-07-17T06:34:00Z\") and diff the two 12-vs-8 result sets, per SKILL.md's documented cross-commit delta pattern -- the 4 names above are the rows present only in the second run.",
      "baseline_cmd": "git diff 2a524096b9eb 3f30610f49a4 -- fact_index.py | grep '^+def ' | sed 's/^+def //;s/(.*//'"
    },
    {
      "id": 3,
      "category": "regression-tracing",
      "question": "When did rebuild_index first appear in fact_index.py?",
      "datalog": "[:find ?c ?date :where [:function/fact-index-py-rebuild-index :introduced-by ?c] [?c :date ?date]]",
      "expected": [[":commit/6f2e04df1145", "2026-07-17T06:22:35Z"]],
      "baseline_cmd": "git log -S'def rebuild_index' --oneline -- fact_index.py"
    },
    {
      "id": 4,
      "category": "dependency-impact",
      "question": "Which module(s) depend on fact_index.py?",
      "datalog": "[:find ?m :where [?m :depends-on :module/fact-index-py] [?m :entity-type :type/module]]",
      "expected": [[":module/mcp-server-py"]],
      "baseline_cmd": "grep -l '^import fact_index' *.py"
    },
    {
      "id": 5,
      "category": "cross-layer",
      "question": "(before) At a synthetic decision's valid-from (2026-07-17T04:00:00Z, before fact_index.py existed), how many :type/function entities exist for fact_index.py?",
      "seed": "[[:decision/persisted-fact-index :entity-type :type/decision] [:decision/persisted-fact-index :description \"Persist the fact index as a SQLite FTS5 table instead of an in-memory index\"]]",
      "seed_valid_from": "2026-07-17T04:00:00Z",
      "seed_note": "Synthetic -- no agent-authored decision datom naturally exists tied to this repo's real history. Seeded explicitly so this question is answerable; not a claim the correlation is organically real. See design doc's 'Cross-layer question caveat'.",
      "datalog": "[:find ?fn :valid-at \"2026-07-17T04:00:00Z\" :where [?fn :entity-type :type/function] [?fn :file \"fact_index.py\"]]",
      "expected": [],
      "baseline_cmd": "git log --oneline --diff-filter=A -- fact_index.py"
    },
    {
      "id": 6,
      "category": "cross-layer",
      "question": "(after) At the pinned commit's valid-time (2026-07-17T06:34:00Z, after the decision's valid-from), how many :type/function entities exist for fact_index.py?",
      "datalog": "[:find (count ?fn) :valid-at \"2026-07-17T06:34:00Z\" :where [?fn :entity-type :type/function] [?fn :file \"fact_index.py\"]]",
      "expected": [[12]],
      "baseline_cmd": "git show 3f30610f49a4:fact_index.py | grep -c '^def '"
    }
  ]
}
```

- [ ] **Step 2: Validate the JSON parses and has the expected shape**

Run: `python -c "import json; d = json.load(open('evals/at_scale/query_ground_truth.json')); assert len(d['entries']) == 6; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add evals/at_scale/query_ground_truth.json
git commit -m "feat(evals): add hand-verified query ground truth for at-scale benchmark (#120)"
```

---

### Task 7: Query correctness & latency benchmark script

**Files:**
- Create: `evals/at_scale/run_query_benchmark.py`
- Test: `tests/test_at_scale_query_benchmark.py`

**Interfaces:**
- Consumes: `evals.at_scale.run_ingestion_benchmark.run_ingestion_benchmark` (Task 2); `mcp_server.handle_minigraf_query`; `mcp_server.get_db()`; `evals/at_scale/query_ground_truth.json` (Task 6).
- Produces: `async def run_query_benchmark(repo_path: str, graph_path: Path, ground_truth_path: Path) -> list[dict[str, Any]]` — one result dict per ground-truth entry: `{"id": int, "category": str, "passed": bool, "actual": Any, "expected": Any, "minigraf_latency_seconds": float, "baseline_latency_seconds": float}`. Entries with a `"seed"` key are transacted (with `"seed_valid_from"` as the `:valid-from` option) before their query runs. Entries with `"note"` instead of `"expected"` (id 2, the delta entry) are skipped from pass/fail scoring and reported with `"passed": None` — the note documents a two-query manual-diff procedure, not a single-query assertion.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_at_scale_query_benchmark.py
import json
import subprocess as _subprocess

import pytest

from evals.at_scale.run_query_benchmark import run_query_benchmark


@pytest.fixture
def tiny_ground_truth(tmp_path):
    """A minimal 2-entry ground truth file exercising both the plain-query
    path and the seed+valid-from path, independent of the real fact_index.py
    fixture in evals/at_scale/query_ground_truth.json (which needs this
    repo's real history and isn't reproducible against a throwaway git_repo)."""
    gt = {
        "pinned_commit": "HEAD",
        "entries": [
            {
                "id": 1,
                "category": "point-in-time",
                "question": "How many commit entities exist?",
                "datalog": "[:find (count ?e) :where [?e :entity-type :type/commit]]",
                "expected": [[2]],
                "baseline_cmd": "python3 -c \"print(2)\"",
            },
            {
                "id": 2,
                "category": "cross-layer",
                "question": "Does the seeded decision exist?",
                "seed": "[[:decision/test-decision :entity-type :type/decision] [:decision/test-decision :description \"test\"]]",
                "seed_valid_from": "2020-01-01T00:00:00Z",
                "datalog": "[:find ?d :where [:decision/test-decision :description ?d]]",
                "expected": [["test"]],
                "baseline_cmd": "python3 -c \"print('test')\"",
            },
        ],
    }
    path = tmp_path / "ground_truth.json"
    path.write_text(json.dumps(gt))
    return path


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)
    return repo


class TestRunQueryBenchmark:
    @pytest.mark.asyncio
    async def test_all_entries_pass_against_matching_fixture(self, git_repo, tmp_path, tiny_ground_truth):
        graph_path = tmp_path / "bench.graph"
        results = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        assert len(results) == 2
        assert all(r["passed"] for r in results)

    @pytest.mark.asyncio
    async def test_result_includes_latency_fields(self, git_repo, tmp_path, tiny_ground_truth):
        graph_path = tmp_path / "bench.graph"
        results = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        for r in results:
            assert r["minigraf_latency_seconds"] >= 0
            assert r["baseline_latency_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_mismatch_reports_failure(self, git_repo, tmp_path, tiny_ground_truth):
        gt = json.loads(tiny_ground_truth.read_text())
        gt["entries"][0]["expected"] = [[999]]
        tiny_ground_truth.write_text(json.dumps(gt))
        graph_path = tmp_path / "bench.graph"
        results = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        assert results[0]["passed"] is False
        assert results[0]["actual"] != results[0]["expected"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_at_scale_query_benchmark.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'evals.at_scale.run_query_benchmark'`

- [ ] **Step 3: Write the implementation**

```python
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
            db.execute(
                f'(transact {entry["seed"]} {{:valid-from "{entry["seed_valid_from"]}"}})'
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_at_scale_query_benchmark.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/run_query_benchmark.py tests/test_at_scale_query_benchmark.py
git commit -m "feat(evals): add query correctness+latency benchmark script (#120)"
```

---

### Task 8: Query-report writer + real run against this repo's ground truth

**Files:**
- Modify: `evals/at_scale/report.py`
- Test: `tests/test_at_scale_report.py`

**Interfaces:**
- Consumes: `list[dict[str, Any]]` as produced by `run_query_benchmark` (Task 7).
- Produces: `append_query_report(results: list[dict], report_path: Path) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_at_scale_report.py
from evals.at_scale.report import append_query_report

SAMPLE_QUERY_RESULTS = [
    {
        "id": 1, "category": "point-in-time", "passed": True,
        "actual": [[8]], "expected": [[8]],
        "minigraf_latency_seconds": 0.003, "baseline_latency_seconds": 0.015,
    },
    {
        "id": 2, "category": "delta", "passed": None,
        "actual": None, "expected": None,
        "minigraf_latency_seconds": 0.0, "baseline_latency_seconds": 0.0,
    },
]


class TestAppendQueryReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_RESULTS, report_path)
        assert report_path.read_text().startswith("# At-Scale Code-Graph Benchmark")

    def test_reports_pass_fail_and_skipped(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_RESULTS, report_path)
        text = report_path.read_text()
        assert "## Query Correctness Run" in text
        assert "PASS" in text
        assert "SKIPPED (manual diff)" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_at_scale_report.py::TestAppendQueryReport -v`
Expected: FAIL with `ImportError: cannot import name 'append_query_report'`

- [ ] **Step 3: Implement `append_query_report`**

Add to `evals/at_scale/report.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_at_scale_report.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/report.py tests/test_at_scale_report.py
git commit -m "feat(evals): add query-benchmark report writer (#120)"
```

- [ ] **Step 6: Run the query benchmark for real against this repo**

Run: `python -m evals.at_scale.run_query_benchmark --repo-path .`

This ingests through the pinned commit (`3f30610f49a4...`) and runs the 6 ground-truth entries from Task 6 for real.

- [ ] **Step 7: Reconcile any real FAIL results**

If any entry reports `FAIL`, compare `actual` vs `expected` in the printed JSON against the entry's `baseline_cmd` output run manually (`git show ...`, `git diff ...`, `git log -S...` as documented in each entry). Two likely sources of mismatch, both fixable in `evals/at_scale/query_ground_truth.json` without touching `mcp_server.py`:
- A slug-canonicalization detail this plan's hand-derivation got wrong (re-derive using `python3 -c "import mcp_server; print(mcp_server._canonical_ident('function', 'fact_index.py::rebuild_index'))"` and correct the `datalog`/`expected` ident strings).
- A timestamp boundary too close to a real commit's exact second (widen the `:valid-at` gap in the affected entry, e.g. `"2026-07-17T04:35:00Z"` instead of `"2026-07-17T04:31:00Z"`, and re-verify against the corresponding `baseline_cmd`).

If entries needed correction, commit the fixture fix separately before re-running.

- [ ] **Step 8: Commit the final baseline report**

```bash
git add evals/at_scale/benchmark.md evals/at_scale/results/
git commit -m "chore(evals): record first at-scale query correctness benchmark baseline (#120)"
```

---

## Self-Review Notes

- **Spec coverage:** Part A metrics (wall-clock, throughput, peak RSS, graph/index size, in-flight responsiveness, `--compare-ignore`) — Tasks 2-3. Checked-in `evals/at_scale/benchmark.md` + `results/*.json` — Tasks 4-5. Part B ground truth across all 5 issue-named categories (point-in-time, delta, regression-tracing, dependency-impact standing in for transitive-impact-at-depth, cross-layer) with git-baseline comparison — Tasks 6-8. In-process/no-LLM harness architecture — Task 2. Default-to-self-repo with `--repo-path` override — Tasks 2, 5, 8.
- **Placeholder scan:** Ground-truth `expected` values (Task 6) and the delta entry's manual-diff `note` are real, hand-verified content derived from this repo's actual git history and `_canonical_ident` algorithm, not fabricated. Task 8 Step 7's reconciliation branch is a contingency procedure (concrete diagnosis steps + concrete fix commands), not a vague "handle errors" placeholder — included because ground truth authored by hand-tracing code, without running the not-yet-built harness, carries real (if small) risk of a boundary/slug mismatch that only surfaces at real execution time.
- **Type consistency:** `run_ingestion_benchmark`'s return dict keys (Task 2) are consumed identically by `append_ingestion_report` (Task 4) and by Task 3's `ignore_comparison` addition. `run_query_benchmark`'s per-entry result dict shape (Task 7) matches exactly what `append_query_report` (Task 8) consumes.
