# evals/at_scale/probe_dep_preload_exposure.py
"""#245 exposure probe: how much does the :depends-on preload's ts(W) bound
actually misclassify against real history?

#238 replaced the forward-walk entity preload's author-date bound with a
position-indexed one. That fix reached three of four preload sites.
_preload_known_deps and _preload_pinned_commits stayed at ts(W) -- the
watermark commit's own author date -- because those facts carry no commit
reference to join a :hash to, and so admit no position clause.

This probe MEASURES that residual against real history, and -- as of #245 --
also VERIFIES the fix: the fixed preloads accept position arguments
(ts_positions, watermark_pos, t_hi_ms), and --verify-fix (sweep's verify_fix
parameter) drives them instead of the date-only call. An earlier version of
this docstring said the oracle's inversion (invert_ms_to_positions,
edge_live_at -> position_exact_live_edges) was NOT a candidate fix; that was
true against the pre-#246 shape this probe was conceived against, where
build_linearization and _git_commits ran BELOW the preload block. It is false
against current master: PR #246 moved both above the preload block, which is
what makes full-history commit_metadata available at preload time and is
exactly what lets a resuming forward walk perform the identical
timestamp-to-position inversion with only the watermark in hand
(mcp_server._position_of_valid_time, _fact_is_live_at_position). That
inversion IS the basis of the shipped fix.

What still separates the oracle from the fix, and must stay separated, is the
ambiguity policy: edge_live_at resolves a collision toward NOT understating
exposure (earliest position for an ambiguous introduction, latest for an
ambiguous close), while mcp_server._position_of_valid_time resolves the same
collision the OPPOSITE way, toward the recoverable direction (latest for an
introduction, earliest for a close) -- see position_exact_live_edges'
docstring for why the two must never be unified into one helper.

--verify-fix HAS NOT BEEN RUN TO COMPLETION AGAINST FULL HISTORY (as of this
commit). An attempt against this repository's ~657-commit history reached
only 247 commits in 9.8 hours (~25 commits/hour) before being killed. A
py-spy dump of the stalled attempt showed it stuck in
mcp_server._reverse_apply -> _entity_introduced_by_query ->
_entity_introduced_by_values_query -> _db_execute -> minigraf_ffi.execute --
issue #239's pre-existing per-ident :introduced-by point-query cost, NOT the
close-side preload path this mode exists to verify (confirmed by `git diff
master..HEAD -- mcp_server.py`, which touches only _preload_known_entities,
_preload_known_deps, _preload_pinned_commits, _load_ingestion_preload_state
and its own helpers -- _reverse_apply and the :introduced-by queries are
untouched by this branch). #239 needs to be fixed before this mode's
acceptance run is practical to complete. Do not read the mode's mere
existence, its unit-test coverage, or the small-repo smoke test in this
module's own review history as evidence it has been validated at ingestion
scale -- it has not.

TWO figures, not one (final whole-branch review, CRITICAL finding). The first
version of this probe held file_entities -- the output of
_preload_known_entities -- fixed on both sides of the diff, on the claim that
#238 made that narrowing position-correct and so left the ts(W) :depends-on
bound as the single variable. That claim is FALSE. #238 made
_preload_known_entities position-correct on the INTRODUCTION side only: its
position clause keys on the introducing commit ([?e :introduced-by ?c]
[?c :hash ?hash] -> hash_to_pos, mcp_server.py:7213-7215). Its CLOSE side is
still governed by valid_at = T_hi(W), a date bound suffering the identical
author-date inversion this probe exists to measure -- the function's own
comment says so ("the date bound above only governs how widely entities closed
ABOVE the watermark are re-admitted").

Consequence: a module whose close DATE falls below ts(W) but whose close
POSITION sits above W disappears from file_entities at that W, taking its
:depends-on edges out of BOTH sides of the diff before the diff is computed.
On this repository that is four modules deleted by df6b8be at position 124,
and 30 misclassified edges that the narrow figure never saw (measured
2026-08-07: narrow 2 distinct vs wide 32, a factor of 16).

So the sweep reports both, side by side:

  NARROW -- file_entities exactly as _preload_known_entities returns it. This
    measures the ts(W) :depends-on bound CONDITIONAL ON #238's still-open
    close-side residual: the entity preload has already discarded the modules
    whose own close inverted, so what is left is only the edges that survived
    that discard. It is the figure the shipped code produces today.

  WIDE -- file_entities rebuilt position-correctly, from :path facts whose
    both ends are inverted to positions. This measures the ts(W) :depends-on
    bound IN ISOLATION, with the entity preload's own close-side defect
    removed.

The comparison between the two IS the finding. Neither alone is the number.
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Sequence, Tuple

# minigraf's i64::MAX "still open" :db/valid-to sentinel. Mirrors
# mcp_server._VALID_TIME_FOREVER_MS (mcp_server.py:7450) exactly; duplicated
# rather than imported so the analysis primitives stay importable without
# opening a graph. If the two ever diverge, every open edge is misread as
# closed at a nonsense position, so keep them identical.
VALID_TIME_FOREVER_MS = (1 << 63) - 1

CommitMeta = Tuple[str, str, str, str]  # (hash, ts_iso, author_email, subject)


def build_ts_positions(commit_metadata: Sequence[CommitMeta]) -> Dict[str, List[int]]:
    """Map each author-date timestamp to every linearization position holding it.

    A LIST, not a single position, and deliberately so: _git_commits formats
    "%Y-%m-%dT%H:%M:%SZ" (second granularity), so distinct commits routinely
    share an instant. Collapsing that to one position would silently pick a
    winner and produce a confidently wrong exposure number.
    """
    ts_positions: Dict[str, List[int]] = {}
    for pos, (_hash, ts_iso, _author, _subject) in enumerate(commit_metadata):
        ts_positions.setdefault(ts_iso, []).append(pos)
    return ts_positions


def resume_envelopes(commit_metadata: Sequence[CommitMeta]) -> List[str]:
    """T_hi(W) = max(ts[0..W]) for every W, the bound _preload_known_entities
    takes after #238.

    Timestamps are fixed-width UTC, so lexicographic max is chronological max
    -- the same property mcp_server._resume_envelope relies on.
    """
    envelopes: List[str] = []
    running_max = ""
    for _hash, ts_iso, _author, _subject in commit_metadata:
        running_max = max(running_max, ts_iso)
        envelopes.append(running_max)
    return envelopes


def affected_positions(commit_metadata: Sequence[CommitMeta]) -> List[int]:
    """Positions where the ts(W) bound is structurally capable of misclassifying.

    This is a position-level precondition computed from commit_metadata alone,
    independent of any fact. It is what keeps the sweep off every position in
    the history.

    W is exposed iff either structural condition holds. The UNION is what this
    returns, and the union is what matters -- but do not read either condition
    as belonging to one misclassification direction. Each condition enables one
    of EACH direction, because the bound is half-open containment
    [vf, vt) ∋ ts(W) (mcp_server._valid_time_window_clauses) and a fact's
    CLOSE is date-bounded on exactly the same terms as its introduction:

      condition A -- min(ts[W+1..]) <= ts(W): some commit ABOVE W carries a
      date at or below W's own.
        * wrong INCLUSION via introduction: a fact introduced at that commit
          has vf <= ts(W), so it passes the bound, but its introducing
          position is above W -- the walk sees a future edge.
        * wrong EXCLUSION via close: a fact CLOSED at that commit has
          vt <= ts(W), so half-open containment rejects it, but its closing
          position is above W -- the edge is still live at W and the walk
          cannot see it.

      condition B -- T_hi(W) > ts(W): some commit at position <= W carries a
      LATER date.
        * wrong EXCLUSION via introduction: a fact introduced there has
          vf > ts(W), outside the bound, though it is live at W.
        * wrong INCLUSION via close: a fact CLOSED there has vt > ts(W), so
          the bound still reads it as open, though its close position is at
          or below W and the edge is already dead.

    An earlier version of this docstring labelled condition A "wrong
    inclusion" and condition B "wrong exclusion" outright, which is wrong and
    was empirically decisive in the wrong direction: on this repository
    positions 118-123 are flagged by condition A alone, and every one of them
    misclassifies by wrong EXCLUSION -- via close, the arm the old labelling
    did not name.

    Condition A uses <=, not <, because the bound's vf test is `<=` -- a fact
    starting exactly at the instant is live.
    """
    timestamps = [ts for _h, ts, _a, _s in commit_metadata]
    n = len(timestamps)
    if n == 0:
        return []

    envelopes = resume_envelopes(commit_metadata)

    # suffix_min[i] = min(timestamps[i+1 .. n-1]), or None past the end.
    suffix_min: List[Optional[str]] = [None] * n
    running_min: Optional[str] = None
    for i in range(n - 1, -1, -1):
        suffix_min[i] = running_min
        running_min = timestamps[i] if running_min is None else min(running_min, timestamps[i])

    affected: List[int] = []
    for w in range(n):
        # Named for the structural conditions, not for directions: see the
        # docstring -- each one enables a wrong inclusion AND a wrong exclusion.
        condition_b_later_dated_at_or_below_w = envelopes[w] > timestamps[w]
        condition_a_earlier_dated_above_w = (
            suffix_min[w] is not None and suffix_min[w] <= timestamps[w]
        )
        if condition_b_later_dated_at_or_below_w or condition_a_earlier_dated_above_w:
            affected.append(w)
    return affected


def invert_ms_to_positions(ms: int, ts_positions: Dict[str, List[int]]) -> List[int]:
    """Map an epoch-millisecond :db/valid-from or :db/valid-to back to the
    linearization positions whose commit carries that instant.

    Returns [] when no commit matches. The caller MUST treat that as a
    diagnostic, not as an empty result to skip: an unmappable fact means the
    inversion assumption is broken, which invalidates the measurement rather
    than shrinking it.
    """
    ts_iso = (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return list(ts_positions.get(ts_iso, []))


def edge_live_at(
    vf_positions: List[int],
    vt_positions: Optional[List[int]],
    w: int,
) -> bool:
    """Is a fact introduced at vf_positions and closed at vt_positions live at
    position w?

    vt_positions is None for a fact still open (the forever sentinel).

    Ambiguity resolution is deliberately asymmetric, and always in the
    direction that cannot UNDERSTATE exposure: an ambiguous introduction takes
    the earliest colliding position, an ambiguous close the latest. Understating
    is the dangerous direction here -- it would argue for closing #245 as
    negligible on a number that was rounded in our own favour.

    An empty vf_positions (unmappable introduction) is never live; the driver
    counts these separately.
    """
    if not vf_positions:
        return False
    introduced_at = min(vf_positions)
    if introduced_at > w:
        return False
    if vt_positions:
        return w < max(vt_positions)
    return True


def gitlink_event_count(repo_path: str) -> int:
    """Number of raw diff entries across all history touching a gitlink
    (mode 160000).

    :pinned-commit facts are written only by gitlink handling, so a zero here
    means this history produces none and its #245 exposure is structurally
    unmeasurable -- not zero-risk, unmeasurable. That distinction goes in the
    report verbatim.
    """
    import subprocess

    result = subprocess.run(
        ["git", "log", "--all", "--raw", "--no-abbrev", "--format=%H"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return sum(
        1 for line in result.stdout.splitlines()
        if line.startswith(":") and "160000" in line
    )


def position_exact_live_edges(
    edges: Sequence[Dict],
    ts_positions: Dict[str, List[int]],
    file_entities: Dict[str, List[str]],
    w: int,
) -> set:
    """The (src_ident, dep_ident) edges genuinely live at position w.

    NOT ITSELF A FIX -- this function runs offline with the entire history in
    hand and no watermark-only constraint, so it is a measurement device, not
    a preload. But the INVERSION it performs (invert_ms_to_positions ->
    edge_live_at, both timestamp-to-position lookups against full-history
    commit_metadata) is, as of #245, the same technique the shipped fix uses:
    mcp_server._position_of_valid_time / _fact_is_live_at_position perform the
    identical inversion inside _load_ingestion_preload_state, with only the
    watermark position in hand. An earlier version of this docstring said "a
    resuming forward walk has no such thing -- that is the whole reason #245
    exists"; that was true against the pre-#246 shape this probe was conceived
    against, where build_linearization ran below the preload block, and it is
    false against current master, where PR #246 moved it above and made
    full-history commit_metadata available at preload time.

    What keeps this function itself out of the fix path is edge_live_at's
    ambiguity policy, not the inversion: edge_live_at resolves a collision
    toward NOT understating exposure (min position for an ambiguous
    introduction, max for an ambiguous close), the deliberate OPPOSITE of
    mcp_server._position_of_valid_time's policy, which resolves toward the
    recoverable direction (max for an introduction, min for a close) because a
    fix must not risk the unrecoverable inverted-interval direction (see
    _preload_known_entities' docstring). A measurement must not understate; a
    fix must not invert. The two policies must never be unified into one
    helper -- pinned on the fix side by
    test_collision_resolves_toward_exclusion_at_both_ends. sweep's verify_fix
    parameter (--verify-fix) is the actual fix-verification path; this
    function remains the oracle it drives against.

    Restricted to edges whose source module appears in file_entities, mirroring
    _preload_known_deps' own ident_to_file filter. WHICH file_entities the
    caller passes is what selects the NARROW or the WIDE measurement, and the
    two answer different questions:

    - _preload_known_entities' own output -> NARROW. That narrowing is
      position-correct on the INTRODUCTION side only; its close side is a
      T_hi(W) date bound carrying the same inversion defect (see the module
      docstring). So the narrow figure measures the ts(W) :depends-on bound
      conditional on #238's still-open close-side residual, not in isolation.
      An earlier version of this docstring claimed the narrowing was
      position-correct outright and that holding it fixed left the
      :depends-on bound as the single variable. It does not, and that claim
      understated the measured exposure by ~16x.

    - position_correct_file_entities' output -> WIDE. Both ends of each
      module's :path fact inverted to positions, so the entity preload's own
      defect is out of the picture and the :depends-on bound is measured
      alone.

    An unmappable CLOSE (a non-sentinel vt_ms whose instant matches no commit)
    falls through here to vt_positions=[], which edge_live_at's `if
    vt_positions:` treats identically to vt_positions=None -- "still open".
    That is deliberate, not an oversight: it is the same cannot-understate
    direction edge_live_at's own ambiguous-position handling takes. It does
    mean such an edge reads as live at every later w, inflating
    wrongly_excluded at each of them. The driver (sweep) counts these
    separately as unmappable_valid_to_facts so that inflation stays visible
    instead of being silently baked into the headline numbers.
    """
    import mcp_server

    known_src_idents = {
        mcp_server._code_ident("module", file_path) for file_path in file_entities
    }

    live = set()
    for edge in edges:
        if edge["src"] not in known_src_idents:
            continue
        vf_positions = invert_ms_to_positions(edge["vf_ms"], ts_positions)
        vt_positions = (
            None if edge["vt_ms"] >= VALID_TIME_FOREVER_MS
            else invert_ms_to_positions(edge["vt_ms"], ts_positions)
        )
        if edge_live_at(vf_positions, vt_positions, w):
            live.add((edge["src"], edge["dep"]))
    return live


def load_dep_edges(db) -> List[Dict]:
    """Every :depends-on fact in the graph, current and historical, with its
    validity window.

    Mirrors _preload_known_deps' own query shape exactly, including the clause
    ORDER: [?src :ident ?srci] must precede [?src :depends-on ?dep], because
    minigraf's :db/valid-from / :db/valid-to pseudo-attributes bind to
    whichever EAV clause on ?src most recently precedes them. Putting :ident
    between :depends-on and the pseudo-attributes would bind ?vf to the :ident
    fact's own valid-from instead -- wrong, and silently so.

    Deduplicated on (src, dep, vf, vt): an entity carrying more than one
    :ident fact across time (a rename, or a retract/reassert of the same
    value) makes the [?src :ident ?srci] join under :any-valid-time multiply
    each of that source's :depends-on rows once per :ident fact, which would
    otherwise inflate dep_edges_total and the unmappable-fact counts without
    changing which edges are actually live at any position.
    """
    import json

    import mcp_server

    raw = mcp_server._db_execute(
        db,
        "(query [:find ?srci ?dep ?vf ?vt "
        ":any-valid-time "
        ":where [?src :ident ?srci] "
        "[?src :depends-on ?dep] "
        "[?src :db/valid-from ?vf] "
        "[?src :db/valid-to ?vt]])",
    )
    seen = set()
    edges = []
    for src, dep, vf, vt in json.loads(raw).get("results", []):
        key = (src, dep, int(vf), int(vt))
        if key in seen:
            continue
        seen.add(key)
        edges.append({"src": src, "dep": dep, "vf_ms": int(vf), "vt_ms": int(vt)})
    return edges


def load_module_path_facts(db) -> List[Dict]:
    """Every :path fact on a module entity, current and historical, with its
    validity window.

    The raw material for the WIDE measurement. Same clause-order rule as
    load_dep_edges: [?e :path ?path] must be the EAV clause immediately
    preceding the :db/valid-from / :db/valid-to pseudo-attributes, because
    those bind to whichever EAV clause on ?e most recently precedes them.
    Putting :entity-type between them would bind ?vf to the :entity-type
    fact's window instead -- wrong, and silently so.

    Restricted to :type/module deliberately. :path is also carried by
    external-dependency entities, but _preload_known_deps keys its
    ident_to_file on _code_ident("module", path); admitting a submodule path
    there would synthesize a module ident for an entity that is not one.

    Deduplicated on (path, vf, vt) for the same reason load_dep_edges dedupes:
    an entity carrying more than one :entity-type fact across time would
    otherwise multiply each :path row without changing any liveness answer.
    A rename legitimately produces two DISTINCT (path, vf, vt) rows -- the old
    path closed, the new one opened -- and both are kept, which is what makes
    the set position-correct across renames.
    """
    import json

    import mcp_server

    raw = mcp_server._db_execute(
        db,
        "(query [:find ?path ?vf ?vt "
        ":any-valid-time "
        ":where [?e :entity-type :type/module] "
        "[?e :path ?path] "
        "[?e :db/valid-from ?vf] "
        "[?e :db/valid-to ?vt]])",
    )
    seen = set()
    facts = []
    for path, vf, vt in json.loads(raw).get("results", []):
        key = (path, int(vf), int(vt))
        if key in seen:
            continue
        seen.add(key)
        facts.append({"path": path, "vf_ms": int(vf), "vt_ms": int(vt)})
    return facts


def position_correct_file_entities(
    path_facts: Sequence[Dict],
    ts_positions: Dict[str, List[int]],
    w: int,
) -> Dict[str, List[str]]:
    """The module paths genuinely live at position w, shaped like the
    file_entities dict _preload_known_deps and position_exact_live_edges both
    consume.

    This is the WIDE side's replacement for _preload_known_entities' own
    file_entities. It is position-correct at BOTH ends -- introduction and
    close -- because it inverts both :db/valid-from and :db/valid-to through
    the same primitives the edge oracle uses (invert_ms_to_positions,
    edge_live_at), rather than trusting either against a date bound.

    That second end is the whole point. A module deleted at a position ABOVE w
    by a commit whose author date falls BELOW ts(w) -- the exact shape of
    df6b8be at position 124 on this repository -- reads as already closed to
    any date bound, and so vanishes from _preload_known_entities' output at w.
    Here it stays live, because max(close positions) > w.

    Values are empty lists: only the KEYS (the paths) are load-bearing
    downstream. _preload_known_deps reads only `for file_path in
    file_entities`, and position_exact_live_edges only
    _code_ident("module", file_path) over the same keys.

    Like everything else here, this is a measurement device and NOT a
    candidate fix -- it needs the whole history in hand, which a resuming
    forward walk does not have.
    """
    live: Dict[str, List[str]] = {}
    for fact in path_facts:
        vf_positions = invert_ms_to_positions(fact["vf_ms"], ts_positions)
        vt_positions = (
            None if fact["vt_ms"] >= VALID_TIME_FOREVER_MS
            else invert_ms_to_positions(fact["vt_ms"], ts_positions)
        )
        if edge_live_at(vf_positions, vt_positions, w):
            live.setdefault(fact["path"], [])
    return live


def count_unmappable_module_path_facts(
    path_facts: Sequence[Dict],
    ts_positions: Dict[str, List[int]],
) -> Tuple[int, int]:
    """How many module :path facts carry a vf_ms/vt_ms whose instant matches
    no commit -- the WIDE framing's own unmappable-fact diagnostic.

    WIDE (position_correct_file_entities) is built entirely from these
    :path facts, run through the same invert_ms_to_positions/edge_live_at
    machinery as the :depends-on edges. An unmappable vf silently drops a
    module from WIDE at every W (understating it); an unmappable non-sentinel
    vt silently reads it as never-closed (overstating it). Mirrors sweep's
    own unmappable_valid_from_facts/unmappable_valid_to_facts computation for
    :depends-on edges, applied to :path instead.

    Runs over ALL of path_facts, not a measured subset: unlike a
    :depends-on edge, a :path fact has no "did it ever enter consideration"
    filter to apply first -- it IS file_entities.

    Returns (unmappable_valid_from, unmappable_valid_to).
    """
    unmappable_vf = sum(
        1 for f in path_facts
        if not invert_ms_to_positions(f["vf_ms"], ts_positions)
    )
    unmappable_vt = sum(
        1 for f in path_facts
        if f["vt_ms"] < VALID_TIME_FOREVER_MS
        and not invert_ms_to_positions(f["vt_ms"], ts_positions)
    )
    return unmappable_vf, unmappable_vt


def sweep(
    db,
    repo_path: str,
    linearization: List[str],
    commit_metadata: Sequence[CommitMeta],
    branch: Optional[str] = None,
    verify_fix: bool = False,
) -> Dict:
    """Drive the REAL preload functions at each affected position and diff
    against the oracle, in BOTH the narrow and the wide entity framing.

    verify_fix (--verify-fix) selects which arguments the two real preloads
    are driven with:

      False (default) -- the date-only call: _preload_known_entities gets
        valid_at/hash_to_pos/watermark_pos, _preload_known_deps gets only
        valid_at_ms. This is #245's pre-fix measurement path.

      True -- ALSO passes ts_positions=ts_positions, watermark_pos=w,
        t_hi_ms=mcp_server._iso_to_epoch_ms(envelopes[w]) to both preloads,
        which is what turns position_mode on inside them
        (mcp_server._fact_is_live_at_position). This is the fixed preload
        path, exercised exactly as _load_ingestion_preload_state exercises it
        -- same functions, same kwargs, only the watermark in hand. The
        oracle side (position_exact_live_edges) is unchanged either way; it
        always has the whole history.

    Once verify_fix is True, NARROW and WIDE are expected to CONVERGE: narrow
    is _preload_known_entities' own output, wide rebuilds that same set
    position-correctly from :path facts, and after the close side became
    position-exact (#238's close-side residual, closed by this branch) the two
    computations select the identical entity set. Convergence is therefore the
    EXPECTED result under verify_fix, not independent corroboration of
    anything -- it is reported explicitly as framings_converged below so a
    reader does not mistake "both report zero" for two separate confirmations.
    Before the close-side fix, the two framings differed by ~16x on this
    repository; that gap is precisely what closing #238's close-side residual
    was supposed to erase.

    AGREEMENT UNDER verify_fix CHECKS PLUMBING, NOT ALGORITHM. The oracle
    (position_exact_live_edges) and the fix (mcp_server._fact_is_live_at_position
    via _preload_known_entities/_preload_known_deps) now perform the same
    timestamp-to-position inversion -- but the oracle runs offline with the
    entire history already in hand, while the fix runs inside
    _load_ingestion_preload_state with only the watermark position W, through
    the real minigraf queries, the real entity_type loop, and the real
    ident_to_file narrowing. A zero-diff run here confirms that plumbing
    reproduces the algorithm correctly; it is NOT independent evidence the
    algorithm itself is correct, because both sides share it. The unit tests
    (test_at_scale_dep_preload_probe.py, and #238/#245's own
    TestPreloadKnownEntitiesCloseSide / TestPreloadKnownDepsPositionBound /
    TestResumeWithInvertedAuthorDates suites in tests/test_mcp_server.py) are
    the non-circular evidence for the algorithm; this sweep is not.

    Calls the functions under test rather than a restatement of what we
    believe they do. On the #238 branch a reviewer and an implementer both
    simulated the counterfactual with a date bound instead of the real
    position-filtered one, which made an inadequate test look adequate and
    produced a false "bug not reachable" conclusion -- two fix rounds lost.

    NARROW vs WIDE. Every misclassification count below appears twice. The
    real _preload_known_deps is driven twice per position, differing only in
    the file_entities handed to it:

      narrow_* -- file_entities straight from _preload_known_entities. What
        the shipped code produces today, and the only figure the first
        version of this probe reported. It measures the ts(W) :depends-on
        bound CONDITIONAL ON #238's still-open close-side residual: entities
        whose own close inverted are already gone from file_entities, so
        their edges never reach either side of the diff.

      wide_* -- file_entities from position_correct_file_entities, both ends
        inverted to positions. It measures the ts(W) :depends-on bound in
        ISOLATION.

    Neither is "the" number; the gap between them is the finding, and it is
    what the module docstring explains. Reporting only the narrow one
    understated this repository's exposure by ~16x.

    provenance. repo_path alone was not enough to reproduce the first
    recorded artifact -- it named a scratch directory that no longer exists,
    with no branch and no head SHA. branch and head_commit are recorded here
    so a future run carries its own.

    Three report fields exist purely to keep this sweep from lying quietly
    about itself (#245 review round):

    - actual_dep_counts_by_position / preload_deps_empty_everywhere:
      _preload_known_deps swallows its own query failure (bare `except
      Exception: return file_deps, dep_valid_from`, mcp_server.py:7554-7555)
      and returns ({}, {}) on any runtime error. That failure mode and "this
      position genuinely has zero live deps" are indistinguishable from a
      *_wrongly_excluded_total alone -- both make every expected edge look
      wrongly excluded. Recording each framing's actual and expected counts
      per position, and flagging the all-positions-zero case explicitly, is
      what lets a reader tell them apart. The flag keys on the NARROW actual,
      the one the shipped code path produces.

    - unmappable_valid_from_facts / unmappable_valid_to_facts: counted only
      over the MEASURED population -- edges whose source module ident
      appears in file_entities at at least one affected position -- not over
      every row load_dep_edges returns. An edge that never enters the
      oracle's or the preload's consideration at any position can't corrupt
      either, so counting it would only make an unrelated, filtered-out
      corner of the graph look like it invalidated this measurement.
      unmappable_valid_to_facts exists because position_exact_live_edges
      resolves an unmappable CLOSE by treating the edge as still open (see
      its docstring); that is the correct not-understating direction, but it
      is silent unless counted here.

    - unmappable_module_path_valid_from / unmappable_module_path_valid_to:
      the same diagnostic applied to the WIDE framing's own raw material.
      WIDE is built from module :path facts via position_correct_file_entities,
      which runs :path's vf_ms/vt_ms through the identical
      invert_ms_to_positions/edge_live_at machinery as the :depends-on edges
      above -- an unmappable :path vf silently drops a module from WIDE at
      every W (understating it), and an unmappable :path vt silently reads it
      as never-closed (overstating it). Counted over ALL of path_facts, not a
      measured subset: unlike an edge, a :path fact has no "did it ever enter
      consideration" filter to apply first -- it IS file_entities. Both fold
      into measurement_invalid below, same as the edge counters.

    Every *_total_position_weighted is position-weighted: one edge
    misclassified at all N affected positions contributes N, not 1. The
    distinct-edge counts alongside them are the union across positions, for
    whichever unit the reader actually wants.
    """
    import mcp_server

    if len(commit_metadata) != len(linearization):
        raise ValueError(
            f"commit_metadata has {len(commit_metadata)} entries but "
            f"linearization has {len(linearization)}; a misaligned pair "
            "mis-filters the entire sweep"
        )
    for i, ((meta_hash, _ts, _a, _s), lin_hash) in enumerate(
        zip(commit_metadata, linearization)
    ):
        if meta_hash != lin_hash:
            raise ValueError(
                f"commit_metadata[{i}] is {meta_hash} but linearization[{i}] "
                f"is {lin_hash}; the two must be positionally aligned"
            )

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    ts_positions = build_ts_positions(commit_metadata)
    envelopes = resume_envelopes(commit_metadata)
    timestamps = [ts for _h, ts, _a, _s in commit_metadata]
    edges = load_dep_edges(db)
    path_facts = load_module_path_facts(db)
    collisions = {ts: pos for ts, pos in ts_positions.items() if len(pos) > 1}

    per_position = []
    actual_dep_counts_by_position = []
    measured_src_idents: set = set()
    for w in affected_positions(commit_metadata):
        valid_at_ms = mcp_server._iso_to_epoch_ms(timestamps[w])
        # Position args are threaded to _preload_known_deps ONLY under
        # verify_fix, and only as a matched trio -- mirroring
        # _load_ingestion_preload_state's own all-three-or-none position_mode
        # gate (watermark_pos AND ts_positions AND t_hi_ms, mcp_server.py:
        # _preload_known_deps' position_mode). Passing a subset would silently
        # fall back to the date-only path inside the preload, and this sweep
        # would think it was verifying the fix when it was not.
        #
        # _preload_known_entities is different: it already receives
        # hash_to_pos/watermark_pos unconditionally (that pair alone
        # position-filters the INTRODUCTION side, #238) and only gains
        # ts_positions/t_hi_ms -- which additionally position-filter the CLOSE
        # side, #245's residual -- under verify_fix.
        position_kwargs = (
            {"ts_positions": ts_positions, "watermark_pos": w,
             "t_hi_ms": mcp_server._iso_to_epoch_ms(envelopes[w])}
            if verify_fix else {}
        )

        (
            _entity_valid_from, _entity_descriptions, _entity_introduced_by,
            narrow_file_entities, _submodule_paths,
        ) = mcp_server._preload_known_entities(
            db, repo_path, valid_at=envelopes[w],
            hash_to_pos=hash_to_pos, watermark_pos=w,
            **{k: v for k, v in position_kwargs.items() if k != "watermark_pos"},
        )
        wide_file_entities = position_correct_file_entities(path_facts, ts_positions, w)

        # The measured population spans BOTH framings: an edge the narrow
        # entity set drops but the wide one keeps still enters the oracle, so
        # an unmappable timestamp on it still invalidates a reported number.
        measured_src_idents.update(
            mcp_server._code_ident("module", file_path)
            for file_path in (set(narrow_file_entities) | set(wide_file_entities))
        )

        # The SAME unmodified _preload_known_deps on both sides. Only the
        # file_entities differ -- that is the single variable this comparison
        # isolates.
        _narrow_file_deps, narrow_dep_valid_from = mcp_server._preload_known_deps(
            db, narrow_file_entities, valid_at_ms=valid_at_ms,
            **position_kwargs,
        )
        _wide_file_deps, wide_dep_valid_from = mcp_server._preload_known_deps(
            db, wide_file_entities, valid_at_ms=valid_at_ms,
            **position_kwargs,
        )
        narrow_actual = set(narrow_dep_valid_from.keys())
        wide_actual = set(wide_dep_valid_from.keys())

        narrow_expected = position_exact_live_edges(
            edges, ts_positions, narrow_file_entities, w
        )
        wide_expected = position_exact_live_edges(
            edges, ts_positions, wide_file_entities, w
        )

        actual_dep_counts_by_position.append({
            "position": w,
            "narrow_actual_count": len(narrow_actual),
            "narrow_expected_count": len(narrow_expected),
            "wide_actual_count": len(wide_actual),
            "wide_expected_count": len(wide_expected),
            "narrow_module_count": len(narrow_file_entities),
            "wide_module_count": len(wide_file_entities),
            # The counts alone are NOT enough: they can match while the sets
            # differ. Measured on a synthetic inverted-date repo, both sides
            # held 3 modules at the affected position while the narrow side
            # had dropped the deleted-later module and admitted a
            # not-yet-introduced one (_preload_known_entities pre-seeds
            # file_entities from `git ls-files` on the CURRENT worktree, so
            # its errors cancel in the count). These two are the direct
            # evidence of the entity preload's close-side residual.
            "modules_wide_only": len(set(wide_file_entities) - set(narrow_file_entities)),
            "modules_narrow_only": len(set(narrow_file_entities) - set(wide_file_entities)),
        })

        narrow_wrongly_included = sorted(narrow_actual - narrow_expected)
        narrow_wrongly_excluded = sorted(narrow_expected - narrow_actual)
        wide_wrongly_included = sorted(wide_actual - wide_expected)
        wide_wrongly_excluded = sorted(wide_expected - wide_actual)
        if (
            narrow_wrongly_included or narrow_wrongly_excluded
            or wide_wrongly_included or wide_wrongly_excluded
        ):
            per_position.append({
                "position": w,
                "commit": linearization[w],
                "date": timestamps[w],
                "narrow_wrongly_included": [list(e) for e in narrow_wrongly_included],
                "narrow_wrongly_excluded": [list(e) for e in narrow_wrongly_excluded],
                "wide_wrongly_included": [list(e) for e in wide_wrongly_included],
                "wide_wrongly_excluded": [list(e) for e in wide_wrongly_excluded],
            })

    measured_edges = [e for e in edges if e["src"] in measured_src_idents]
    unmappable_vf = sum(
        1 for e in measured_edges
        if not invert_ms_to_positions(e["vf_ms"], ts_positions)
    )
    unmappable_vt = sum(
        1 for e in measured_edges
        if e["vt_ms"] < VALID_TIME_FOREVER_MS
        and not invert_ms_to_positions(e["vt_ms"], ts_positions)
    )
    unmappable_module_path_vf, unmappable_module_path_vt = count_unmappable_module_path_facts(
        path_facts, ts_positions
    )

    # Keyed on the NARROW actual: that is the one produced by the shipped
    # code path, so it is the one whose all-zero signature would mean
    # _preload_known_deps' bare `except Exception` ate a real failure.
    preload_deps_empty_everywhere = bool(actual_dep_counts_by_position) and all(
        c["narrow_actual_count"] == 0 for c in actual_dep_counts_by_position
    )

    def _distinct(key: str) -> int:
        return len({tuple(e) for p in per_position for e in p[key]})

    narrow_wrongly_included_total = sum(len(p["narrow_wrongly_included"]) for p in per_position)
    narrow_wrongly_excluded_total = sum(len(p["narrow_wrongly_excluded"]) for p in per_position)
    wide_wrongly_included_total = sum(len(p["wide_wrongly_included"]) for p in per_position)
    wide_wrongly_excluded_total = sum(len(p["wide_wrongly_excluded"]) for p in per_position)
    narrow_wrongly_included_distinct = _distinct("narrow_wrongly_included")
    narrow_wrongly_excluded_distinct = _distinct("narrow_wrongly_excluded")
    wide_wrongly_included_distinct = _distinct("wide_wrongly_included")
    wide_wrongly_excluded_distinct = _distinct("wide_wrongly_excluded")

    # Named explicitly rather than left for a reader to infer from two equal
    # numbers (task-7 brief). Once the entity preload's close side is
    # position-correct, narrow (_preload_known_entities' own output) and wide
    # (position_correct_file_entities, rebuilt position-correctly at both
    # ends) select the SAME entity set by construction -- so under verify_fix
    # this is the EXPECTED result, not independent corroboration. It is only
    # a meaningful non-trivial signal when verify_fix is True; under the
    # date-only default path the two are expected to keep differing (that gap
    # is the original #245 finding), so a False here in default mode is not
    # itself informative.
    framings_converged = (
        narrow_wrongly_included_total == wide_wrongly_included_total
        and narrow_wrongly_excluded_total == wide_wrongly_excluded_total
        and narrow_wrongly_included_distinct == wide_wrongly_included_distinct
        and narrow_wrongly_excluded_distinct == wide_wrongly_excluded_distinct
    )

    return {
        "repo_path": repo_path,
        "branch": branch,
        "verify_fix": verify_fix,
        # See framings_converged's own note: meaningful as "the fix's close
        # side collapses the narrow/wide gap to zero" only when verify_fix is
        # True. Always computed and always reported, so a reader comparing
        # this artifact against the pre-fix baseline can see the collapse
        # directly rather than diffing eight numbers by hand.
        "framings_converged": framings_converged,
        # The ingested head, taken from the linearization rather than from a
        # fresh `git rev-parse`: it is the commit this sweep's graph actually
        # ends at, which a later rev-parse of the same branch need not be.
        "head_commit": linearization[-1] if linearization else None,
        "commits": len(linearization),
        "dep_edges_total": len(edges),
        "measured_dep_edges_total": len(measured_edges),
        "module_path_facts_total": len(path_facts),
        "affected_positions": affected_positions(commit_metadata),
        "misclassifying_positions": per_position,
        "actual_dep_counts_by_position": actual_dep_counts_by_position,
        "preload_deps_empty_everywhere": preload_deps_empty_everywhere,

        # NARROW -- file_entities as _preload_known_entities returns it. The
        # shipped behaviour, and the ts(W) :depends-on bound measured
        # CONDITIONAL ON #238's still-open close-side residual.
        "narrow_wrongly_included_total_position_weighted": narrow_wrongly_included_total,
        "narrow_wrongly_excluded_total_position_weighted": narrow_wrongly_excluded_total,
        "narrow_wrongly_included_distinct_edges": narrow_wrongly_included_distinct,
        "narrow_wrongly_excluded_distinct_edges": narrow_wrongly_excluded_distinct,

        # WIDE -- file_entities rebuilt position-correctly at BOTH ends. The
        # ts(W) :depends-on bound measured in ISOLATION.
        "wide_wrongly_included_total_position_weighted": wide_wrongly_included_total,
        "wide_wrongly_excluded_total_position_weighted": wide_wrongly_excluded_total,
        "wide_wrongly_included_distinct_edges": wide_wrongly_included_distinct,
        "wide_wrongly_excluded_distinct_edges": wide_wrongly_excluded_distinct,

        "timestamp_collisions": len(collisions),
        "unmappable_valid_from_facts": unmappable_vf,
        "unmappable_valid_to_facts": unmappable_vt,
        "unmappable_module_path_valid_from": unmappable_module_path_vf,
        "unmappable_module_path_valid_to": unmappable_module_path_vt,
        "gitlink_events": gitlink_event_count(repo_path),
    }


async def _ingest_into(repo_path: str, branch: Optional[str], graph_path) -> Tuple[str, str]:
    """Ingest repo_path into a fresh scratch graph, using the plain in-process
    path.

    Deliberately NOT run_ingestion_benchmark: its in-flight poller starved the
    ingestion it measured (#242, fixed on this same branch). This probe needs
    a completed ingestion, not a measured one, so it takes the simplest path
    and no poller at all.

    Returns (resolved_branch, status), where status is
    mcp_server._ingest_progress["status"] read immediately after
    _run_ingestion returns. That status is the ONLY signal available:
    _run_ingestion wraps its whole body in `except Exception`, sets status
    "error", and returns normally -- it never raises. (No explicit lease
    cleanup on that path either, as of #255: every ingestion lease is
    `with`-scoped, so there is nothing left open by the time the exception
    handler runs.) A partial run that stopped short of the frontier sets
    "stopped" the same way, also without raising.
    Either way the caller gets a graph that opened successfully and a status
    that says not to trust it; surfacing status is what makes that
    distinguishable from a genuine "complete". Mirrors the sibling pattern in
    evals/at_scale/run_ingestion_benchmark.py:151-152.
    """
    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph_path))
    mcp_server._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }
    resolved_branch = branch or mcp_server._default_git_branch(repo_path)
    await mcp_server._run_ingestion(repo_path, resolved_branch)
    return resolved_branch, mcp_server._ingest_progress["status"]


def main() -> int:
    import argparse
    import asyncio
    import json
    import sys
    import tempfile
    from pathlib import Path

    repo_root = Path(__file__).parent.parent.parent.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    parser = argparse.ArgumentParser(
        description="Measure #245's :depends-on preload exposure against real history."
    )
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    # --output is accepted as an alias of --json-out (same dest) so either
    # spelling works from the command line.
    parser.add_argument("--json-out", "--output", dest="json_out", default=None)
    parser.add_argument(
        "--verify-fix", action="store_true",
        help=(
            "Drive the two real preloads with position arguments "
            "(ts_positions, watermark_pos, t_hi_ms) instead of the date-only "
            "call, i.e. exercise the FIXED preload path (#238/#245) rather "
            "than measure the pre-fix residual. Also tightens the exit gate: "
            "any nonzero wrongly_included/wrongly_excluded in either framing, "
            "or any timestamp collision, fails the run in addition to the "
            "unmappable-fact checks that always apply. "
            "NOT YET RUN TO COMPLETION at full-history scale: an attempt on "
            "this repo's ~657 commits reached 247 in 9.8 hours before being "
            "killed, stuck in _reverse_apply's per-ident :introduced-by "
            "query (#239, pre-existing, unrelated to the preload path this "
            "flag verifies). See the module docstring."
        ),
    )
    args = parser.parse_args()

    import frontier_registry
    import mcp_server

    with tempfile.TemporaryDirectory(prefix="minigraf-245-probe-") as tmpdir:
        graph_path = Path(tmpdir) / "probe.graph"
        branch, ingest_status = asyncio.run(
            _ingest_into(args.repo_path, args.branch, graph_path)
        )

        # _run_ingestion never raises on failure (see _ingest_into's
        # docstring) -- ingest_status is the only signal that the graph
        # underneath this sweep is actually complete. Sweeping a "stopped" or
        # "error" graph would silently report on a partial ingestion as
        # though it were the full history; refuse instead.
        if ingest_status != "complete":
            error = mcp_server._ingest_progress.get("error")
            print(
                f"Ingestion did not complete (status={ingest_status!r}"
                + (f", error={error!r}" if error else "")
                + "). Refusing to sweep a partial or failed graph."
            )
            return 1

        linearization = frontier_registry.build_linearization(args.repo_path, branch)
        commit_metadata = mcp_server._git_commits(args.repo_path, None, branch)
        # _ingest_into's own open_db(str(graph_path)) call already bound the
        # lease manager's path, and nothing since has reset it -- db_lease()
        # below resolves to it without needing another explicit bind (#255).
        with mcp_server.db_lease() as db:
            report = sweep(
                db, args.repo_path, linearization, commit_metadata, branch=branch,
                verify_fix=args.verify_fix,
            )
        report["ingest_status"] = ingest_status

    print(json.dumps(report, indent=2))
    print()
    print(
        f"mode:                                     "
        f"{'--verify-fix (fixed, position-filtered preloads)' if report['verify_fix'] else 'date-only (pre-fix / #245 residual measurement)'}"
    )
    print(f"repo:                                     {report['repo_path']} @ {report['branch']}")
    print(f"ingested head:                            {report['head_commit']}")
    print(f"commits:                                  {report['commits']}")
    print(f":depends-on facts (raw, deduped):         {report['dep_edges_total']}")
    print(f":depends-on facts (measured population):  {report['measured_dep_edges_total']}")
    print(f"module :path facts (raw, deduped):        {report['module_path_facts_total']}")
    print(f"structurally affected W:                  {len(report['affected_positions'])}")
    print(f"W actually misclassifying:                {len(report['misclassifying_positions'])}")
    print()
    print("                                          NARROW      WIDE")
    print("  (narrow = file_entities as _preload_known_entities returns it: the")
    print("   ts(W) :depends-on bound CONDITIONAL on #238's open close-side residual.")
    print("   wide = file_entities rebuilt position-correctly at both ends: the")
    print("   ts(W) :depends-on bound in ISOLATION.")
    print("   EXCLUDED gap = #238's own close-side leak: its date bound drops")
    print("   modules deleted later but dated earlier, inflating narrow's excluded")
    print("   count relative to wide's.")
    print("   INCLUDED gap is a DIFFERENT effect, not #238's leak: _preload_known_")
    print("   entities pre-seeds file_entities from the current worktree's `git")
    print("   ls-files` and never removes from it, so narrow admits every current-")
    print("   worktree module at every W, including ones not yet introduced. A")
    print("   NEGATIVE included gap (wide < narrow) is expected from that pre-seed")
    print("   and is NOT evidence #245's inclusion exposure is small.)")
    print(
        f"  wrongly INCLUDED, position-weighted:    "
        f"{report['narrow_wrongly_included_total_position_weighted']:<11}"
        f"{report['wide_wrongly_included_total_position_weighted']}"
    )
    print(
        f"  wrongly INCLUDED, distinct edges:       "
        f"{report['narrow_wrongly_included_distinct_edges']:<11}"
        f"{report['wide_wrongly_included_distinct_edges']}"
    )
    print(
        f"  wrongly EXCLUDED, position-weighted:    "
        f"{report['narrow_wrongly_excluded_total_position_weighted']:<11}"
        f"{report['wide_wrongly_excluded_total_position_weighted']}"
    )
    print(
        f"  wrongly EXCLUDED, distinct edges:       "
        f"{report['narrow_wrongly_excluded_distinct_edges']:<11}"
        f"{report['wide_wrongly_excluded_distinct_edges']}"
    )
    print()
    print(f"framings_converged:                       {report['framings_converged']}")
    if report["verify_fix"]:
        print(
            "  (--verify-fix mode: narrow and wide are EXPECTED to converge --\n"
            "   narrow is _preload_known_entities' own output, wide rebuilds that\n"
            "   same set position-correctly, and once the close side is\n"
            "   position-exact the two select the identical entity set. This is\n"
            "   the expected result, not independent corroboration -- see sweep's\n"
            "   docstring. Agreement here checks PLUMBING, not algorithm: the\n"
            "   oracle (position_exact_live_edges) runs offline with the whole\n"
            "   history; the fix runs inside _load_ingestion_preload_state with\n"
            "   only the watermark, through the real queries, the real\n"
            "   entity_type loop, and the real ident_to_file narrowing. The unit\n"
            "   tests are the non-circular evidence for the algorithm itself.)"
        )
    else:
        print(
            "  (date-only mode: narrow and wide are NOT expected to converge --\n"
            "   this is the pre-fix #245 residual measurement. Pass --verify-fix\n"
            "   to drive the fixed, position-filtered preloads instead.)"
        )
    print()
    print(f"preload returned zero deps at every W:    {report['preload_deps_empty_everywhere']}")
    print(f"timestamp collisions:                     {report['timestamp_collisions']}")
    print(f"unmappable :valid-from facts (measured):  {report['unmappable_valid_from_facts']}")
    print(f"unmappable :valid-to facts (measured):    {report['unmappable_valid_to_facts']}")
    print(f"unmappable module :path valid-from facts: {report['unmappable_module_path_valid_from']}")
    print(f"unmappable module :path valid-to facts:   {report['unmappable_module_path_valid_to']}")
    print(f"gitlink events:                           {report['gitlink_events']}")

    # Emphasis is deliberately inverted from a naive reading: nonzero
    # unmappable facts mean the position-inversion assumption this whole
    # sweep rests on is broken for at least one fact -- either in the
    # :depends-on measured population, or in the module :path facts that are
    # WIDE's own raw material -- which invalidates the wrongly_included/
    # wrongly_excluded numbers above. Zero gitlink events only NARROWS what
    # was measured; it does not call the rest of the report into question.
    unmappable_facts_present = (
        report["unmappable_valid_from_facts"] > 0
        or report["unmappable_valid_to_facts"] > 0
        or report["unmappable_module_path_valid_from"] > 0
        or report["unmappable_module_path_valid_to"] > 0
    )
    # --verify-fix's acceptance criteria fold into this SAME gate rather than
    # adding a second one (task-7 brief): a run either produces a trustworthy,
    # passing measurement, or it doesn't, and there is one exit code. Nonzero
    # timestamp_collisions belongs here and not only in the unmappable check
    # above because the fix and the oracle resolve a collision in OPPOSITE
    # directions (mcp_server._position_of_valid_time vs this module's
    # edge_live_at) -- a nonzero count makes the fix/oracle comparison
    # invalid, exactly like an unmappable fact does, even though neither side
    # left a fact literally unplaceable.
    fix_acceptance_failed = report["verify_fix"] and (
        report["narrow_wrongly_included_total_position_weighted"] > 0
        or report["narrow_wrongly_excluded_total_position_weighted"] > 0
        or report["wide_wrongly_included_total_position_weighted"] > 0
        or report["wide_wrongly_excluded_total_position_weighted"] > 0
        or report["timestamp_collisions"] > 0
    )
    measurement_invalid = unmappable_facts_present or fix_acceptance_failed
    if unmappable_facts_present:
        print()
        print(
            "INVALID MEASUREMENT: nonzero unmappable :valid-from/:valid-to facts,\n"
            "on either the :depends-on edges or the module :path facts WIDE is\n"
            "built from, mean the timestamp-to-position inversion this sweep\n"
            "depends on is broken for at least one fact. The wrongly_included/\n"
            "wrongly_excluded numbers above are not trustworthy until this is zero."
        )
    if fix_acceptance_failed:
        print()
        print(
            "VERIFY-FIX ACCEPTANCE FAILED: --verify-fix requires zero wrongly_\n"
            "included and zero wrongly_excluded in BOTH framings, and zero\n"
            "timestamp_collisions (the fix and the oracle resolve a collision in\n"
            "OPPOSITE directions, so a nonzero count invalidates the comparison\n"
            "rather than merely widening it). At least one of those is nonzero\n"
            "above -- this is a real finding about the fix, not noise; do not\n"
            "adjust this gate to make a failing run pass."
        )
    if report["preload_deps_empty_everywhere"]:
        print()
        print(
            "NOTE: _preload_known_deps returned zero live deps at every affected\n"
            "position. That may be a genuinely dep-free history, or it may be\n"
            "_preload_known_deps' own bare `except Exception` (mcp_server.py:\n"
            "7554-7555) swallowing a real query failure -- the two are\n"
            "indistinguishable from this report alone. Check dep_edges_total\n"
            "above: nonzero there with zero here is the signature of the latter."
        )

    if report["gitlink_events"] == 0:
        print()
        print(
            "NOTE: zero gitlink events -- this history produces no :pinned-commit\n"
            "facts, so #245's :pinned-commit half is UNMEASURABLE here. That is\n"
            "not the same as zero risk; its field exposure remains unknown."
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.json_out}")
    return 1 if measurement_invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
