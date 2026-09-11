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
from typing import List, Optional, Tuple

TAG_AUTHORITATIVE = "authoritative"
TAG_PROVISIONAL = "provisional"


@dataclass
class Interval:
    lo_pos: int
    hi_pos: int
    tag: str
    anchor_pos: Optional[int] = None
    is_base: bool = False
    # #325 review round 3 (Finding 3): the caller's persisted-entity identity
    # for this interval, opaque to the allocator (it never reads or computes
    # one, only carries it through _extend/_coalesce the same way it already
    # carries anchor_pos). Set by the caller on a LOADED interval whose real
    # ident cannot be re-derived from anchor_pos alone -- an extra interval
    # whose anchor hash has dropped out of the current linearization falls
    # back to anchor_pos=hi_pos for gap math, but its ON-DISK ident still
    # names the ORIGINAL (now-unresolvable) anchor hash. Re-deriving the
    # ident from that fallback anchor_pos would mint a DIFFERENT ident and
    # create a second live entity for the same region. None for an interval
    # created fresh during a run (anchor_pos there IS the creation position,
    # in THIS linearization, always safely re-derivable).
    ident: Optional[str] = None


@dataclass
class ClaimResult:
    """What a claim did, for the persistence layer to mirror without
    re-reading the persisted interval set once per commit.

    `interval` is the post-coalesce interval the position now belongs to;
    `absorbed` are the intervals that coalesce merged away, whose persisted
    entities must be retracted in the same write.
    """
    pos: int
    interval: "Interval"
    absorbed: List["Interval"]


def coalesce_intervals(
    intervals: List[Interval], tag: str
) -> Tuple[List[Interval], List[Interval]]:
    """Merge same-tag intervals that overlap or touch. Returns
    (every interval sorted by lo_pos with `tag`'s merged, the intervals
    merged AWAY so the persistence layer can retract their entities).

    Module-level rather than a method (#329) because _frontier_load needs
    the merge on a set it has just built from graph facts, with no claim in
    sight: _coalesce ran only from _extend, and _extend only from a claim,
    so two contiguous LOADED intervals with an already-empty gap never
    merged. Both callers share this one function so the load-time merge
    cannot drift from the claim-time merge -- it IS the claim-time merge.

    Survivor rule: the base wins if either participant is base; otherwise
    the LOWER one wins and keeps its anchor_pos. The base is what persists
    at the fixed :ingestion/frontier-high ident, so it must survive every
    merge it takes part in or that ident would have to be re-pointed at a
    different entity. The keeper's `ident` travels with it the same way
    anchor_pos does -- both name the surviving entity's identity, opaque to
    this module either way.

    Only same-tag intervals merge -- the authoritative/provisional boundary
    is the lineage frontier later phases read, and must survive the two
    sides becoming adjacent.
    """
    same = sorted((iv for iv in intervals if iv.tag == tag), key=lambda iv: iv.lo_pos)
    merged: List[Interval] = []
    absorbed: List[Interval] = []
    for iv in same:
        if merged and iv.lo_pos <= merged[-1].hi_pos + 1:
            prev = merged[-1]
            keeper, loser = (prev, iv) if (prev.is_base or not iv.is_base) else (iv, prev)
            absorbed.append(loser)
            merged[-1] = Interval(
                prev.lo_pos, max(prev.hi_pos, iv.hi_pos), tag,
                keeper.anchor_pos, prev.is_base or iv.is_base, keeper.ident,
            )
        else:
            merged.append(iv)
    others = [iv for iv in intervals if iv.tag != tag]
    return sorted(others + merged, key=lambda iv: iv.lo_pos), absorbed


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

    Positions accumulate into tagged intervals: authoritative grows upward
    from claim_low(), provisional grows downward from claim_high(). Same-tag
    intervals coalesce on touch/overlap, but authoritative and provisional
    are never merged into each other even once adjacent -- the boundary
    between them is the lineage-authority frontier later phases read. The
    provisional side can hold more than one interval at once (a reload of a
    persisted high interval that no longer reaches the tip, plus a fresh one
    opened above it by new commits) -- see _adjacent_interval. The gap is
    the complement of the interval set, not the space between two anchored
    intervals; see _unclaimed.
    """

    def __init__(self, total_positions: int, intervals: Optional[List[Interval]] = None):
        self.total_positions = total_positions
        self._intervals: List[Interval] = list(intervals or [])
        self.last_claim: Optional[ClaimResult] = None

    def _unclaimed(self) -> List[Tuple[int, int]]:
        """Maximal runs of unclaimed positions, ascending. The gap is the
        COMPLEMENT of the interval set, not the space between two anchored
        intervals -- a fragmented provisional side has more than one hole,
        and the old 'interval covering position 0 / the last position'
        definition hands out positions an interval already owns."""
        holes: List[Tuple[int, int]] = []
        cursor = 0
        for iv in sorted(self._intervals, key=lambda i: i.lo_pos):
            if iv.lo_pos > cursor:
                holes.append((cursor, iv.lo_pos - 1))
            cursor = max(cursor, iv.hi_pos + 1)
        if cursor <= self.total_positions - 1:
            holes.append((cursor, self.total_positions - 1))
        return holes

    @property
    def gap_lo(self) -> int:
        holes = self._unclaimed()
        return holes[0][0] if holes else self.total_positions

    @property
    def gap_hi(self) -> int:
        holes = self._unclaimed()
        return holes[-1][1] if holes else -1

    def is_gap_empty(self) -> bool:
        return not self._unclaimed()

    def unclaimed_count(self) -> int:
        """How many positions this allocator will still hand out -- summed
        over every hole of the complement, never `gap_hi - gap_lo + 1`,
        which counts the claimed intervals between holes too (#222 phase 4:
        RunProgress's `to_retire`)."""
        return sum(hi - lo + 1 for lo, hi in self._unclaimed())

    def intervals(self) -> List[Interval]:
        return list(self._intervals)

    def _interval_covering(self, pos: int, tag: Optional[str] = None) -> Optional[Interval]:
        """The interval containing pos, optionally restricted to one tag.

        `tag` exists for _extend's post-coalesce lookup: after a merge, more
        than one tag's intervals can sit in self._intervals, and only the
        same-tag one is the survivor a claim just landed in.
        """
        for iv in self._intervals:
            if tag is not None and iv.tag != tag:
                continue
            if iv.lo_pos <= pos <= iv.hi_pos:
                return iv
        return None

    def claim_low(self) -> Optional[int]:
        """Serve ONLY the hole immediately adjacent to the authoritative
        interval's own edge -- `gap_lo == authoritative.hi_pos + 1`, or
        `gap_lo == 0` when no authoritative interval exists yet. Never the
        lowest unclaimed position in general.

        #325 review: "strictly ascending" was believed sufficient because
        `_forward_apply`'s own precondition only needs an ascending
        sequence. It is not sufficient -- the forward stream's real
        contract is CONTIGUOUS FROM C0, and three things outside the
        allocator read it that way: `:ingestion/watermark`,
        `_preload_known_entities`'s `watermark_pos` bound, and
        `:ingestion/lineage-confirmed-through`. Once the bulk gap between
        the authoritative and provisional regions closes, any further hole
        (a retained provisional interval's own tip growth, or a fresh gap
        above it) is NOT adjacent to what the forward stream has actually
        walked. Serving it would jump the forward walk over positions it
        has never visited, and `:ingestion/watermark` would then assert a
        contiguity it does not have: a later commit that merely re-touches
        an entity introduced inside the skipped-over region reads as new to
        the forward walk's watermark-bounded preload, minting a DUPLICATE
        `:introduced-by` -- caught end-to-end by
        TestMultiStreamParityWithForwardOnly, which master could never
        reach because there was only ever one gap.

        So once the bulk gap closes, the forward stream is simply finished
        for the run: it has nothing legitimate to do above the provisional
        region until that region folds into the authoritative one (a later
        task's job, not the allocator's). `claim_high()` is unchanged --
        serving the topmost hole and falling through to the bulk gap is the
        whole point of #325, and the reverse stream carries no contiguity
        contract.
        """
        if self.is_gap_empty():
            return None
        pos = self.gap_lo
        authoritative = next(
            (iv for iv in self._intervals if iv.tag == TAG_AUTHORITATIVE), None
        )
        if authoritative is not None:
            if pos != authoritative.hi_pos + 1:
                return None
        elif pos != 0:
            return None
        self._extend(pos, tag=TAG_AUTHORITATIVE, from_low=True)
        return pos

    def claim_high(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_hi
        self._extend(pos, tag=TAG_PROVISIONAL, from_low=False)
        return pos

    def _adjacent_interval(self, pos: int, tag: str, from_low: bool) -> Optional[Interval]:
        """The same-tag interval this claim should grow: the one whose edge
        touches pos from the direction of growth.

        Deliberately NOT "the first interval covering the neighbour
        position" (#222 phase 2b1). Those differ once a side holds two
        intervals, which happens whenever a run reloads a persisted high
        interval that no longer reaches the last position -- new commits
        landed on HEAD since. There, claim_high() opens a second provisional
        interval near the top while the reloaded one sits lower, and picking
        the first *covering* interval can select the lower one and rewrite it
        to itself: gap_hi never moves, claim_high() returns the same position
        forever, and _reverse_bulk_fill_walk spins on it, re-parsing and
        re-fsyncing every iteration.
        """
        for iv in self._intervals:
            if iv.tag != tag:
                continue
            touches = (iv.hi_pos == pos - 1) if from_low else (iv.lo_pos == pos + 1)
            if touches:
                return iv
        return None

    def _coalesce(self, tag: str) -> List[Interval]:
        """Claim-time merge. The rule itself lives in the module-level
        coalesce_intervals (#329), shared with mcp_server._frontier_load's
        load-time merge so the two cannot drift."""
        self._intervals, absorbed = coalesce_intervals(self._intervals, tag)
        return absorbed

    def _extend(self, pos: int, tag: str, from_low: bool) -> None:
        target = self._adjacent_interval(pos, tag, from_low)
        if target is not None:
            idx = next(i for i, iv in enumerate(self._intervals) if iv is target)
            if from_low:
                grown = Interval(
                    target.lo_pos, pos, tag, target.anchor_pos, target.is_base, target.ident,
                )
            else:
                grown = Interval(
                    pos, target.hi_pos, tag, target.anchor_pos, target.is_base, target.ident,
                )
            self._intervals[idx] = grown
        else:
            # A brand-new interval. It is the base only if no same-tag
            # interval exists yet -- claim_high() serves the topmost hole, so
            # every later provisional interval is created ABOVE the base.
            is_base = not any(iv.tag == tag for iv in self._intervals)
            grown = Interval(pos, pos, tag, anchor_pos=pos, is_base=is_base)
            self._intervals.append(grown)
        absorbed = self._coalesce(tag)
        surviving = self._interval_covering(pos, tag=tag)
        self.last_claim = ClaimResult(pos=pos, interval=surviving, absorbed=absorbed)
