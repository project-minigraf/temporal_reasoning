# Frontier/Interval Registry + Shared-Gap Allocator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational data structure for #222's converging multi-stream ingestion: a fixed topological commit linearization, an in-memory shared-gap allocator (`FrontierAllocator`), and its graph persistence — including migration from the old scalar `:ingestion/watermark`. No stream-walking logic is wired up yet; that's phase 2.

**Architecture:** A new standalone module `frontier_registry.py` (mirroring `fact_index.py`'s existing separate-module pattern) holds the pure, DB-free pieces: `Interval`, `FrontierAllocator`, and `build_linearization()` (shells out to `git log --topo-order`). `mcp_server.py` gets the DB-touching glue (`_frontier_load`, `_frontier_persist_claim`, migration) as new module-level functions following the exact style of the existing `_watermark_query`/`_watermark_update` pair, plus a fix to `_git_commits` to use `--topo-order` (a real latent bug: plain `git log --reverse` does not guarantee parent-before-child under non-monotonic committer dates).

**Tech Stack:** Python 3, `subprocess` (git), minigraf Datalog (via existing `_db_execute`/`_transact`/`_retract` helpers), pytest.

## Global Constraints

- Follow `docs/testing-conventions.md`: every test uses a real `MiniGrafDb` (via the `real_db` fixture or a real file-backed `MiniGrafDb.open()`), never a `MagicMock` fake of the DB. Real git subprocess calls for anything git-related — no mocked subprocess output.
- Design spec: `docs/superpowers/specs/2026-07-24-frontier-registry-design.md` — every task below implements one of its sections; do not deviate from the persisted schema (`:ingestion/frontier-low` / `:ingestion/frontier-high`, role-based idents) or the claim semantics without updating that spec first.
- Phase 1 only. Do not wire `_frontier_load`/`_frontier_persist_claim` into `_run_ingestion`'s actual commit loop — that is phase 2's job. This plan's deliverables are used by nothing yet except their own tests.

---

### Task 1: Fix `_git_commits` to use strict topological order

**Files:**
- Modify: `mcp_server.py:4109-4131` (`_git_commits`)
- Test: `tests/test_mcp_server.py` (extend the existing `TestGitHelpers` class, ~line 5863)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_git_commits(repo_path, watermark_hash, branch="HEAD") -> List[tuple]` — same signature and return shape as today, only the ordering guarantee changes (now strictly parent-before-child, not date-order).

- [ ] **Step 1: Write the failing test**

Add this fixture and test to `tests/test_mcp_server.py`, in `TestGitHelpers` (near the existing `git_repo` fixture at line 5841):

```python
@pytest.fixture
def git_repo_clock_skewed(tmp_path):
    """Two commits where the CHILD's committer date is earlier than its
    PARENT's -- simulates clock skew / a rebase. Plain chronological
    ordering (git log's default, newest-first by date) would list the
    parent before the child in the non-reversed traversal (parent has the
    later date), so --reverse alone flips that to [child, parent] -- wrong,
    since child is structurally after parent. --topo-order must still
    produce [parent, child] regardless of the date skew.
    """
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


class TestGitCommitsTopoOrder:
    def test_orders_by_topology_not_committer_date(self, git_repo_clock_skewed):
        import mcp_server
        commits = mcp_server._git_commits(str(git_repo_clock_skewed), watermark_hash=None)
        subjects = [c[3] for c in commits]
        assert subjects == ["parent", "child"]
```

`os` is already imported at the top of `tests/test_mcp_server.py` (line 22) — no new import needed.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestGitCommitsTopoOrder::test_orders_by_topology_not_committer_date -v`
Expected: FAIL — `assert ['child', 'parent'] == ['parent', 'child']` (today's plain `--reverse`, no `--topo-order`, produces the buggy date-sorted order).

- [ ] **Step 3: Add `--topo-order` to the git log invocation**

In `mcp_server.py`, in `_git_commits` (currently lines 4109-4131), change:

```python
    result = _subprocess.run(
        ["git", "log", "--reverse", "--format=%H %at %ae %s", range_spec],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
```

to:

```python
    result = _subprocess.run(
        ["git", "log", "--topo-order", "--reverse", "--format=%H %at %ae %s", range_spec],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py::TestGitCommitsTopoOrder::test_orders_by_topology_not_committer_date tests/test_mcp_server.py::TestGitHelpers -v`
Expected: PASS (all of `TestGitHelpers`' existing tests must still pass unchanged — `--topo-order` agrees with plain chronological order on ordinary, non-skewed history).

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Fix #222 phase 1: _git_commits uses --topo-order, not date order

Plain \`git log --reverse\` (no --topo-order) does not guarantee
parent-before-child when committer dates are non-monotonic (clock skew,
rebases) -- a real latent bug the #222 design spec's frontier registry
depends on being fixed, since interval positions require a stable
topological linearization."
```

---

### Task 2: `frontier_registry.py` — `Interval`, `FrontierAllocator`, `build_linearization`

**Files:**
- Create: `frontier_registry.py`
- Test: `tests/test_frontier_registry.py`

**Interfaces:**
- Consumes: nothing (standalone module, no dependency on `mcp_server.py` or minigraf).
- Produces:
  - `frontier_registry.TAG_AUTHORITATIVE: str = "authoritative"`, `frontier_registry.TAG_PROVISIONAL: str = "provisional"`
  - `frontier_registry.Interval(lo_pos: int, hi_pos: int, tag: str)` — a dataclass with value equality.
  - `frontier_registry.build_linearization(repo_path: str, branch: str = "HEAD") -> List[str]`
  - `frontier_registry.FrontierAllocator(total_positions: int, intervals: Optional[List[Interval]] = None)` with:
    - `.total_positions: int`
    - `.gap_lo -> int`, `.gap_hi -> int` (properties)
    - `.is_gap_empty() -> bool`
    - `.intervals() -> List[Interval]`
    - `.claim_low() -> Optional[int]`, `.claim_high() -> Optional[int]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_frontier_registry.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_frontier_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'frontier_registry'` (module doesn't exist yet).

- [ ] **Step 3: Write `frontier_registry.py`**

Create `frontier_registry.py`:

```python
"""Frontier/interval registry + shared-gap allocator for #222 phase 1.

Represents ingestion progress as a small set of disjoint, tagged intervals
over a fixed topological commit linearization, rather than a single scalar
watermark -- the foundation phase 2 builds concurrent forward-truth /
reverse-bulk-fill streams on top of. See
docs/superpowers/specs/2026-07-24-frontier-registry-design.md.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional

TAG_AUTHORITATIVE = "authoritative"
TAG_PROVISIONAL = "provisional"


@dataclass
class Interval:
    lo_pos: int
    hi_pos: int
    tag: str


def build_linearization(repo_path: str, branch: str = "HEAD") -> List[str]:
    """Full C0..branch commit hash list in fixed topological order (oldest first).

    --topo-order guarantees parent-before-child even when committer dates are
    non-monotonic (clock skew, rebases) -- plain chronological `git log`
    order does not.
    """
    result = subprocess.run(
        ["git", "log", "--topo-order", "--reverse", "--format=%H", branch],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.strip().splitlines() if line.strip()]


class FrontierAllocator:
    """In-memory shared-gap allocator over a fixed linearization.

    Holds at most two intervals: one anchored at position 0
    (tag=authoritative, grows upward via claim_low) and one anchored at the
    last position (tag=provisional, grows downward via claim_high). They are
    never merged into each other even once adjacent -- the boundary between
    them is the lineage-authority frontier later phases read.
    """

    def __init__(self, total_positions: int, intervals: Optional[List[Interval]] = None):
        self.total_positions = total_positions
        self._intervals: List[Interval] = list(intervals or [])

    @property
    def gap_lo(self) -> int:
        low = self._interval_covering(0)
        return low.hi_pos + 1 if low else 0

    @property
    def gap_hi(self) -> int:
        if self.total_positions == 0:
            return -1
        last = self.total_positions - 1
        high = self._interval_covering(last)
        return high.lo_pos - 1 if high else last

    def is_gap_empty(self) -> bool:
        return self.gap_lo > self.gap_hi

    def intervals(self) -> List[Interval]:
        return list(self._intervals)

    def _interval_covering(self, pos: int) -> Optional[Interval]:
        for iv in self._intervals:
            if iv.lo_pos <= pos <= iv.hi_pos:
                return iv
        return None

    def claim_low(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_lo
        self._extend(pos, tag=TAG_AUTHORITATIVE, from_low=True)
        return pos

    def claim_high(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_hi
        self._extend(pos, tag=TAG_PROVISIONAL, from_low=False)
        return pos

    def _extend(self, pos: int, tag: str, from_low: bool) -> None:
        neighbor_pos = pos - 1 if from_low else pos + 1
        existing = self._interval_covering(neighbor_pos)
        if existing is not None and existing.tag == tag:
            idx = self._intervals.index(existing)
            if from_low:
                self._intervals[idx] = Interval(existing.lo_pos, pos, tag)
            else:
                self._intervals[idx] = Interval(pos, existing.hi_pos, tag)
        else:
            self._intervals.append(Interval(pos, pos, tag))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_frontier_registry.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add frontier_registry.py tests/test_frontier_registry.py
git commit -m "Add frontier_registry.py: Interval, FrontierAllocator, build_linearization

Standalone module (no DB dependency, mirrors fact_index.py's existing
separate-module pattern) implementing #222 phase 1's shared-gap
allocator: claim_low/claim_high atomically extend the low-anchored
authoritative / high-anchored provisional intervals, with gap_lo/gap_hi
as pure readouts of the interval set (never a value one stream compares
against another's), per the design spec."
```

---

### Task 3: `_frontier_load` + migration from `:ingestion/watermark`

**Files:**
- Modify: `mcp_server.py` — add `import frontier_registry` near line 32 (next to `import fact_index`), and add new functions after `_watermark_update` (currently ends at line 4909, immediately before `_LAST_RUN_KEYWORD_ATTRS` at line 4912).
- Test: `tests/test_mcp_server.py` — add `import frontier_registry` near the top, and a new `TestFrontierLoad` class.

**Interfaces:**
- Consumes: `frontier_registry.FrontierAllocator`, `frontier_registry.Interval`, `frontier_registry.TAG_AUTHORITATIVE`, `frontier_registry.TAG_PROVISIONAL` (Task 2). `_watermark_query(db) -> Optional[str]`, `_db_execute(db, datalog) -> str`, `_transact(db, datalog_facts, valid_from, ..., index_con=None) -> str`, `_edn_escape(s) -> str` (all pre-existing in `mcp_server.py`).
- Produces:
  - `_FRONTIER_LOW_IDENT: str = ":ingestion/frontier-low"`, `_FRONTIER_HIGH_IDENT: str = ":ingestion/frontier-high"`
  - `_frontier_read_bounds(db, ident: str) -> Optional[Tuple[str, str]]`
  - `_frontier_seed_from_watermark(db, linearization: List[str], run_ts_iso: str, index_con=None) -> None`
  - `_frontier_load(db, linearization: List[str], run_ts_iso: str, index_con=None) -> frontier_registry.FrontierAllocator`

- [ ] **Step 1: Write the failing tests**

Add near the top of `tests/test_mcp_server.py` (alongside the other module-level imports, e.g. right after `from minigraf import MiniGrafError`):

```python
import frontier_registry
```

Add this new test class anywhere in `tests/test_mcp_server.py` (e.g. right after the `TestGitHelpers` class):

```python
class TestFrontierLoad:
    def test_migrates_from_watermark_when_no_intervals_exist(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2", "h3"]
        mcp_server._watermark_update(db, "h1", "2026-01-01T00:00:00Z", "seed watermark")

        allocator = mcp_server._frontier_load(db, linearization, "2026-01-02T00:00:00Z")

        assert allocator.total_positions == 4
        assert allocator.intervals() == [
            frontier_registry.Interval(0, 1, frontier_registry.TAG_AUTHORITATIVE)
        ]
        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT) == ("h0", "h1")

    def test_second_load_does_not_duplicate_migration(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]
        mcp_server._watermark_update(db, "h1", "2026-01-01T00:00:00Z", "seed watermark")

        mcp_server._frontier_load(db, linearization, "2026-01-02T00:00:00Z")
        second = mcp_server._frontier_load(db, linearization, "2026-01-03T00:00:00Z")

        assert second.intervals() == [
            frontier_registry.Interval(0, 1, frontier_registry.TAG_AUTHORITATIVE)
        ]

    def test_no_watermark_no_intervals_yields_empty_allocator(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1"]

        allocator = mcp_server._frontier_load(db, linearization, "2026-01-01T00:00:00Z")

        assert allocator.intervals() == []
        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT) is None

    def test_empty_linearization_yields_empty_allocator(self, real_db):
        import mcp_server
        db = real_db
        allocator = mcp_server._frontier_load(db, [], "2026-01-01T00:00:00Z")
        assert allocator.total_positions == 0
        assert allocator.intervals() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestFrontierLoad -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_frontier_load'`.

- [ ] **Step 3: Add the import and new functions to `mcp_server.py`**

Add the import right after `import fact_index` (line 32):

```python
import fact_index
import frontier_registry
```

Insert the following block into `mcp_server.py` immediately after `_watermark_update` ends (line 4909, right before the blank lines preceding `_LAST_RUN_KEYWORD_ATTRS` at line 4912):

```python
_FRONTIER_LOW_IDENT = ":ingestion/frontier-low"
_FRONTIER_HIGH_IDENT = ":ingestion/frontier-high"


def _frontier_read_bounds(db: Any, ident: str) -> Optional[Tuple[str, str]]:
    """Return (lo_hash, hi_hash) for ident's :type/ingest-interval fact, or
    None if that interval hasn't been created yet."""
    raw = _db_execute(
        db,
        f"(query [:find ?lo ?hi :where [{ident} :lo-hash ?lo] [{ident} :hi-hash ?hi]])",
    )
    results = json.loads(raw).get("results", [])
    return (results[0][0], results[0][1]) if results else None


def _frontier_seed_from_watermark(
    db: Any, linearization: List[str], run_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """One-time migration: seed :ingestion/frontier-low as [C0, W] tagged
    authoritative from the old scalar :ingestion/watermark. No-op if
    frontier-low already exists or there is no watermark to migrate from
    (see the #222 phase-1 design spec's "Migration" section).
    """
    if _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is not None:
        return
    watermark_hash = _watermark_query(db)
    if watermark_hash is None or not linearization:
        return
    facts = [
        f"[{_FRONTIER_LOW_IDENT} :entity-type :type/ingest-interval]",
        f'[{_FRONTIER_LOW_IDENT} :lo-hash "{linearization[0]}"]',
        f'[{_FRONTIER_LOW_IDENT} :hi-hash "{_edn_escape(watermark_hash)}"]',
        f"[{_FRONTIER_LOW_IDENT} :tag :authoritative]",
    ]
    _transact(db, "[" + " ".join(facts) + "]", run_ts_iso, index_con=index_con)


def _frontier_load(
    db: Any, linearization: List[str], run_ts_iso: str, index_con: Optional[Any] = None
) -> "frontier_registry.FrontierAllocator":
    """Reconstruct a FrontierAllocator from persisted graph facts, migrating
    a pre-#222 watermark-only graph on first load. See the design spec's
    "Migration" and "Graph persistence schema" sections.
    """
    if not linearization:
        return frontier_registry.FrontierAllocator(0, [])

    if (
        _frontier_read_bounds(db, _FRONTIER_LOW_IDENT) is None
        and _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT) is None
    ):
        _frontier_seed_from_watermark(db, linearization, run_ts_iso, index_con=index_con)

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    intervals: List[frontier_registry.Interval] = []
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    if low_bounds is not None and low_bounds[0] in hash_to_pos and low_bounds[1] in hash_to_pos:
        intervals.append(frontier_registry.Interval(
            hash_to_pos[low_bounds[0]], hash_to_pos[low_bounds[1]], frontier_registry.TAG_AUTHORITATIVE
        ))
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is not None and high_bounds[0] in hash_to_pos and high_bounds[1] in hash_to_pos:
        intervals.append(frontier_registry.Interval(
            hash_to_pos[high_bounds[0]], hash_to_pos[high_bounds[1]], frontier_registry.TAG_PROVISIONAL
        ))
    return frontier_registry.FrontierAllocator(len(linearization), intervals)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestFrontierLoad -v`
Expected: PASS (all 4 tests).

Also run the full existing suite to confirm no regression from the new import / inserted functions:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS (no new failures).

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _frontier_load + migration from :ingestion/watermark

Reconstructs a frontier_registry.FrontierAllocator from persisted
:ingestion/frontier-low / :ingestion/frontier-high graph facts, seeding
a one-time [C0, W] authoritative interval from the old scalar watermark
when neither exists yet -- per #222 phase-1 design spec's Migration
section. Not yet wired into _run_ingestion (phase 2)."
```

---

### Task 4: `_frontier_persist_claim`

**Files:**
- Modify: `mcp_server.py` — add the new function immediately after `_frontier_load` (from Task 3).
- Test: `tests/test_mcp_server.py` — new `TestFrontierPersistClaim` class.

**Interfaces:**
- Consumes: `_frontier_read_bounds`, `_FRONTIER_LOW_IDENT`, `_FRONTIER_HIGH_IDENT` (Task 3); `_retract(db, datalog_facts, index_con=None) -> str`, `_transact`, `_edn_escape` (pre-existing).
- Produces: `_frontier_persist_claim(db, linearization: List[str], pos: int, from_low: bool, commit_ts_iso: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_mcp_server.py`:

```python
class TestFrontierPersistClaim:
    def test_first_claim_from_low_creates_interval(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]

        mcp_server._frontier_persist_claim(db, linearization, 0, from_low=True, commit_ts_iso="2026-01-01T00:00:00Z")

        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT) == ("h0", "h0")

    def test_second_claim_from_low_extends_hi_hash_only(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2"]

        mcp_server._frontier_persist_claim(db, linearization, 0, from_low=True, commit_ts_iso="2026-01-01T00:00:00Z")
        mcp_server._frontier_persist_claim(db, linearization, 1, from_low=True, commit_ts_iso="2026-01-01T00:00:01Z")

        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT) == ("h0", "h1")

    def test_claim_from_high_is_tracked_separately_from_low(self, real_db):
        import mcp_server
        db = real_db
        linearization = ["h0", "h1", "h2", "h3"]

        mcp_server._frontier_persist_claim(db, linearization, 0, from_low=True, commit_ts_iso="2026-01-01T00:00:00Z")
        mcp_server._frontier_persist_claim(db, linearization, 3, from_low=False, commit_ts_iso="2026-01-01T00:00:01Z")
        mcp_server._frontier_persist_claim(db, linearization, 2, from_low=False, commit_ts_iso="2026-01-01T00:00:02Z")

        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT) == ("h0", "h0")
        assert mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_HIGH_IDENT) == ("h2", "h3")

    def test_claim_persists_across_reopen(self, tmp_path):
        """Real file-backed DB, closed and reopened -- proves the claim
        survives a genuine process restart, not just an in-memory read
        within the same open() call (docs/testing-conventions.md Pattern 2).
        """
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(tmp_path / "t.graph"))
        db = mcp_server.get_db()
        linearization = ["h0", "h1", "h2"]

        mcp_server._frontier_persist_claim(db, linearization, 0, from_low=True, commit_ts_iso="2026-01-01T00:00:00Z")
        mcp_server._db_checkpoint(db)
        mcp_server._db = None  # release lock, force a genuine reopen below

        mcp_server.open_db(str(tmp_path / "t.graph"))
        reopened_db = mcp_server.get_db()
        allocator = mcp_server._frontier_load(reopened_db, linearization, "2026-01-02T00:00:00Z")

        assert allocator.intervals() == [
            frontier_registry.Interval(0, 0, frontier_registry.TAG_AUTHORITATIVE)
        ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestFrontierPersistClaim -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_frontier_persist_claim'`.

- [ ] **Step 3: Add `_frontier_persist_claim` to `mcp_server.py`**

Insert immediately after `_frontier_load` (end of Task 3's insertion):

```python
def _frontier_persist_claim(
    db: Any,
    linearization: List[str],
    pos: int,
    from_low: bool,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Persist a single claimed position by extending the correct fixed-ident
    interval fact -- retracts+reasserts only the moved bound, mirroring
    _watermark_update's per-commit cost profile (see the design spec's
    "Persistence timing" and "Graph persistence schema" sections).
    """
    ident = _FRONTIER_LOW_IDENT if from_low else _FRONTIER_HIGH_IDENT
    tag = ":authoritative" if from_low else ":provisional"
    moved_hash = linearization[pos]
    existing = _frontier_read_bounds(db, ident)

    to_retract: List[str] = []
    to_transact: List[str] = []
    if existing is None:
        to_transact.append(f"[{ident} :entity-type :type/ingest-interval]")
        to_transact.append(f"[{ident} :tag {tag}]")
        to_transact.append(f'[{ident} :lo-hash "{_edn_escape(moved_hash)}"]')
        to_transact.append(f'[{ident} :hi-hash "{_edn_escape(moved_hash)}"]')
    else:
        lo_hash, hi_hash = existing
        if from_low:
            to_retract.append(f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]')
            to_transact.append(f'[{ident} :hi-hash "{_edn_escape(moved_hash)}"]')
        else:
            to_retract.append(f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]')
            to_transact.append(f'[{ident} :lo-hash "{_edn_escape(moved_hash)}"]')

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestFrontierPersistClaim -v`
Expected: PASS (all 4 tests).

Also run the full existing suite to confirm no regression:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS.

And the standalone module's own tests, to confirm nothing here broke them:

Run: `python -m pytest tests/test_frontier_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _frontier_persist_claim: per-claim retract+reassert of one bound

Completes #222 phase 1: the frontier/interval registry and shared-gap
allocator are now fully persistable and reloadable, including surviving
a genuine process restart (file-backed DB close/reopen). Phase 2 wires
this into _run_ingestion's actual Stream 1/2 walk."
```

## Self-Review Notes

- **Spec coverage:** Linearization (Task 1) — covered. In-memory registry + allocator mechanics + degenerate cases (Task 2) — covered. Graph persistence schema + migration (Task 3) — covered. Persistence timing / per-claim retract+reassert (Task 4) — covered, including the reopen-based round-trip the spec's Testing section calls for. Explicitly-deferred items (Stream 1/2/3 wiring, status reporting, DAG/force-push hardening) are correctly left untouched.
- **Type consistency:** `Interval(lo_pos, hi_pos, tag)` field names and `FrontierAllocator` method names (`claim_low`, `claim_high`, `gap_lo`, `gap_hi`, `is_gap_empty`, `intervals`, `total_positions`) are used identically across Tasks 2, 3, and 4. `_frontier_read_bounds`/`_FRONTIER_LOW_IDENT`/`_FRONTIER_HIGH_IDENT` introduced in Task 3 are reused verbatim in Task 4.
- **No placeholders:** every step has complete, runnable code — no TBD/TODO markers, no "similar to Task N" shorthand.
