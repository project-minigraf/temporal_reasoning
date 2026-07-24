"""Tests for frontier_registry.py -- real git subprocess calls, no mocking.

This module has no DB dependency, so its own real dependency (git) is what
gets exercised for real here, matching the spirit of
docs/testing-conventions.md's real-backend rule.
"""
import os
import subprocess as _subprocess

import pytest

import frontier_registry
from frontier_registry import FrontierAllocator, Interval, TAG_AUTHORITATIVE, TAG_PROVISIONAL


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "first"], cwd=repo, check=True, capture_output=True)
    (repo / "b.py").write_text("y = 2\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "second"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_repo_diamond_clock_skewed(tmp_path):
    """A fork+merge DAG where one forked branch's single commit is dated
    EARLIER than its own parent (clock skew) -- unlike a linear chain (which
    has no ordering ambiguity for any git log mode to resolve), this
    fork+merge shape genuinely produces different output depending on
    --topo-order. Verified empirically: plain `git log --reverse` (no
    --topo-order) outputs C1 BEFORE P, a real topological violation (a
    commit before its own parent), because C1's date is earlier than P's.
    `--topo-order --reverse` correctly places P first.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def commit(filename, content, message, date_iso):
        (repo / filename).write_text(content)
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        env = {**os.environ, "GIT_AUTHOR_DATE": date_iso, "GIT_COMMITTER_DATE": date_iso}
        _subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True, env=env)

    # P (root), dated Jan 3
    commit("p.txt", "p\n", "P", "2026-01-03T00:00:00")
    _subprocess.run(["git", "branch", "branch2"], cwd=repo, check=True, capture_output=True)

    # C1: child of P, dated Jan 1 -- EARLIER than P (the skew)
    commit("c1.txt", "c1\n", "C1", "2026-01-01T00:00:00")
    _subprocess.run(["git", "branch", "branch1"], cwd=repo, check=True, capture_output=True)

    # branch2: normal monotonically-increasing chain from P
    _subprocess.run(["git", "checkout", "branch2"], cwd=repo, check=True, capture_output=True)
    commit("c2a.txt", "c2a\n", "C2a", "2026-01-05T00:00:00")
    commit("c2b.txt", "c2b\n", "C2b", "2026-01-06T00:00:00")
    commit("c2tip.txt", "c2tip\n", "C2tip", "2026-01-07T00:00:00")

    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-01-08T00:00:00", "GIT_COMMITTER_DATE": "2026-01-08T00:00:00"}
    _subprocess.run(
        ["git", "merge", "--no-ff", "-m", "MG", "branch1"],
        cwd=repo, check=True, capture_output=True, env=env,
    )

    return repo


class TestBuildLinearization:
    def test_returns_hashes_oldest_first(self, git_repo):
        result = _subprocess.run(
            ["git", "log", "--format=%H"], cwd=git_repo, capture_output=True, text=True, check=True
        )
        newest_first = result.stdout.strip().splitlines()
        linearization = frontier_registry.build_linearization(str(git_repo))
        assert linearization == list(reversed(newest_first))


class TestBuildLinearizationTopoOrder:
    def test_topo_order_survives_clock_skew(self, git_repo_diamond_clock_skewed):
        linearization = frontier_registry.build_linearization(str(git_repo_diamond_clock_skewed))
        log_result = _subprocess.run(
            ["git", "log", "--topo-order", "--reverse", "--format=%H %s"],
            cwd=git_repo_diamond_clock_skewed, capture_output=True, text=True, check=True,
        )
        lines = log_result.stdout.strip().splitlines()
        expected_hashes = [line.split(" ", 1)[0] for line in lines]
        expected_subjects = [line.split(" ", 1)[1] for line in lines]
        assert expected_subjects == ["P", "C2a", "C2b", "C2tip", "C1", "MG"]
        assert linearization == expected_hashes


class TestFrontierAllocatorDegenerateCases:
    def test_empty_repo_both_claims_none(self):
        allocator = FrontierAllocator(0)
        assert allocator.claim_low() is None
        assert allocator.claim_high() is None

    def test_gap_already_empty_at_construction(self):
        allocator = FrontierAllocator(5, [Interval(0, 4, TAG_AUTHORITATIVE)])
        assert allocator.is_gap_empty()
        assert allocator.claim_low() is None
        assert allocator.claim_high() is None

    def test_single_commit_repo_exactly_once_low_first(self):
        allocator = FrontierAllocator(1)
        assert not allocator.is_gap_empty()
        pos = allocator.claim_low()
        assert pos == 0
        assert allocator.is_gap_empty()
        assert allocator.claim_high() is None

    def test_single_commit_repo_exactly_once_high_first(self):
        allocator = FrontierAllocator(1)
        pos = allocator.claim_high()
        assert pos == 0
        assert allocator.is_gap_empty()
        assert allocator.claim_low() is None


class TestFrontierAllocatorClaiming:
    def test_claim_low_grows_authoritative_interval_upward(self):
        allocator = FrontierAllocator(10)
        assert allocator.claim_low() == 0
        assert allocator.claim_low() == 1
        assert allocator.claim_low() == 2
        assert allocator.intervals() == [Interval(0, 2, TAG_AUTHORITATIVE)]

    def test_claim_high_grows_provisional_interval_downward(self):
        allocator = FrontierAllocator(10)
        assert allocator.claim_high() == 9
        assert allocator.claim_high() == 8
        assert allocator.intervals() == [Interval(8, 9, TAG_PROVISIONAL)]

    def test_streams_converge_and_stay_separate_by_tag(self):
        allocator = FrontierAllocator(4)
        assert allocator.claim_low() == 0
        assert allocator.claim_high() == 3
        assert allocator.claim_low() == 1
        assert allocator.claim_high() == 2
        assert allocator.is_gap_empty()
        assert sorted(allocator.intervals(), key=lambda iv: iv.lo_pos) == [
            Interval(0, 1, TAG_AUTHORITATIVE),
            Interval(2, 3, TAG_PROVISIONAL),
        ]

    def test_seeded_authoritative_interval_extends_correctly(self):
        allocator = FrontierAllocator(10, [Interval(0, 4, TAG_AUTHORITATIVE)])
        assert allocator.claim_low() == 5
        assert allocator.intervals() == [Interval(0, 5, TAG_AUTHORITATIVE)]
