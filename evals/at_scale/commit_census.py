# evals/at_scale/commit_census.py
"""#317: count the graph's `:type/commit` entities against the repo itself.

THE LOSS NO FACT-LEVEL CHECK CAN SEE. Every content check the at-scale tier
had before this one is fact-level, and a commit that never reached the graph
at all is invisible to all of them:

  * `fact_audit.divergence` compares the graph against its own fact index.
    Both are written from the same triples in the same transaction boundary,
    so a commit that was never written is absent from BOTH. The two witnesses
    agree perfectly. This is the blind spot fact_audit's own docstring names.
  * `introduced_by_duplicates` (#287) and `entities_without_introduced_by`
    (#316) are well-formedness checks on entities that EXIST. A commit that
    produced no entities produces nothing for either to find.
  * `stderr_capture` only sees corruption that prints, and #313 measured a
    whole class of it printing zero bytes.
  * `_exit_code`'s `graph_facts == 0` clause is the one existing gesture at
    this, and it only fires when the graph reads back COMPLETELY empty. One
    commit missing in 847 is not zero facts.

THREE NUMBERS, NOT ONE DELTA, so a mismatch says WHERE it happened:

  1. `repo_commits` -- `git rev-list --count <ref>`, what the repo holds.
  2. `walk_claimed` -- `walk_claimed_from_progress(_ingest_progress)`, what
     the walk CLAIMS it applied. An in-process counter, never read back from
     the graph.
  3. `graph_commit_entities` -- `mcp_server._count_commit_entities`, what the
     graph can actually produce.

`walk_vs_graph` (2 vs 3) is the cheap comparison and needs no repo handle: the
walk's own claim against the graph's answer. It catches a commit that was
walked and then lost.

`repo_vs_walk` (1 vs 2) is the strong one, and the reason this is not just
another `fact_audit` key. It catches a commit that was NEVER WALKED -- a
linearization that dropped a position, a frontier claim that skipped one.
That is the case no in-process counter can see, because the counter and the
walk share the bug.

WHAT WAS MEASURED BEFORE THIS WAS GATED (see results/317-commit-census.json).
CLAUDE.md's standing rule is that a zero-tolerance gate needs its clean
baseline measured AND its positive control checked, and this one had a
specific extra hazard: the clean difference might not have been zero, and
shipping it as if it were would have repeated the trap #316 had to avoid with
`:type/external-dependency`. Each of these was resolved from the code and then
confirmed on a real run rather than assumed:

  * MERGE COMMITS COUNT ON BOTH SIDES. `frontier_registry.build_linearization`
    is `git log --topo-order --reverse --format=%H <branch>`, the same commit
    set `git rev-list --count <branch>` reports, merges included -- and both
    `_forward_apply` and `_reverse_apply` write
    `[commit_ident :entity-type :type/commit]` unconditionally as the FIRST
    triple in their list. 66 of this repo's 847 commits are merges.
  * PATH-IGNORE CHANGES NOTHING. That triple is written before any extracted
    file is looked at, so a commit touching only ignored paths still gets its
    entity.
  * AN EXTRACTION-SKIPPED COMMIT RAISES 2 WITHOUT RAISING 3. `_run_ingestion`'s
    per-commit `except` retires the position as `failed` and `continue`s from
    ABOVE the lease, so nothing is written. Such a run is
    already failed by `_exit_code`'s `skipped_commits` clause; the census fires
    too, independently, which is the point -- that clause is stderr-derived and
    reads clean when the capture itself fails.
  * THE REF IS THE RESOLVED BRANCH, NEVER `HEAD`. `_run_ingestion`'s own
    `repo_total` was hardcoded to `HEAD` while ingestion takes a `branch`
    argument; a census inheriting that would be wrong for any non-HEAD run and
    silently so. Fixed at the source in the same change; this module takes the
    ref explicitly and never defaults it.

AN INCOMPLETE RUN IS NOT FAILED FOR WALKING FEWER. `final_status` has five
non-running values and only `complete` means the walk was supposed to reach
the end; a `stopped` run walked fewer BY DESIGN. So `repo_vs_walk` and
`repo_vs_graph` are gated only on a completed run. `walk_vs_graph` is gated
ALWAYS, because it needs no completion assumption: however few commits the
walk claimed, the graph must hold that many.
"""

from __future__ import annotations

import subprocess
from typing import Any, Mapping, Optional

# `:commit/{hash[:12]}` -- mcp_server's commit ident rule, in _forward_apply,
# _reverse_apply and every pos_by_commit_ident map. Mirrored rather than
# imported so this module stays importable without mcp_server; the real
# collector below asserts nothing about it, it only counts prefixes of this
# length.
COMMIT_IDENT_PREFIX_LEN = 12


def walk_claimed_from_progress(progress: Mapping[str, Any]) -> int:
    """`walk_claimed` from `mcp_server._ingest_progress` (#222 phase 4).

    `prior_ingested` (the graph's commit count at run start) plus the
    positions this run RETIRED -- written, skipped or failed. That is exactly
    the number `_ingest_progress["processed"]` held, so every gate
    below keeps its meaning. A run that failed before its RunProgress was
    built (`_run` None or absent) retired nothing.
    """
    run = progress.get("_run")
    return progress.get("prior_ingested", 0) + (run.retired_count if run is not None else 0)


def commit_census(
    repo_commits: int,
    walk_claimed: int,
    graph_commit_entities: int,
    distinct_commit_idents: int,
    final_status: str,
    census_error: Optional[str] = None,
) -> dict[str, Any]:
    """Compare the three counts and return the gate's verdict.

    PURE. It compares integers somebody else collected, so it can be tested
    without a repo or a graph -- and so the collection half can fail loudly
    (`census_error`) without this half having to guess.

    Returns the three raw counts, the three deltas, `ident_collisions`,
    `proved_nothing`, `ok`, `interpretation` and `census_error`.

    THE RAW COUNTS ALWAYS SHIP, including on a run whose deltas are not
    gated. Not gated is not unmeasured: a reader comparing runs needs to see
    how far an interrupted walk actually got, and `repo_commits` is the
    denominator that makes a zero believable at all.

    `proved_nothing` IS THE POSITIVE CONTROL. Three counts that are all zero
    match perfectly, so a census over a repo with no commits would otherwise
    be indistinguishable from a census over a healthy one. It is reported and
    NOT failed -- a repo holding no commits is not a defect -- exactly as
    #316's `code_entities_scanned == 0` renders as "proved nothing about it".
    A failed collection reaches this function with `repo_commits == 0` too,
    which is why `census_error` is checked FIRST: the exemption must not
    swallow the error it looks exactly like.

    THE DIAGNOSES OVERLAP, so `interpretation` names the most SPECIFIC cause
    and the raw deltas carry the rest. An ident collision produces a
    `walk_vs_graph` of exactly the same shape as a lost write, so the
    collision branch is checked FIRST -- otherwise a collision reads as a
    write-path bug and sends a reader to the wrong code. `census_error` beats
    every delta for the same reason it beats the empty-repo exemption: numbers
    gathered by a collection that failed are not evidence of anything. Nothing
    is hidden by the ordering; only the headline is chosen by it.

    `ident_collisions` is `repo_commits - distinct_commit_idents`, and it is a
    hazard this census would otherwise MISATTRIBUTE rather than one it exists
    to find. Two commits sharing a 12-character hash prefix collapse into one
    `:commit/...` entity, so the graph legitimately holds fewer than the repo
    -- through no fault of the write path the other deltas point at. It is a
    genuine loss and still fails; the extra number only makes the failure
    attributable. Measured 0 (847 distinct prefixes of 847) when this shipped.
    """
    deltas = {
        "repo_vs_walk": repo_commits - walk_claimed,
        "walk_vs_graph": walk_claimed - graph_commit_entities,
        "repo_vs_graph": repo_commits - graph_commit_entities,
    }
    ident_collisions = repo_commits - distinct_commit_idents
    complete = final_status == "complete"
    proved_nothing = census_error is None and repo_commits == 0

    if census_error is not None:
        ok = False
        interpretation = (
            f"census could not run ({census_error}) -- unverified, not "
            f"verified-clean."
        )
    elif proved_nothing:
        ok = True
        interpretation = (
            "repo holds no commits, so this census proved nothing about the "
            "graph. Not a defect, and not evidence either."
        )
    elif ident_collisions:
        ok = False
        interpretation = (
            f"{ident_collisions} commits share a {COMMIT_IDENT_PREFIX_LEN}-"
            f"character hash prefix with another, so the commit ident rule "
            f"collapses them into one entity. A real loss, but the ident rule "
            f"is where it happened, not the write path."
        )
    elif deltas["walk_vs_graph"]:
        ok = False
        interpretation = (
            f"the walk claimed {walk_claimed} commits but the graph holds "
            f"{graph_commit_entities} -- {deltas['walk_vs_graph']} walked and "
            f"then lost on the write path."
        )
    elif complete and deltas["repo_vs_walk"]:
        ok = False
        interpretation = (
            f"the repo holds {repo_commits} commits but the walk only claimed "
            f"{walk_claimed} -- {deltas['repo_vs_walk']} never walked at all. "
            f"No in-process counter can see this: the counter and the walk "
            f"share the bug."
        )
    else:
        ok = True
        interpretation = (
            f"repo, walk and graph agree at {graph_commit_entities} commits."
            if complete
            else (
                f"run ended {final_status!r}, so the shortfall against the "
                f"repo is expected; the walk's {walk_claimed} claimed commits "
                f"are all present in the graph."
            )
        )

    return {
        "ref": None,
        "repo_commits": repo_commits,
        "walk_claimed": walk_claimed,
        "graph_commit_entities": graph_commit_entities,
        "distinct_commit_idents": distinct_commit_idents,
        **deltas,
        "ident_collisions": ident_collisions,
        "final_status": final_status,
        "proved_nothing": proved_nothing,
        "ok": ok,
        "interpretation": interpretation,
        "census_error": census_error,
    }


def repo_commit_counts(repo_path: str, ref: str) -> tuple[int, int]:
    """`(rev-list --count <ref>, distinct 12-char hash prefixes)`.

    TWO SUBPROCESSES, NOT ONE. The count could be derived from the hash list,
    but `--count` is the number `_run_ingestion` itself uses for
    `_ingest_progress["total"]`, and the census's whole value is asking the
    same question the same way rather than a reimplementation that could drift.

    `check=True`: a `git` that failed is not a repo with zero commits, and
    the caller turns the raised error into a `census_error` rather than a
    count of 0 (see collect_commit_census).
    """
    count = subprocess.run(
        ["git", "rev-list", "--count", ref],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    hashes = subprocess.run(
        ["git", "rev-list", ref],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    prefixes = {
        line[:COMMIT_IDENT_PREFIX_LEN]
        for line in hashes.stdout.split()
        if line
    }
    return int(count.stdout.strip()), len(prefixes)


def graph_commit_entities(db: Any) -> int:
    """`mcp_server._count_commit_entities`, imported rather than re-typed.

    That function is the one `prepare_turn` already consults for "does this
    graph hold any ingested history", and its docstring states the property
    this census needs: it reflects reality even after a run was interrupted
    before it could write its completion watermark. Re-typing its query here
    would let the census and the production path drift into counting
    different things, which is the one way this check could go quietly wrong.
    """
    import mcp_server

    return mcp_server._count_commit_entities(db)


def collect_commit_census(
    repo_path: str,
    ref: str,
    walk_claimed: int,
    db: Any,
    final_status: str,
) -> dict[str, Any]:
    """Gather the three counts and hand them to `commit_census`.

    THE COLLECTION FAILS INTO `census_error`, NEVER INTO A ZERO. A `git` that
    could not run, or a graph too damaged to answer a count query, is the
    loudest possible result of this census -- but an exception here would
    destroy the metrics of the run that found it, exactly as
    `fact_audit._graph_facts` documents for its own scan. So the failure is
    RECORDED and gated on, and the counts it could not gather stay 0 while
    `commit_census` refuses to read that 0 as an empty repo.

    `db` is a leased handle supplied by the caller, never opened here: the
    single-handle invariant applies to this census like everywhere else.
    """
    repo_commits = 0
    distinct = 0
    graph_count = 0
    error: Optional[str] = None
    try:
        repo_commits, distinct = repo_commit_counts(repo_path, ref)
        graph_count = graph_commit_entities(db)
    except Exception as e:  # noqa: BLE001 -- recorded, see docstring
        error = f"{type(e).__name__}: {e}"

    result = commit_census(
        repo_commits=repo_commits,
        walk_claimed=walk_claimed,
        graph_commit_entities=graph_count,
        distinct_commit_idents=distinct,
        final_status=final_status,
        census_error=error,
    )
    result["ref"] = ref
    return result
