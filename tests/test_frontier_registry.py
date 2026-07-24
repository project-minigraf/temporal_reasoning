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
def git_repo_clock_skewed(tmp_path):
    """Child commit dated earlier than its parent -- topo order must still
    place the parent first; date order would not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    env_parent = {**os.environ, "GIT_COMMITTER_DATE": "2026-01-10T00:00:00", "GIT_AUTHOR_DATE": "2026-01-10T00:00:00"}
    (repo / "a.py").write_text("x = 1\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "parent"], cwd=repo, check=True, capture_output=True, env=env_parent)

    env_child = {**os.environ, "GIT_COMMITTER_DATE": "2026-01-01T00:00:00", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00"}
    (repo / "b.py").write_text("y = 2\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "child"], cwd=repo, check=True, capture_output=True, env=env_child)

    return repo


class TestBuildLinearization:
    def test_returns_hashes_oldest_first(self, git_repo):
        result = _subprocess.run(
            ["git", "log", "--format=%H"], cwd=git_repo, capture_output=True, text=True, check=True
        )
        newest_first = result.stdout.strip().splitlines()
        linearization = frontier_registry.build_linearization(str(git_repo))
        assert linearization == list(reversed(newest_first))

    def test_topo_order_survives_clock_skew(self, git_repo_clock_skewed):
        linearization = frontier_registry.build_linearization(str(git_repo_clock_skewed))
        log_result = _subprocess.run(
            ["git", "log", "--topo-order", "--reverse", "--format=%H %s"],
            cwd=git_repo_clock_skewed, capture_output=True, text=True, check=True,
        )
        lines = log_result.stdout.strip().splitlines()
        expected_hashes = [line.split(" ", 1)[0] for line in lines]
        expected_subjects = [line.split(" ", 1)[1] for line in lines]
        assert expected_subjects == ["parent", "child"]
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
