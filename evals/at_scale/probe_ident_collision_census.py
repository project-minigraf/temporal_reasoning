# evals/at_scale/probe_ident_collision_census.py
"""#263 audit: how many _code_ident values on real history are reachable from
more than one distinct (entity_type, file_path, name) input?

_canonical_ident (mcp_server.py:4090) maps every character outside [a-z0-9-]
to a hyphen and then collapses runs of hyphens. _code_ident builds its input as
f"{file_path}::{name}", so the separator and a leading underscore in the name
both become hyphens and the collapse merges them:

    tests/test_mcp_server.py::_commit -> tests-test-mcp-server-py-commit
    tests/test_mcp_server.py::commit  -> tests-test-mcp-server-py-commit

WHY THIS AUDIT IS SOURCE-DERIVED AND NOT GRAPH-DERIVED. When two entities
collide, the second one parsed takes _build_code_triples' `ident in
entity_valid_from` branch, which appends a :modified-in triple and NOTHING
else -- no :entity-type, no :ident, no :description, no :file. The graph
therefore holds no record of the losing entity's (file_path, name) pair at
all, and widening #257's census key from :description to (:file, :description)
does not recover it. The three collisions #257 found were visible only because
those entities were closed and reopened, which re-runs the introduction branch.
Any graph-side count is a bound; only the inputs give an exact one.

READ-ONLY BY CONSTRUCTION. This module opens no MiniGrafDb handle, writes no
facts, and never mutates mcp_server. The candidate rules below are standalone
pure functions, deliberately NOT monkeypatches of _canonical_ident -- an audit
must not mutate the module it measures.

WHAT THE CONTENT-FETCH `continue` DOES AND DOES NOT COST. _extract_commit
silently drops an A/M file whose blob fetch raises (mcp_server.py:8641-8644).
This is NOT an undercount of production's idents: production ingestion runs
that same function and takes that same `continue`, so a file skipped there
never produces an ident in production either. The census stays exact with
respect to the idents production actually generates. The residual risk is
narrower -- if a fetch fails TRANSIENTLY, two runs over the same head_commit
can collect different input sets, so a recorded number is reproducible only to
the extent that git object reads are. `extraction_failures` does not cover
this: that counter only sees _extract_commit raising outright, and this branch
swallows the error inside it.

WHICH BRANCH GETS MEASURED. collect_inputs resolves an unspecified branch
through mcp_server._default_git_branch, i.e. the repository's mainline, not
whatever branch happens to be checked out. That is deliberate: the audit must
count the idents mainline history produces, not ones introduced by the
in-progress work that is adding this probe. `branch` and `head_commit` are
both recorded and both printed, so a run is never ambiguous about what it read.

See docs/superpowers/specs/2026-08-11-ident-collision-audit-design.md.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import multiprocessing
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import combinations
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mcp_server  # noqa: E402


# The five types _code_ident actually produces. external-dependency is NOT a
# sixth: it shares the :module/ namespace (mcp_server.py:7396), which is why
# the module bucket below is pooled across producers rather than split.
ENTITY_TYPES = ("module", "function", "class", "variable", "field")

# Which call site produced an input. Only ever used to label a collision as
# cross-producer -- it is deliberately NOT part of the ident, because the
# whole point is that these three feed one namespace.
#   "code"    -- _code_ident over a parsed file (mcp_server.py:7044-7122)
#   "gitlink" -- _code_ident("module", path) for a submodule (mcp_server.py:9556)
#   "import"  -- _canonical_ident("module", specifier), the unresolved-import
#                fallback (mcp_server.py:4250, 4285)
PRODUCERS = ("code", "gitlink", "import")


class EntityInput(NamedTuple):
    """One (entity_type, file_path, name) triple that ever reached _code_ident.

    For producer "import", file_path holds the raw import specifier and name is
    None -- _canonical_ident("module", spec) is exactly _code_ident("module",
    spec, None), so no separate ident path is needed.
    """

    entity_type: str
    producer: str
    file_path: str
    name: Optional[str]


def raw_value(inp: EntityInput) -> str:
    """The string _code_ident slugs, reproducing mcp_server.py:4298-4301."""
    if inp.name:
        return f"{inp.file_path}::{inp.name}"
    return inp.file_path


def current_ident(inp: EntityInput) -> str:
    """The ident the PRE-#263 rule built for this input.

    FROZEN, and deliberately no longer `mcp_server._code_ident`. This whole
    artifact is the audit that CHOSE the current rule: its PREDICTIONS block was
    fixed before any data existed, and P3/P4 are statements about R5 and R2 as
    measured against the old baseline. Re-baselining this onto whatever
    production does today would silently re-evaluate those predictions against a
    DIFFERENT experiment while still printing them as "held" -- destroying the
    one property that makes a pre-registered prediction worth anything.

    So this probe reproduces the historical measurement forever, and does NOT
    track production. The forward guard against a regression in the shipped rule
    is TestIdentCollisionRegression263 in tests/test_mcp_server.py; a census of
    NEW history under the shipped rule needs its own probe (filed as a
    follow-up), not a mutation of this one.
    """
    return f":{inp.entity_type}/{_slug_current(raw_value(inp))}"


def group_by_ident(
    inputs: Iterable[EntityInput],
    ident_fn: Callable[[EntityInput], str],
) -> Dict[str, List[EntityInput]]:
    """Group DISTINCT inputs by the ident ident_fn assigns them.

    Distinct is load-bearing: an unchanged name arrives once per commit that
    touched its file, and counting occurrences would report every entity in
    the repository as colliding with itself.
    """
    groups: Dict[str, Dict[EntityInput, None]] = {}
    for inp in inputs:
        groups.setdefault(ident_fn(inp), {})[inp] = None
    return {ident: list(members) for ident, members in groups.items()}


def offenders(
    groups: Dict[str, List[EntityInput]],
) -> Dict[str, List[EntityInput]]:
    """The groups reachable from more than one distinct input."""
    return {
        ident: members for ident, members in groups.items() if len(members) > 1
    }


SHAPES = (
    "leading-underscore",
    "case-only",
    "separator-vs-path",
    "cross-producer",
    "other",
)


def _strip_private(name: Optional[str]) -> str:
    """Drop leading underscores from the LAST dot-segment of a name.

    Fields reach _code_ident qualified as "Cls.field" (mcp_server.py:7098), so
    the private marker sits mid-string. A bare name.lstrip("_") would miss
    "Cls._x" against "Cls.x" entirely.
    """
    if not name:
        return ""
    head, dot, last = name.rpartition(".")
    stripped = last.lstrip("_")
    return f"{head}{dot}{stripped}" if dot else stripped


def _name_is_trailing_path_segment(named: EntityInput, other: EntityInput) -> bool:
    """True if `named`'s name is a trailing WHOLE path segment of `other`'s
    file_path, extension stripped.

    This is the design spec's definition of separator-vs-path verbatim (spec
    line 189): "the `name` of one input appears as a trailing path segment of
    another's `file_path`" -- the case _code_ident's own docstring anticipates,
    where the ident cannot say whether the last component came from the path or
    from the name.

    WHOLE-SEGMENT, not raw string suffix. `mcp_server.py` does NOT end with the
    segment `server`: the substring is there, but not as its own path
    component, and calling that a path/name boundary would relabel ordinary
    underscore collisions. The boundary logic is mcp_server._path_segments plus
    mcp_server._segments_end_with (mcp_server.py:4102-4117), reused rather than
    re-implemented for the third time -- reading the audited module is fine
    here, only MUTATING it is forbidden.

    Casefolded on both sides for the same reason the leading-underscore check
    is: the ident lowercases, so `src/auth/Login.py` against a name of `login`
    is a genuine instance of this family, and an exact-case comparison would
    drop it into "other", which is reserved for UNPREDICTED families.
    """
    if not named.name:
        return False
    segments = mcp_server._path_segments(other.file_path)
    if not segments:
        return False
    segments = segments[:-1] + [Path(segments[-1]).stem]
    return mcp_server._segments_end_with(
        [seg.casefold() for seg in segments],
        [seg.casefold() for seg in mcp_server._path_segments(named.name)],
    )


def _pair_shapes(a: EntityInput, b: EntityInput) -> Set[str]:
    """Every collision family this pair belongs to.

    The checks are INDEPENDENT, not an elif chain. A pair can belong to more
    than one family and the caller returns a set, so making them exclusive
    would silently drop the second label -- and, worse, would let the winner
    depend on check order rather than on the data.

    Each check also carries an inequality guard. Without one, "case-only"
    fires on two inputs whose raw values are byte-identical (a pair differing
    only in producer -- exactly the cross-producer collision this audit exists
    to find), asserting a case difference that is not there.
    """
    shapes: Set[str] = set()
    if a.producer != b.producer:
        shapes.add("cross-producer")
    a_raw, b_raw = raw_value(a), raw_value(b)
    if a_raw != b_raw and a_raw.casefold() == b_raw.casefold():
        shapes.add("case-only")
    # casefold on BOTH sides, so a private PascalCase helper beside a public
    # snake_case one (_Config/config, _Handler/handler -- ordinary Python) is
    # still recognised as the underscore family. An exact-case comparison here
    # drops those into "other", which is reserved for UNPREDICTED families and
    # so would hide a predicted one.
    #
    # The guard compares NAME casefold, not exact name, and requires that
    # comparison to be UNEQUAL -- i.e. that the pair is not already
    # explainable by case alone. Without it, a pair with no underscore at
    # all (Foo/foo) also satisfies the stripped-casefold equality below,
    # since stripping is a no-op when there is nothing to strip, and
    # leading-underscore would fire on top of a plain case-only pair. That
    # is the same false-positive shape as Finding 1, just on the other
    # check.
    a_name_cf = (a.name or "").casefold()
    b_name_cf = (b.name or "").casefold()
    if (
        a_name_cf != b_name_cf
        and a.file_path == b.file_path
        and _strip_private(a.name).casefold() == _strip_private(b.name).casefold()
    ):
        shapes.add("leading-underscore")
    # The spec's definition, narrowly: one input's NAME is a trailing whole
    # path segment of the other's file_path, so the ident cannot say where the
    # path ended and the name began -- the family _code_ident's docstring
    # anticipates. This deliberately does NOT fire merely because the two
    # file_paths differ: "the paths differ" describes most collisions on any
    # history and says nothing about a path/name boundary. See
    # _name_is_trailing_path_segment.
    if _name_is_trailing_path_segment(a, b) or _name_is_trailing_path_segment(b, a):
        shapes.add("separator-vs-path")
    return shapes


def classify_shapes(members: Sequence[EntityInput]) -> Set[str]:
    """Label an offender by every collision family present among its inputs.

    An offender may carry more than one label -- the per-pair checks are
    independent, not exclusive, so a cross-producer collision is usually also
    something else. "other" is emitted when nothing BUT "cross-producer"
    applied -- not when no label at all applied. "cross-producer" says which
    call sites the two inputs came from, never what made their values collide,
    so an offender carrying only that label is still unexplained and belongs in
    the bucket where an unpredicted collision family surfaces. Collapsing
    "other" into any named label would hide exactly the finding worth having.
    """
    shapes: Set[str] = set()
    for a, b in combinations(members, 2):
        shapes |= _pair_shapes(a, b)
    if not shapes - {"cross-producer"}:
        shapes.add("other")
    return shapes


def _slug_current(value: str) -> str:
    """The PRE-#263 slug: '_' mapped to '-', then hyphen runs collapsed.

    FROZEN. This was `_canonical_ident`'s slug verbatim when the audit ran;
    production has since moved to R3 (#263). It is kept as a hand copy rather
    than re-pointed at production for the reason in `current_ident`: this
    artifact must keep reproducing the experiment its predictions were
    registered against. The name is left as `_slug_current` so the recorded
    report and this file stay legible against each other.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _slug_keep_underscore(value: str) -> str:
    """R1: '_' joins the allowed charset, so a private marker survives."""
    slug = re.sub(r"[^a-z0-9_-]", "-", value.lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _slug_no_collapse(value: str) -> str:
    """R2: hyphen runs are preserved, so separator arity carries the marker
    ('py---commit' against 'py--commit')."""
    return re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")


def _slug_keep_underscore_no_collapse(value: str) -> str:
    """R3: R1 and R2 together."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower()).strip("-")


def _ident_from_slug(slug_fn: Callable[[str], str]) -> Callable[[EntityInput], str]:
    def ident(inp: EntityInput) -> str:
        return f":{inp.entity_type}/{slug_fn(raw_value(inp))}"

    return ident


def _ident_hash_suffix(inp: EntityInput) -> str:
    """R4: the current slug plus 8 hex of sha256 over the RAW value.

    Hashing the raw value, before lowercasing, is what separates a case-only
    collision as well -- the slug alone cannot.
    """
    value = raw_value(inp)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f":{inp.entity_type}/{_slug_current(value)}-{digest}"


def _ident_independent_parts(inp: EntityInput) -> str:
    """R5, the CONTROL. Slugs file_path and name separately and joins them
    with a fixed token.

    This is expected to FAIL. _slug_current strips leading hyphens, so a name
    of "_commit" reduces to "commit" exactly as "commit" does, and the pair
    still collides. R5 exists so score_all_rules has a known-negative: if it
    ever reports zero residual on a history with leading-underscore pairs, the
    scorer is wrong and every other row it produced is suspect.
    """
    path_slug = _slug_current(inp.file_path)
    if inp.name is None:
        return f":{inp.entity_type}/{path_slug}"
    return f":{inp.entity_type}/{path_slug}--{_slug_current(inp.name)}"


RULES: Dict[str, Tuple[str, Callable[[EntityInput], str]]] = {
    "R1": (
        "keep underscores: charset becomes [^a-z0-9_-]",
        _ident_from_slug(_slug_keep_underscore),
    ),
    "R2": (
        "no hyphen collapse: drop re.sub(r'-+', '-', slug)",
        _ident_from_slug(_slug_no_collapse),
    ),
    "R3": (
        "R1 + R2: keep underscores and drop the collapse",
        _ident_from_slug(_slug_keep_underscore_no_collapse),
    ),
    "R4": (
        "hash suffix: current slug + '-' + sha256(raw value)[:8]",
        _ident_hash_suffix,
    ),
    "R5": (
        "CONTROL, expected to still collide: slug file_path and name "
        "independently, join with '--'",
        _ident_independent_parts,
    ),
}


def score_rule(
    inputs: Sequence[EntityInput],
    ident_fn: Callable[[EntityInput], str],
    baseline_groups: Dict[str, List[EntityInput]],
) -> Dict[str, int]:
    """Residual collisions and rename cost for one candidate rule.

    renames counts BASELINE idents, not inputs: a baseline ident is renamed
    when any input in its group maps somewhere other than that same ident.
    Rename cost is not a footnote to residual -- every change to derivation
    renames entities in every existing graph, and that cost is what decides
    forward-fix against migration.
    """
    residual = len(offenders(group_by_ident(inputs, ident_fn)))
    renames = sum(
        1
        for ident, members in baseline_groups.items()
        if any(ident_fn(member) != ident for member in members)
    )
    return {"residual": residual, "renames": renames}


def renames_by_entity_type(
    ident_fn: Callable[[EntityInput], str],
    baseline_groups: Dict[str, List[EntityInput]],
) -> Dict[str, Dict[str, int]]:
    """Rename cost split by entity type, as {entity_type: {"renamed", "total"}}.

    score_rule's single `renames` integer cannot answer the question that
    decides between candidate rules: WHERE the rename cost lands. R2 drops
    only the hyphen collapse, and a module input has no '::' separator to
    produce an adjacent hyphen run, so it should spare module idents while
    renaming nearly every named one. A whole-corpus total hides that shape
    completely.

    Keyed on the entity_type of a group's first member, matching how
    build_report already partitions baseline groups per type.

    The rename criterion is score_rule's, verbatim -- a baseline ident counts
    as renamed when any input in its group maps somewhere other than that same
    ident -- so these per-type counts SUM to score_rule's total. A test pins
    that equality for every rule, because the two paths silently drifting is
    how a per-type breakdown stops describing the total it sits beside.

    Every declared entity type appears even at zero, for the same reason
    build_report emits them all: a missing key and a zero are different claims.
    An entity_type outside ENTITY_TYPES is still counted rather than dropped,
    so the sum invariant holds unconditionally.
    """
    per_type: Dict[str, Dict[str, int]] = {
        entity_type: {"renamed": 0, "total": 0} for entity_type in ENTITY_TYPES
    }
    for ident, members in baseline_groups.items():
        row = per_type.setdefault(
            members[0].entity_type, {"renamed": 0, "total": 0}
        )
        row["total"] += 1
        if any(ident_fn(member) != ident for member in members):
            row["renamed"] += 1
    return per_type


def score_all_rules(
    inputs: Sequence[EntityInput],
    baseline_groups: Dict[str, List[EntityInput]],
) -> Dict[str, Dict]:
    scored = {}
    for rule_id, (description, ident_fn) in RULES.items():
        row = score_rule(inputs, ident_fn, baseline_groups)
        row["description"] = description
        scored[rule_id] = row
    return scored


# ---------------------------------------------------------------------------
# Stage 1: collect the inputs, through the real extractor
# ---------------------------------------------------------------------------


def inputs_from_commit_extraction(
    file_results: Sequence[tuple],
    gitlink_changes: Sequence[tuple],
) -> List[EntityInput]:
    """Harvest every _code_ident input one commit's extraction implies.

    file_results entries are (status, file_path, extracted, precomputed,
    old_path) per _extract_commit's contract (mcp_server.py:8475-8484). A "D"
    entry carries extracted=None and precomputed=None and is skipped: the path
    names an entity being CLOSED, not one being introduced, and its inputs were
    already collected when it was introduced.

    The category-to-entity_type mapping and the field qualification mirror
    _precompute_file_triples (mcp_server.py:7044-7122). If that function and
    this one ever disagree, that one is right.
    """
    collected: List[EntityInput] = []

    for _status, file_path, extracted, precomputed, _old_path in file_results:
        if extracted is None:
            continue
        collected.append(EntityInput("module", "code", file_path, None))
        for fn_name in extracted.get("functions", []):
            collected.append(EntityInput("function", "code", file_path, fn_name))
        for cls_name in extracted.get("classes", []):
            collected.append(EntityInput("class", "code", file_path, cls_name))
        for gvar_name in extracted.get("globals", []):
            collected.append(EntityInput("variable", "code", file_path, gvar_name))
        for field_name, owning_class, _is_static in extracted.get("fields", []):
            collected.append(
                EntityInput("field", "code", file_path, f"{owning_class}.{field_name}")
            )
        # Only UNRESOLVED imports. A resolved one already points at the in-tree
        # module ident the module producer above contributes, so counting it
        # again would manufacture a cross-producer collision on every internal
        # import in the repository.
        for import_name, _dep_ident, is_resolved in (
            (precomputed or {}).get("resolved_imports", [])
        ):
            if not is_resolved:
                collected.append(EntityInput("module", "import", import_name, None))

    # add/bump/remove all reach one `for kind, sha, path` loop that takes
    # _code_ident("module", path) unconditionally (mcp_server.py:9555-9556),
    # so every kind contributes.
    for _kind, _sha, path in gitlink_changes:
        collected.append(EntityInput("module", "gitlink", path, None))

    return collected


def collect_inputs(
    repo_path: str,
    branch: Optional[str] = None,
    jobs: Optional[int] = None,
) -> Tuple[List[EntityInput], Dict[str, Any]]:
    """Walk the branch and collect every input that ever reached _code_ident.

    Drives mcp_server._extract_commit rather than re-implementing parsing.
    That function is documented read-only, stateless and DB-free
    (mcp_server.py:8455) and is exactly what _run_ingestion's worker pool
    runs, so this audit measures the code that actually produces idents in
    production rather than a reimplementation that could drift from it.

    Every commit's file_results are consumed, filtered only on `extracted is
    None` -- so A, M and R contribute, and so does any other status the raw
    parse reports (T/typechange), which carries an extraction like any other
    content change. That reaches every version of every file that ever existed
    on the branch: each distinct blob at each path is introduced by exactly one
    such entry, so no separate initial-tree pass is needed.

    That coverage claim rests on mcp_server, not on this function.
    _git_diff_tree_raw passes --root (mcp_server.py:4406), so the initial
    commit reports its whole tree as adds instead of an empty diff, and merge
    commits fall back to the --cc combined diff (_git_diff_tree_combined_raw,
    mcp_server.py:4479) plus the one-parent-side supplement above it. A path a
    merge resolved to a content no parent carried would otherwise be invisible.

    Inputs are deduplicated here. A name unchanged across 400 commits costs one
    entry, not 400.

    The resolved branch and the resolved ignore-pattern list are BOTH returned
    as diagnostics because both are environment-dependent and both change which
    idents get counted: _default_git_branch honours MINIGRAF_GIT_BRANCH, and
    _load_ignore_patterns merges MINIGRAF_INGEST_IGNORE with any .temporalignore
    in the working tree. Without them recorded, two runs on the same
    head_commit can legitimately disagree with nothing in the artifact
    explaining why.

    The pool uses an explicit "spawn" context for the same reason
    _run_ingestion does (mcp_server.py:10536-10546): workers are created lazily
    while other threads may already be alive in this process (this runs under
    pytest, and Task 5 puts a CLI on top of it), and forking with a thread
    holding a lock deadlocks the child forever. It also makes the start method
    independent of the platform default, which changed in 3.14.
    """
    resolved_branch = branch or mcp_server._default_git_branch(repo_path)
    ignore_patterns = mcp_server._load_ignore_patterns(repo_path)
    commits = mcp_server._git_commits(repo_path, None, resolved_branch, first_parent=False)
    # _git_commits walks --topo-order --reverse, so hashes[-1] is the branch
    # tip. Taken from the walked list, not a separate rev-parse, so the
    # reported head cannot disagree with what was actually measured.
    hashes = [row[0] for row in commits]

    seen: Dict[EntityInput, None] = {}
    failed: List[str] = []

    with ProcessPoolExecutor(
        max_workers=jobs, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        futures = {
            executor.submit(
                mcp_server._extract_commit, repo_path, commit_hash, ignore_patterns
            ): commit_hash
            for commit_hash in hashes
        }
        # as_completed drains in completion order, so a straggler at position 0
        # does not hold the loop. That alone bounds NOTHING, though: a Future
        # keeps its _result after result() returns, and this dict keeps a
        # strong reference to every future for the whole `with` block, so peak
        # memory would be identical to iterating futures.items(). The pop is
        # what actually drops the reference and lets a consumed result be
        # collected. Safe because as_completed snapshots its argument before
        # the first loop body runs.
        for future in as_completed(futures):
            commit_hash = futures.pop(future)
            try:
                file_results, gitlink_changes, _gitmodules, _renamed = future.result()
            except Exception:
                # Per-commit failure must not abort the audit -- it is a
                # measurement diagnostic, and the report's exit gate decides
                # whether there were too many for the run to mean anything.
                failed.append(commit_hash)
                continue
            for inp in inputs_from_commit_extraction(file_results, gitlink_changes):
                seen[inp] = None

    # Completion order is whatever the pool happened to finish first; sort back
    # into walk order so a re-run's recorded diagnostic is byte-comparable.
    walk_order = {commit_hash: i for i, commit_hash in enumerate(hashes)}
    failed.sort(key=walk_order.__getitem__)

    # `seen` was filled in result-consumption order, i.e. pool completion order,
    # which varies run to run once jobs > 1. This audit's output is a COMMITTED
    # artifact, so scheduling must not reach it: impose a total ordering on the
    # inputs themselves. name is Optional, and sorting None against str raises,
    # hence `or ""`.
    ordered = sorted(
        seen,
        key=lambda inp: (inp.entity_type, inp.producer, inp.file_path, inp.name or ""),
    )

    return ordered, {
        "head_commit": hashes[-1] if hashes else None,
        "branch": resolved_branch,
        "ignore_patterns": list(ignore_patterns),
        "commits": len(hashes),
        "extraction_failures": len(failed),
        "failed_commits": failed,
    }


# ---------------------------------------------------------------------------
# Stage 2: report, predictions, exit gate, CLI
# ---------------------------------------------------------------------------


# Fixed BEFORE any data exists, so the run cannot be rationalized afterwards.
# A prediction that fails is a finding about the design, not noise to be
# smoothed over: it is recorded in the result either way.
#
# Every statement below is worded to say exactly what evaluate_predictions
# mechanically checks. A statement broader than its check would let a "held"
# be read as evidence for something the audit never measured.
PREDICTIONS: Tuple[Tuple[str, str], ...] = (
    ("P1", "The true collision count exceeds the 3 #257's census found, "
           "because that census can only see collisions whose loser was "
           "closed and reopened."),
    ("P2", "leading-underscore STRICTLY outnumbers every other named shape "
           "among all offenders."),
    ("P3", "R5, the control, reports a nonzero residual at least as large as "
           "the leading-underscore offender count."),
    ("P4", "R2's rename cost falls on named entities, not on module idents: "
           "a module input has no '::' separator to produce an adjacent "
           "hyphen run, so R2 renames a strictly smaller FRACTION of module "
           "idents than of function idents."),
)

# NOT predictions. A prediction the data cannot contradict is a tautology, and
# one sitting in the predictions block inflates the held count with something
# that was never at risk. These are declared self-checks instead: statements
# that MUST hold by construction, kept because a failure indicts the scorer
# rather than the design. The model is RULES["R5"], whose description declares
# it the known-negative control instead of pretending to be a candidate.
#
# P5 lived here as a prediction until review: R4 is _ident_hash_suffix, which
# appends "-{digest}" to every ident unconditionally, so no run that clears the
# validity gate can falsify it. The label P5 is retired and deliberately not
# reused, so this artifact and the plan stay legible against each other.
SELF_CHECKS: Tuple[Tuple[str, str], ...] = (
    ("S1", "R4 appends a hash suffix to every ident by construction, so its "
           "rename count must equal the total ident count. If it does not, "
           "score_rule's rename accounting is broken and every other rule's "
           "rename number in this table is suspect."),
)


def _outcome_row(statement: str, held: bool, evidence: str) -> Dict[str, Any]:
    """One {statement, outcome, evidence} row, shared by predictions and
    self-checks so nothing downstream needs a second shape."""
    return {
        "statement": statement,
        "outcome": "held" if held else "failed",
        "evidence": evidence,
    }


def evaluate_self_checks(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Score the by-construction invariants. A failure indicts the SCORER.

    This is the rename-accounting analogue of the R5 control: R5 catches a
    residual counter that reports a known-colliding rule as clean, and S1
    catches a rename counter that has lost idents. Neither says anything about
    the design under audit -- which is exactly why they are reported apart from
    the predictions, where a held/failed tally is meant to be informative.
    """
    statements = dict(SELF_CHECKS)
    r4_renames = report["candidates"]["R4"]["renames"]
    idents_total = report["idents_total"]
    return {
        "S1": _outcome_row(
            statements["S1"],
            r4_renames == idents_total,
            f"R4 renames={r4_renames}, idents_total={idents_total}",
        ),
    }


def build_report(
    inputs: Sequence[EntityInput],
    diagnostics: Dict[str, Any],
    repo_path: str,
) -> Dict[str, Any]:
    """Assemble the committed artifact from Stage 1's inputs.

    Carries branch, head_commit AND ignore_patterns, because all three decide
    which idents were counted and none of them is recoverable from the numbers
    afterwards. head_commit is mainline's tip, not the working branch's, unless
    a branch was passed explicitly -- see the module docstring.

    The content-fetch caveat in the module docstring applies to every count
    here: they are exact with respect to the idents production generates, and
    reproducible only to the extent that git object reads are.
    """
    baseline = group_by_ident(inputs, current_ident)
    all_offenders = offenders(baseline)

    per_type: Dict[str, Dict[str, Any]] = {}
    for entity_type in ENTITY_TYPES:
        rows = {
            ident: members
            for ident, members in all_offenders.items()
            if members[0].entity_type == entity_type
        }
        per_type[entity_type] = {
            # A missing key and a zero are different claims; only the second
            # says "measured, found none". Every type appears unconditionally.
            "idents_total": sum(
                1 for _ident, members in baseline.items()
                if members[0].entity_type == entity_type
            ),
            "count": len(rows),
            # Verbatim members, not just a count: a nonzero count escalates
            # this to fix design, and that work starts from the data rather
            # than re-deriving it by hand.
            "idents": {
                ident: [
                    {"producer": m.producer, "file_path": m.file_path, "name": m.name}
                    for m in members
                ]
                for ident, members in rows.items()
            },
        }

    shape_counts: Dict[str, int] = {shape: 0 for shape in SHAPES}
    for members in all_offenders.values():
        for shape in classify_shapes(members):
            shape_counts[shape] += 1

    candidates = score_all_rules(inputs, baseline)
    for rule_id, (_description, ident_fn) in RULES.items():
        candidates[rule_id]["renames_by_entity_type"] = renames_by_entity_type(
            ident_fn, baseline
        )

    report: Dict[str, Any] = {
        "repo_path": repo_path,
        "branch": diagnostics["branch"],
        "head_commit": diagnostics["head_commit"],
        # Indexed, not .get()-with-default: a silent [] here would record "no
        # patterns were applied" for a run whose patterns simply were not
        # threaded, which is precisely the ambiguity this field exists to end.
        "ignore_patterns": diagnostics["ignore_patterns"],
        "commits": diagnostics["commits"],
        "extraction_failures": diagnostics["extraction_failures"],
        "failed_commits": diagnostics["failed_commits"],
        # NAMED for the (entity_type, file_path, name) triple the question is
        # framed on, but counted over DISTINCT EntityInputs, whose key is the
        # 4-tuple including `producer`. The two coincide only while
        # cross-producer is 0; on a history with gitlinks, one triple reached
        # from two producers counts twice here, so read this as "distinct
        # inputs", not "distinct triples".
        "triples_total": len(inputs),
        "idents_total": len(baseline),
        "offenders": per_type,
        "offenders_total": len(all_offenders),
        "offenders_by_shape": shape_counts,
        # Composed here rather than inside score_all_rules: that function's
        # {description, residual, renames} shape is Task 3's contract and a
        # test pins it. The breakdown answers WHERE a rule's rename cost lands,
        # which is what separates two rules that both reach zero residual.
        "candidates": candidates,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    report["predictions"] = evaluate_predictions(report)
    # Reported apart from the predictions on purpose: a reader counting
    # held/failed predictions must not be counting a by-construction invariant
    # among them.
    report["self_checks"] = evaluate_self_checks(report)
    return report


def evaluate_predictions(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Score every declared prediction against the measured report.

    Each outcome is derived from a number in the report. An evaluator that
    returned "held" unconditionally would be worse than no predictions at all,
    because it would launder a failed prediction as confirmation.
    """
    statements = dict(PREDICTIONS)
    shape_counts = report["offenders_by_shape"]
    leading = shape_counts.get("leading-underscore", 0)

    def _row(pid: str, held: bool, evidence: str) -> Dict[str, Any]:
        return _outcome_row(statements[pid], held, evidence)

    total = report["offenders_total"]
    r5 = report["candidates"]["R5"]["residual"]

    # P2 is ruled by STRICT comparison against the runner-up, not by max().
    # max() returns the first maximal element and SHAPES[0] is
    # "leading-underscore", so it named leading-underscore the winner on a
    # corpus where every count was 0 -- and a zero-offender run clears the
    # validity gate, so that laundered "held" reached the committed artifact.
    # The same first-wins bias read an exact tie as a win. This is the defect
    # P4 was corrected for, in a second place.
    rivals = sorted(
        (
            (shape_counts.get(shape, 0), shape)
            for shape in SHAPES
            if shape not in ("other", "leading-underscore")
        ),
        reverse=True,
    )
    runner_up_count, runner_up = rivals[0]
    if total == 0:
        p2_held = False
        p2_evidence = (
            "not evaluable: no offenders in this corpus "
            f"(offenders_total=0, leading-underscore=0)"
        )
    elif leading == 0 and runner_up_count == 0:
        # Offenders exist but every named shape is empty, i.e. they all landed
        # in "other". Distinct from the case above and worth saying so: "other"
        # is where an UNPREDICTED collision family surfaces, so this is a
        # finding, not an empty run.
        p2_held = False
        p2_evidence = (
            "not evaluable: no offender carries a named shape "
            f"(offenders_total={total}, all in 'other')"
        )
    else:
        # Strict: a tie is not a win. The runner-up is named and counted so a
        # near-tie is visible without re-running anything -- the old evidence
        # string showed only the winner, which made a 3-vs-3 read as dominance.
        p2_held = leading > runner_up_count
        p2_evidence = (
            f"leading-underscore={leading}, "
            f"runner-up {runner_up}={runner_up_count}"
        )

    # P4 is a claim about the SHAPE of R2's rename cost, so it is ruled on
    # fractions rather than on the whole-corpus total, which cannot distinguish
    # "spares module idents" from "renames slightly fewer than everything".
    r2_by_type = report["candidates"]["R2"]["renames_by_entity_type"]
    module_row = r2_by_type["module"]
    function_row = r2_by_type["function"]
    empty_denominators = [
        entity_type
        for entity_type, row in (("module", module_row), ("function", function_row))
        if row["total"] == 0
    ]
    if empty_denominators:
        # Not evaluable on this corpus. Recorded as "failed" rather than as a
        # third outcome value -- held/failed is pinned by a test -- but the
        # evidence must say WHY, or a reader sees a bare 0/0 and reads a real
        # ruling into a corpus that could not produce one.
        p4_held = False
        p4_evidence = (
            "not evaluable: no "
            + " or ".join(empty_denominators)
            + " idents in this corpus (module "
            f"{module_row['renamed']}/{module_row['total']}, function "
            f"{function_row['renamed']}/{function_row['total']})"
        )
    else:
        module_fraction = module_row["renamed"] / module_row["total"]
        function_fraction = function_row["renamed"] / function_row["total"]
        p4_held = module_fraction < function_fraction
        p4_evidence = (
            f"R2 renames module {module_row['renamed']}/{module_row['total']} "
            f"= {module_fraction:.3f}, function "
            f"{function_row['renamed']}/{function_row['total']} "
            f"= {function_fraction:.3f}"
        )

    return {
        "P1": _row("P1", total > 3, f"offenders_total={total}"),
        "P2": _row("P2", p2_held, p2_evidence),
        "P3": _row("P3", r5 >= leading and r5 > 0,
                   f"R5 residual={r5}, leading-underscore offenders={leading}"),
        "P4": _row("P4", p4_held, p4_evidence),
    }


def measurement_invalid(report: Dict[str, Any]) -> Optional[str]:
    """Why this run cannot be believed, or None.

    VALIDITY ONLY. A nonzero collision count is the number #263 asks for and
    must NEVER fail the run -- finding collisions is this audit's entire
    purpose. Do not widen this gate to make a run "pass".
    """
    if report["commits"] == 0:
        return "Zero commits walked. Nothing was measured."
    if report["triples_total"] == 0:
        return (
            "Zero inputs collected across "
            f"{report['commits']} commits. A run that collected nothing "
            "cannot report zero collisions as a finding."
        )
    failures = report["extraction_failures"]
    if failures > report["commits"] * 0.01:
        return (
            f"_extract_commit raised on {failures} of {report['commits']} "
            "commits (>1%). The input set is incomplete, so the count is a "
            "bound rather than the exact number this audit exists to produce."
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Count, exactly, how many _code_ident values on real history are "
            "reachable from more than one (entity_type, file_path, name) input."
        )
    )
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--json-out", "--output", dest="json_out", default=None)
    args = parser.parse_args()

    inputs, diagnostics = collect_inputs(args.repo_path, args.branch, jobs=args.jobs)
    report = build_report(inputs, diagnostics, args.repo_path)

    print(json.dumps(report, indent=2))
    print()
    print(f"repo:                   {report['repo_path']} @ {report['branch']}")
    # Printed next to the branch on purpose. With no --branch, this is
    # mainline's tip rather than the checked-out branch's, which reads as a
    # stale run unless both are visible together.
    print(f"head:                   {report['head_commit']}")
    print(f"ignore patterns:        {report['ignore_patterns'] or '(none)'}")
    print(f"commits:                {report['commits']}")
    print(f"extraction failures:    {report['extraction_failures']}")
    print(f"distinct inputs:        {report['triples_total']}")
    print(f"distinct idents:        {report['idents_total']}")
    print()
    print("OFFENDERS (idents reachable from >1 distinct input)")
    for entity_type in ENTITY_TYPES:
        row = report["offenders"][entity_type]
        print(f"  {entity_type:<12} {row['count']:>6} of {row['idents_total']}")
    print(f"  {'TOTAL':<12} {report['offenders_total']:>6}")
    print()
    print("BY SHAPE (an offender may carry more than one label)")
    for shape in SHAPES:
        print(f"  {shape:<20} {report['offenders_by_shape'][shape]:>6}")
    print()
    print("CANDIDATE RULES")
    print(f"  {'rule':<6} {'residual':>9} {'renames':>9}   description")
    for rule_id in sorted(RULES):
        row = report["candidates"][rule_id]
        print(
            f"  {rule_id:<6} {row['residual']:>9} {row['renames']:>9}   "
            f"{row['description']}"
        )
    print()
    # Only the zero-residual rules: they are the live contenders, and WHERE
    # their rename cost lands is what separates them. One line per rule per
    # type across all five rules would bury that under rows for rules already
    # ruled out.
    print("RENAME COST BY ENTITY TYPE (zero-residual rules only)")
    contenders = [r for r in sorted(RULES) if report["candidates"][r]["residual"] == 0]
    if not contenders:
        print("  (none: no candidate rule reached zero residual)")
    for rule_id in contenders:
        breakdown = report["candidates"][rule_id]["renames_by_entity_type"]
        cells = " ".join(
            f"{entity_type}={breakdown[entity_type]['renamed']}"
            f"/{breakdown[entity_type]['total']}"
            for entity_type in ENTITY_TYPES
        )
        print(f"  {rule_id:<6} {cells}")
    print()
    print("PREDICTIONS (fixed before the run; a failure is a finding)")
    for pid, _ in PREDICTIONS:
        row = report["predictions"][pid]
        print(f"  {pid}  {row['outcome']:<7} {row['evidence']}")
    print()
    # Under its own heading, never inside the predictions block: these hold by
    # construction, so counting them among the predictions would inflate the
    # held tally with something that was never at risk.
    print("SELF-CHECKS (hold by construction; a failure indicts the SCORER)")
    for sid, _ in SELF_CHECKS:
        row = report["self_checks"][sid]
        print(f"  {sid}  {row['outcome']:<7} {row['evidence']}")
        if row["outcome"] == "failed":
            print(f"      {row['statement']}")

    if report["candidates"]["R5"]["residual"] == 0 and (
        report["offenders_by_shape"]["leading-underscore"] > 0
    ):
        print()
        print(
            "NOTE: R5 is the CONTROL and reported zero residual on a history\n"
            "that HAS leading-underscore offenders. R5 slugs the name\n"
            "independently, and strip('-') still eats the leading underscore,\n"
            "so it cannot separate them. The scorer is wrong and every other\n"
            "row in the candidate table is suspect. Do not act on this run."
        )

    reason = measurement_invalid(report)
    if reason:
        print()
        print(f"INVALID MEASUREMENT. {reason}")
        print("Do not adjust this gate to make a run pass.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.json_out}")
    return 1 if reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
