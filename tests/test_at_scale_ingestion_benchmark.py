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
