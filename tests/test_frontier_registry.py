"""Tests for frontier_registry.py -- real git subprocess calls, no mocking.

This module has no DB dependency, so its own real dependency (git) is what
gets exercised for real here, matching the spirit of
docs/testing-conventions.md's real-backend rule.
"""
import itertools
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
        # A brand-new interval is the base for its tag (#325: is_base marks
        # the first same-tag interval, anchor_pos the position it was
        # created at) -- so the grown interval carries real identity, not
        # the dataclass defaults.
        assert allocator.intervals() == [
            Interval(0, 2, TAG_AUTHORITATIVE, anchor_pos=0, is_base=True)
        ]

    def test_claim_high_grows_provisional_interval_downward(self):
        allocator = FrontierAllocator(10)
        assert allocator.claim_high() == 9
        assert allocator.claim_high() == 8
        assert allocator.intervals() == [
            Interval(8, 9, TAG_PROVISIONAL, anchor_pos=9, is_base=True)
        ]

    def test_streams_converge_and_stay_separate_by_tag(self):
        allocator = FrontierAllocator(4)
        assert allocator.claim_low() == 0
        assert allocator.claim_high() == 3
        assert allocator.claim_low() == 1
        assert allocator.claim_high() == 2
        assert allocator.is_gap_empty()
        assert sorted(allocator.intervals(), key=lambda iv: iv.lo_pos) == [
            Interval(0, 1, TAG_AUTHORITATIVE, anchor_pos=0, is_base=True),
            Interval(2, 3, TAG_PROVISIONAL, anchor_pos=3, is_base=True),
        ]

    def test_seeded_authoritative_interval_extends_correctly(self):
        allocator = FrontierAllocator(10, [Interval(0, 4, TAG_AUTHORITATIVE)])
        assert allocator.claim_low() == 5
        assert allocator.intervals() == [Interval(0, 5, TAG_AUTHORITATIVE)]


class TestFrontierAllocatorGrownLinearization:
    """A run that claims from the high end, followed by new commits landing
    on HEAD, reloads a persisted high interval that no longer reaches the
    last position -- so claim_high() opens a SECOND provisional interval and
    _extend has to choose between them. Choosing the first *covering*
    interval rather than the one adjacent in the direction of growth pins
    gap_hi forever, which turns _reverse_bulk_fill_walk's `while True` into
    an unbounded fsync loop (see the 2b review, "never terminates on any
    incremental re-ingest")."""

    def test_claim_high_strictly_decreases_and_terminates(self):
        allocator = FrontierAllocator(5, [Interval(1, 2, TAG_PROVISIONAL)])
        seen = []
        for _ in range(20):  # bounded so a regression fails instead of hanging
            pos = allocator.claim_high()
            if pos is None:
                break
            seen.append(pos)
        assert seen == sorted(set(seen), reverse=True), (
            f"claim_high() must strictly decrease; got {seen}"
        )
        assert allocator.is_gap_empty()

    def test_claim_low_strictly_increases_and_terminates(self):
        # #325 review: seeded at [0, 2], not [2, 3] -- claim_low() now only
        # ever serves the hole adjacent to the authoritative interval's own
        # edge (see its docstring), and an authoritative interval that does
        # not start at position 0 is unreachable in the real system anyway
        # (frontier-low is always seeded from linearization[0]). A seed
        # starting mid-linearization would make claim_low() correctly
        # refuse forever, which is a different property than this test
        # exists to check.
        allocator = FrontierAllocator(5, [Interval(0, 2, TAG_AUTHORITATIVE)])
        seen = []
        for _ in range(20):
            pos = allocator.claim_low()
            if pos is None:
                break
            seen.append(pos)
        assert seen == sorted(set(seen)), f"claim_low() must strictly increase; got {seen}"
        assert allocator.is_gap_empty()

    def test_same_tag_intervals_coalesce_instead_of_overlapping(self):
        allocator = FrontierAllocator(5, [Interval(1, 2, TAG_PROVISIONAL)])
        for _ in range(20):  # bounded: an unbounded loop would hang, not fail
            if allocator.claim_high() is None:
                break
        ivs = allocator.intervals()
        for a, b in itertools.combinations(ivs, 2):
            assert a.hi_pos < b.lo_pos or b.hi_pos < a.lo_pos, (
                f"intervals must stay disjoint, got {ivs}"
            )


class TestFragmentedProvisionalSet:
    """#325: after tip growth the provisional side holds two disjoint
    intervals with a hole above the lower one. The old gap_lo/gap_hi, defined
    off 'the interval covering position 0 / the last position', hand out a
    position an interval already owns."""

    def _alloc(self):
        # 20 positions. Authoritative [0,3]; provisional base [4,11].
        # Positions 12..19 are the "new tip" hole.
        return frontier_registry.FrontierAllocator(20, [
            frontier_registry.Interval(0, 3, frontier_registry.TAG_AUTHORITATIVE,
                                       anchor_pos=0, is_base=True),
            frontier_registry.Interval(4, 11, frontier_registry.TAG_PROVISIONAL,
                                       anchor_pos=11, is_base=True),
        ])

    def test_gap_lo_does_not_return_a_claimed_position(self):
        a = self._alloc()
        assert a.gap_lo == 12

    def test_gap_hi_is_the_topmost_unclaimed_position(self):
        a = self._alloc()
        assert a.gap_hi == 19

    def test_claim_high_serves_the_topmost_gap_first(self):
        a = self._alloc()
        assert [a.claim_high() for _ in range(3)] == [19, 18, 17]

    def test_claim_high_falls_through_to_the_bulk_gap_when_the_tip_closes(self):
        # A real bulk gap below the base: authoritative [0,1], base [8,11],
        # so positions 2..7 are unclaimed underneath and 12..19 above.
        b = frontier_registry.FrontierAllocator(20, [
            frontier_registry.Interval(0, 1, frontier_registry.TAG_AUTHORITATIVE,
                                       anchor_pos=0, is_base=True),
            frontier_registry.Interval(8, 11, frontier_registry.TAG_PROVISIONAL,
                                       anchor_pos=11, is_base=True),
        ])
        for _ in range(8):           # 19..12, closing the tip hole
            b.claim_high()
        assert b.claim_high() == 7, (
            "once the tip hole merges into the base, the topmost unclaimed "
            "position is the bulk gap's top"
        )

    def test_merge_keeps_the_base_and_reports_the_absorbed_interval(self):
        a = self._alloc()
        for _ in range(7):           # 19..13
            a.claim_high()
        result_before = a.last_claim
        assert result_before.absorbed == []
        assert a.claim_high() == 12  # this claim makes [12,19] touch [4,11]
        merged = a.last_claim
        assert merged.interval.lo_pos == 4 and merged.interval.hi_pos == 19
        assert merged.interval.is_base is True
        assert merged.interval.anchor_pos == 11
        assert [iv.anchor_pos for iv in merged.absorbed] == [19]

    def test_claim_low_returns_none_once_the_bulk_gap_closes(self):
        """#325 review (post-Task-4 ruling): this replaces a test that
        asserted [12, 13, 14] here, which encoded exactly the behaviour now
        ruled wrong.

        auth[0,3] + prov[4,11] means the bulk gap between the two sides has
        ALREADY closed -- the only hole left is 12..19, above the
        provisional region, not adjacent to the authoritative interval's
        own edge (3+1=4). A claim_low() that served it anyway would jump
        the forward stream over positions 4..11, which it has never
        visited. `:ingestion/watermark`, `_preload_known_entities`'s
        `watermark_pos` bound, and `:ingestion/lineage-confirmed-through`
        all read the authoritative interval's reach as "the graph knows
        everything up to here" -- a lie once the forward walk jumps over
        unvisited territory. The costed failure mode is not abstract: a
        later commit that merely re-touches an entity introduced inside the
        skipped region reads as new to the forward walk's watermark-bounded
        preload and mints a DUPLICATE `:introduced-by`, silently corrupting
        lineage attribution. `TestMultiStreamParityWithForwardOnly` in
        tests/test_mcp_server.py caught exactly this end-to-end; master
        could never reach it because there was only ever one gap.

        So claim_low() now returns None here: the forward stream has
        nothing legitimate left to do until the provisional region folds
        into the authoritative one.
        """
        a = self._alloc()
        assert a.claim_low() is None

    def test_is_gap_empty_requires_every_hole_closed(self):
        a = self._alloc()
        assert a.is_gap_empty() is False
        for _ in range(8):
            a.claim_high()
        assert a.is_gap_empty() is True


class TestCoalesceSurvivorRuleHigherBase:
    """The merge-survivor rule (#325 code review, finding 1) has two live
    branches: base wins because it is the LOWER participant (every other
    test in this file only exercises that one, since claim_high() always
    opens a fresh fragment ABOVE an existing base), and base wins because it
    is the HIGHER participant. Nothing reaches the second branch through
    claim_low()/claim_high() -- a same-tag interval seeded above an existing
    base is not a state normal claiming produces -- so it is seeded directly
    here, the same way TestFragmentedProvisionalSet seeds a reload state.

    If `keeper, loser = (prev, iv) if (...) else (iv, prev)` in _coalesce
    ever regressed to always keeping `prev` (the lower participant), this
    test's assertions would flip silently: the merged interval would carry
    the NON-base interval's anchor_pos, and `absorbed` would name the actual
    base interval. A later task retracts every entity in `absorbed` -- so
    that inversion means retracting the live :ingestion/frontier-high entity
    while the merged interval keeps pointing persistence at an ident nothing
    still holds. `is_base` on the merged interval would stay True either way
    (it is an OR of both participants, independent of which is kept), so
    that field alone would not catch the regression -- only anchor_pos and
    the identity of the absorbed interval do.
    """

    def test_higher_base_survives_a_merge_with_a_lower_non_base_fragment(self):
        # provisional: a lower non-base fragment [2,4] (anchor_pos=99, as if
        # left over from some earlier state) and a higher base [6,11]
        # (anchor_pos=11, the persisted :ingestion/frontier-high interval).
        # Position 5 is the only gap; claiming it makes the two touch.
        a = frontier_registry.FrontierAllocator(12, [
            frontier_registry.Interval(0, 1, frontier_registry.TAG_AUTHORITATIVE,
                                       anchor_pos=0, is_base=True),
            frontier_registry.Interval(2, 4, frontier_registry.TAG_PROVISIONAL,
                                       anchor_pos=99, is_base=False),
            frontier_registry.Interval(6, 11, frontier_registry.TAG_PROVISIONAL,
                                       anchor_pos=11, is_base=True),
        ])
        assert a.claim_high() == 5
        merged = a.last_claim
        assert merged.interval.lo_pos == 2 and merged.interval.hi_pos == 11
        assert merged.interval.is_base is True
        assert merged.interval.anchor_pos == 11, (
            "the HIGHER interval is the base and must keep its anchor_pos "
            "even though the LOWER interval merged first in sort order"
        )
        assert len(merged.absorbed) == 1
        assert merged.absorbed[0].anchor_pos == 99, (
            "the lower non-base fragment is what gets absorbed/retracted, "
            "never the base"
        )
        assert merged.absorbed[0].is_base is False


class TestCoalesceIntervalsModuleLevel:
    """#329: the merge rule has to be callable WITHOUT a claim, because
    _frontier_load needs it on a set it has just built from graph facts.
    _coalesce runs only from _extend, and _extend only from a claim, so a
    loaded-but-never-claimed set never merges."""

    def test_contiguous_provisional_intervals_merge_and_report_the_absorbed(self):
        loaded = [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(11, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos) for iv in merged] == [(0, 25)]
        assert merged[0].is_base is True
        assert merged[0].ident == ":ingestion/frontier-high", (
            "the base must survive every merge it takes part in -- it is what "
            "persists at the fixed frontier-high ident"
        )
        assert [iv.ident for iv in absorbed] == [":ingestion/interval-provisional-abc"]

    def test_overlapping_provisional_intervals_merge_to_their_union(self):
        loaded = [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(5, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos) for iv in merged] == [(0, 25)]
        assert len(absorbed) == 1

    def test_disjoint_intervals_with_a_real_gap_do_not_merge(self):
        loaded = [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(12, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos) for iv in merged] == [(0, 10), (12, 25)]
        assert absorbed == []

    def test_an_authoritative_interval_is_returned_untouched_and_never_merged(self):
        """The authoritative/provisional boundary is the lineage frontier
        later phases read, and must survive the two sides being adjacent."""
        loaded = [
            Interval(0, 10, TAG_AUTHORITATIVE, anchor_pos=0, is_base=True,
                     ident=":ingestion/frontier-low"),
            Interval(11, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=True,
                     ident=":ingestion/frontier-high"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos, iv.tag) for iv in merged] == [
            (0, 10, TAG_AUTHORITATIVE), (11, 25, TAG_PROVISIONAL),
        ]
        assert absorbed == []

    def test_the_allocator_method_still_merges_on_a_claim(self):
        """_coalesce is now a wrapper -- claim-time behaviour must be
        byte-for-byte what it was, or every #325 guarantee moves."""
        alloc = FrontierAllocator(30, [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(12, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ])
        assert alloc.claim_high() == 29
        assert alloc.claim_high() == 28
        prov = [iv for iv in alloc.intervals() if iv.tag == TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 10), (12, 25), (28, 29)]
        assert alloc.claim_high() == 27
        assert alloc.claim_high() == 26
        prov = [iv for iv in alloc.intervals() if iv.tag == TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 10), (12, 29)], (
            "the claim at 26 must merge the new top interval into the "
            "extra at [12,25], and the LOWER one wins as survivor"
        )
        assert alloc.last_claim.absorbed != [], (
            "the merging claim must still report what it swallowed, or "
            "_reverse_claim_persist_target has nothing to retract"
        )


class TestUnclaimedCount:
    """#222 phase 4: RunProgress's `to_retire` is the gap AT LOAD, so the
    allocator must say how many positions it will hand out -- counted over
    every hole, not gap_hi - gap_lo, which overcounts a fragmented gap."""

    def test_empty_allocator_counts_every_position(self):
        from frontier_registry import FrontierAllocator
        assert FrontierAllocator(7).unclaimed_count() == 7

    def test_counts_every_hole_of_a_fragmented_gap(self):
        from frontier_registry import FrontierAllocator, Interval, TAG_AUTHORITATIVE, TAG_PROVISIONAL
        a = FrontierAllocator(10, [
            Interval(0, 2, TAG_AUTHORITATIVE),
            Interval(5, 5, TAG_PROVISIONAL, anchor_pos=5, is_base=True),
            Interval(8, 8, TAG_PROVISIONAL, anchor_pos=8),
        ])
        # holes: 3-4, 6-7, 9 -> 5 positions; gap_hi - gap_lo + 1 would say 7
        assert a.unclaimed_count() == 5

    def test_zero_once_every_position_is_claimed(self):
        from frontier_registry import FrontierAllocator, Interval, TAG_AUTHORITATIVE
        assert FrontierAllocator(4, [Interval(0, 3, TAG_AUTHORITATIVE)]).unclaimed_count() == 0
