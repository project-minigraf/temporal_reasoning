# tests/test_at_scale_query_benchmark.py
import json
import subprocess as _subprocess

import pytest

from evals.at_scale.run_query_benchmark import _exit_code, run_query_benchmark


class TestExitCode:
    def test_zero_when_all_scored_entries_pass(self):
        results = [
            {"id": 1, "passed": True},
            {"id": 2, "passed": None},
            {"id": 3, "passed": True},
        ]
        assert _exit_code(results) == 0

    def test_nonzero_when_any_entry_fails(self):
        results = [
            {"id": 1, "passed": True},
            {"id": 2, "passed": False},
        ]
        assert _exit_code(results) == 1

    def test_zero_for_empty_results(self):
        assert _exit_code([]) == 0


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
