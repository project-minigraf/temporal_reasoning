# evals/at_scale/probe_resume_census.py
"""#325: the commit census on a RESUMED graph -- the scenario nothing else runs.

WHY THE NIGHTLY'S OWN commit_census CANNOT SEE THIS. `commit_census` (#317)
runs inside `run_ingestion_benchmark`, which ingests once into a fresh graph.
#325 makes `_frontier_load` RETAIN a persisted high interval whose bounds
still resolve, `lo <= hi`, and whose stored `:pos-count` still matches its
span -- and a WRONGLY-retained interval means its positions are never
re-claimed, on any run, ever. A fresh-graph run has no interval to retain in
the first place, so it can never exercise the failure mode this issue
introduced. Every other at-scale detector reads a wrong retention clean too:
`fact_audit`'s two witnesses agree perfectly about a commit neither the graph
nor the index ever received; both `:introduced-by` checks (#287, #316) only
examine entities that EXIST; and `stderr_capture` has nothing to read, because
skipping a position that looks complete prints nothing.

WHAT THIS PROBE DOES. Ingests `<branch>~<truncate_by>` into a fresh graph,
then ingests `<branch>` into the SAME graph -- the resume that can trigger
retention -- then hands the three resulting counts to the existing
`collect_commit_census`, reused VERBATIM. Reusing it rather than
reimplementing the comparison is deliberate: a second copy of "count three
things and diff them" is exactly how this probe and the nightly gate could
drift into silently counting different things.

WHAT "CAN OBSERVE THAT FAILURE" DOES NOT MEAN, AGAINST THE BOUND THIS SAME
CHANGE SHIPS WITH. The nightly step pins `--branch` to the commit at a FIXED
position from repo root and a fixed `--truncate-by` (see
evals/at_scale/benchmark.md and the workflow comment beside the step) rather
than the branch's growing tip, for cost -- so every nightly run re-plays an
IDENTICAL frozen scenario. That is enough to catch a REGRESSION in the
retention predicate itself (the pre-#325 discard-on-tip-growth behaviour
reappearing), which is this probe's actual job. It can NEVER observe a newly
landed commit arriving INSIDE an already-retained interval's bounds -- the
exact scenario `:pos-count`'s checksum was written to catch (see
frontier_registry / mcp_server.py's `:pos-count` discussion) -- because the
frozen slice never grows THAT particular retained interval's span again
after this probe's own run claims it.

That is a narrower gap than "the checksum path is untouched": the probe DOES
put `:pos-count` in play once per run, on its OWN resume. The first ingestion
builds a linearization for `<branch>~<truncate_by>`; the second re-derives a
linearization for the full `<branch>`, which has grown by exactly
`truncate_by` commits since the first was recomputed -- a range genuinely
still being appended to -- and `_frontier_load`'s retain check compares the
interval's stored count against ITS span under that grown linearization. A
run reporting `retention_engaged: true` (see `results/325-resume-census.json`,
truncate_by=30, prior_ingested=262) is evidence the count check ran and
passed, not evidence it was never reached: a mismatch there would discard the
interval and push `retired_this_run` up toward `repo_commits`, reading
`retention_engaged` False. What genuinely never happens in one run of this
probe is a SECOND append landing inside the interval this run's own resume
just retained -- that would need a third ingestion pass this probe does not
make. The error in an earlier draft of this note was conservative (it
understated the probe's own coverage), but a reader could still conclude the
checksum path itself is never touched here, which is wrong.

Also worth stating plainly: `retention_engaged` has ZERO MARGIN at the
truncate_by boundary. `retired_this_run < repo_commits` is a strict
inequality with no threshold, so a partial regression that re-walks 299 of a
300-position resume still reads `retention_engaged: True` -- it discriminates
a TOTAL regression in the retention predicate (a full re-walk), never a
partial one.

The ident-collision census immediately above
this one in the nightly deliberately carries no `--since` bound for the
matching reason (its own comment: the pair to worry about is new-vs-old, and
a bounded collection would see only new-vs-new); this probe's bound is the
opposite trade, accepted for cost, and named here rather than left implicit.

WALK_CLAIMED IS NOT A MANUAL RESET, AND ITS OWN FORMULA IS ALREADY
ESTABLISHED. `_run_ingestion` builds a fresh `RunProgress` for every call
(`_build_run_progress`, mcp_server.py) from the frontier AS LOADED --
linearization, the allocator's unclaimed count, and the two watermarks --
NEVER from `prior_ingested`; its `written`/`skipped`/`failed` counts start
at 0 every run, and `RunProgress.retired_count` increments for every
position retired THIS run (written, skipped or failed), never a cumulative
total. `prior_ingested` is a SEPARATE value, recomputed at the top of every
call (`_count_commit_entities(db)` -- see `_load_ingestion_preload_state`)
and stored at `_ingest_progress["prior_ingested"]`, untouched by
`RunProgress` itself. `walk_claimed_from_progress`
(evals/at_scale/commit_census.py, #222 phase 4) is the one place that
combines the two: `prior_ingested + run.retired_count`. This probe reads
`run.retired_count` straight off the `RunProgress` for
`retired_this_run` rather than re-deriving it by subtraction, and rather
than the earlier draft's `_ingest_progress["processed"] = 0` reset before
the resume call -- which would have done nothing under the old mechanism
either, because `_run_ingestion` overwrote `processed` back to its own
`prior_ingested` immediately after setting `_ingest_progress["status"] =
"running"`, before a single commit was walked. That draft's `this_run` was
silently the CUMULATIVE total, not the run's own delta, and handing it to
`walk_claimed = prior + this_run` would have double-counted the overlap.
`walk_claimed_from_progress(mcp_server._ingest_progress)` after the resume
call already equals `prior_ingested + retired_this_run` by construction, so
`walk_claimed` is just that helper, called once, after the resume
completes.

WHY THE PROBE'S OWN `ok` IS `repo_vs_graph`, NOT collect_commit_census's --
A CONTROLLER RULING, not this file's own design choice. Two earlier stated
reasons for this were wrong; the mechanism is the one already measured in
CLAUDE.md's #326 section (`walk_vs_graph` is nonzero on ANY resume that
touches already-ingested territory, skip or no skip -- measured 10 with the
fast path against 9 without). `commit_census`'s `ok` gates on
`ident_collisions`, `walk_vs_graph` (always) and `repo_vs_walk` (when
complete). A resume that RE-TOUCHES already-ingested territory -- the #326
same-run skip fast path, #313's torn-position repair re-walk, and this
branch's own below-`rev_claim_floor` re-walk are all examples, and all
three are CORRECT behaviour, not degraded resumes -- double-counts that
territory: `RunProgress.retired_count` counts positions RETIRED this run
(skip, extraction failure, or reaching write dispatch regardless of outcome
-- mcp_server.py's three retirement sites), never commits actually WRITTEN,
and `walk_claimed_from_progress` adds it to `prior_ingested` at run start,
so a re-touched position already inside that seed drives `walk_claimed`, and
therefore
`walk_vs_graph = walk_claimed - graph_commit_entities`, POSITIVE on a
perfectly healthy run. `collect_commit_census` gates `walk_vs_graph` BEFORE
`repo_vs_walk` (an `elif` chain in commit_census.py), so `walk_vs_graph` is
the clause that actually fails such a run -- that is what makes it a false
positive. `repo_vs_walk`'s own clause
(`elif complete and deltas["repo_vs_walk"]:`) is only reached once
`walk_vs_graph` reads falsy (zero), at which point
`walk_claimed == graph_commit_entities` forces
`repo_vs_walk == repo_vs_graph`, zero on an intact graph, so that clause is
falsy too. `repo_vs_graph` asks the only question this probe exists to
answer: after a resume, does the graph hold every commit the repo has? It
sidesteps both gates above because it never routes through `walk_claimed`
at all -- see `resume_ok`.

THE REF IS THE RESOLVED BRANCH, NEVER "HEAD". `_run_ingestion`'s own
`repo_total` was hardcoded to `HEAD` while ingestion took a `branch` argument,
live in the very run that measured #317. This probe's `branch` parameter has
no default and is never substituted with `"HEAD"`.

The nightly's two instances of that same shape are now CLOSED (#330), and this
paragraph used to describe them as live -- do not read an older copy of it as
current. The ingestion step passed the literal string `--branch HEAD`, which
is truthy and therefore defeated `run_ingestion_benchmark`'s own
`branch or _default_git_branch(...)` rather than adding to it; it now omits
the argument entirely, and `main()` refuses that literal outright. The step
that drives THIS probe resolved its linearization with
`git rev-parse --abbrev-ref HEAD`, which prints the literal string `HEAD` on a
detached checkout -- the one value `--branch`'s own help text below forbids --
and prints the FEATURE branch on a `workflow_dispatch`, which is #317's
scenario verbatim; it now calls `mcp_server._default_git_branch` directly, so
it tracks the shipped resolution by construction rather than by a matching
guess. `tests/test_at_scale_nightly_workflow.py` keeps both closed.

STATUS KEY VERIFIED, NOT ASSUMED. `_ingest_progress["status"]` is the field
`handle_minigraf_ingest_status` itself reads (mcp_server.py) and the one
`_run_ingestion` sets to `"running"`, `"complete"`, `"stopped"`, `"error"` or
`"skipped"` -- confirmed by reading `_run_ingestion` and
`handle_minigraf_ingest_status` directly rather than assumed from the brief.

See results/325-resume-census.json for the measured baseline and
evals/at_scale/benchmark.md for the section documenting it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mcp_server  # noqa: E402

from evals.at_scale.commit_census import (  # noqa: E402
    collect_commit_census,
    walk_claimed_from_progress,
)

__all__ = ["run_resume_census", "resume_ok", "retention_engaged", "main"]

# mcp_server.py's own module-level `_ingest_progress` initializer (the value
# it holds before any run has ever started) -- NOT `handle_minigraf_ingest_git`'s
# dict literal, which writes `"status": "starting"` rather than `"idle"` and is
# otherwise identical; this mirrors the module-level one specifically. Neither
# is a named constant in mcp_server.py, so this is copied rather than imported.
# This probe calls `_run_ingestion` directly, bypassing the handler that
# normally performs this reset, so without it `_ingest_progress` is a bare
# module global that outlives one `run_resume_census` call: a second call in
# the same process (this file's own multi-test suite runs several in one
# pytest session) would start from whatever the FIRST call's run left behind
# -- e.g. an empty second repo's failed `_run_ingestion` calls touch
# `processed` and `prior_ingested` not at all, so a prior test's real counts
# would leak straight through as this run's numbers. See
# test_ingest_progress_does_not_leak_across_calls.
_CLEAN_INGEST_PROGRESS: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    "phase": None, "positions_skipped": 0,
}


def resume_ok(census: Dict[str, Any]) -> bool:
    """The probe's own verdict -- deliberately NOT `census["ok"]`.

    See the module docstring's "WHY THE PROBE'S OWN `ok`" section for why
    `collect_commit_census`'s gate (ident collisions, `walk_vs_graph` always,
    `repo_vs_walk` when complete) is the wrong instrument on a resumed graph.
    `repo_vs_graph` is the one delta that does not route through
    `walk_claimed` at all, so it reads identically whether this run's resume
    skipped every already-ingested position or re-walked all of them --
    exactly the property a check of #325's retention predicate needs.

    `census_error is None` is checked FIRST and explicitly, not folded into
    `repo_vs_graph` implicitly. A census_error routes BOTH `repo_commits` and
    `graph_commit_entities` to the same zero default inside
    `collect_commit_census`, so `repo_vs_graph` alone reads 0 on a run whose
    collection failed outright -- the exact "unverified reads as
    verified-clean" shape `commit_census`'s own `ok` (`census_error is not
    None` -> `ok = False`, checked before its own `proved_nothing` branch)
    exists to refuse. A persisted result reading `"ok": true` beside a
    non-null `census_error` is a defect, not a design choice: `census_error`
    ships in the result for a human to see regardless, but `ok` must agree
    with it, not read around it. `main()` additionally fails the CLI's exit
    code unconditionally on `census_error` (belt-and-suspenders, matching
    `probe_ident_collision_new_history.py`'s `measurement_invalid` always
    failing regardless of `--fail-on-collision`) -- but that no longer papers
    over this field disagreeing with it.
    """
    return census["census_error"] is None and census["repo_vs_graph"] == 0


def retention_engaged(census: Dict[str, Any]) -> bool:
    """Did this run's resume actually exercise the retention branch it exists
    to watch, or did it just re-walk everything and still land on a clean
    total?

    RENDERED, NEVER GATED -- the positive control, not a second verdict.
    Without it, a future change that made `_frontier_load` always discard
    (i.e. reverted #325 entirely) would make this probe re-walk all
    `repo_commits` positions from scratch on the "resume" and still report
    `repo_vs_graph == 0` -- the graph ends up complete either way, since
    minigraf collapses a re-transacted commit triple at an identical
    `commit_ts_iso` rather than duplicating it. `ok` alone cannot tell a
    correct skip apart from a wasteful full re-walk that happens to land on
    the same total, so a regression in the mechanism this probe exists to
    guard would read green forever -- the same "a check that matched nothing
    also reports 0" trap #316's `code_entities_scanned` and #317's
    `repo_commits` both guard against, applied to THIS probe's own positive
    control rather than to a downstream count.

    `prior_ingested > 0`: there has to have been something already in the
    graph for a resume to be resuming AT ALL. A truncate_by of 0 does NOT
    produce that case: `truncated_ref = f"{branch}~{truncate_by}" if
    truncate_by else branch` treats 0 as falsy, so the FIRST ingestion walks
    the full branch and `prior_ingested` comes back as the full count --
    still > 0 (a resume onto an already-complete graph, which the second
    ingestion then does nothing further with). What actually produces
    `prior_ingested == 0` is a truncated ref with no history below it at
    all -- an UNRESOLVABLE `<branch>~N` (`N` at or beyond the branch's own
    commit count), which fails ingestion outright and leaves the seeded
    `prior_ingested` at its `_CLEAN_INGEST_PROGRESS` default of 0. That is
    the case this clause reads False rather than True by coincidence, not a
    `truncate_by=0` run.

    `retired_this_run < repo_commits`: the run did NOT re-walk (or
    re-claim) every position the repo has. On a healthy resume this is
    `retired_this_run == repo_commits - prior_ingested` (only the newly
    appended commits were freshly retired); a wrongly-discarding
    `_frontier_load` would instead push `retired_this_run` up toward
    `repo_commits` as it re-walks the whole already-ingested region. This is
    a WEAKER check than counting skipped positions directly
    (`skipped_this_run` reads 0 on a perfectly healthy resume too -- see its
    own comment -- because a retained region is excluded from the walkable
    gap before the loop begins, never iterated-then-skipped), which is why
    it is phrased as "did the run avoid re-walking everything", not "did the
    skip counter fire".
    """
    return (
        census["prior_ingested"] > 0
        and census["retired_this_run"] < census["repo_commits"]
    )


async def run_resume_census(
    repo_path: str, branch: str, graph_path: str, truncate_by: int
) -> Dict[str, Any]:
    """Ingest `<branch>~<truncate_by>`, resume to `<branch>` in the SAME
    graph, then census the result. See the module docstring for the full
    design; this is the collection half, mirroring
    `collect_commit_census`'s own split between collection and comparison.

    Single-handle invariant: `_reset_db_state()` runs first, unconditionally,
    so a leaked lease from an earlier call in this process (another test in
    this file, or a prior probe run in a long-lived caller) is released
    before this one opens its own.
    """
    mcp_server._reset_db_state()
    mcp_server.open_db(graph_path)
    mcp_server._ingest_progress = dict(_CLEAN_INGEST_PROGRESS)

    # No try/except around either call: `_run_ingestion`'s own top-level
    # `except Exception as e:` (inside its `try`, guarding the bulk of the
    # function body -- bad ref, unreadable blob, an empty repo's `~N` failing
    # to resolve) reports the failure through `_ingest_progress["status"]` /
    # `["error"]` rather than raising -- confirmed by reading `_run_ingestion`
    # directly. NOT "every Exception-rooted failure" without qualification,
    # though: three statements run above that try
    # (`_shutdown_requested.clear()`, `_reset_introduced_by_ambiguity_log_
    # budget()`, `await owner_hint.__aenter__()`), and an exception raised by
    # any of those would propagate past this call uncaught. None of them do
    # in practice (a clear/reset touch no I/O, and `owner_hint.__aenter__()`
    # is a best-effort hint write), which is why removing the guards here is
    # safe -- but the safety is "these three calls do not fail today", not
    # "the try covers everything". A wrapper here would also only catch
    # BaseException-rooted control flow (asyncio.CancelledError,
    # KeyboardInterrupt), which `except Exception` cannot do either, so it
    # would add no protection against what the inner try already covers --
    # see the review that flagged an earlier draft's guard here as claiming
    # otherwise.
    truncated_ref = f"{branch}~{truncate_by}" if truncate_by else branch
    await mcp_server._run_ingestion(repo_path, truncated_ref)
    await mcp_server._run_ingestion(repo_path, branch)

    walk_claimed = walk_claimed_from_progress(mcp_server._ingest_progress)
    prior_ingested = mcp_server._ingest_progress.get("prior_ingested", 0)
    run = mcp_server._ingest_progress.get("_run")
    retired_this_run = run.retired_count if run is not None else 0
    final_status = mcp_server._ingest_progress.get("status", "error")

    census = collect_commit_census(
        repo_path=repo_path,
        ref=branch,
        walk_claimed=walk_claimed,
        db=mcp_server.get_db(),
        final_status=final_status,
    )
    census["prior_ingested"] = prior_ingested
    census["retired_this_run"] = retired_this_run
    census["truncate_by"] = truncate_by
    # NOT what #325's retention shows up as -- a RETAINED interval's
    # positions are excluded from the walkable gap entirely (they are never
    # claimed this run at all), so a HEALTHY retention-using resume HOLDS
    # `retired_this_run` AT `repo_commits - prior_ingested` (see
    # retention_engaged's own docstring: "On a healthy resume this is
    # retired_this_run == repo_commits - prior_ingested") -- it does not
    # push it below that value. What pushes `retired_this_run` UP, toward
    # `repo_commits`, is a WRONGLY-discarding `_frontier_load` re-walking
    # territory retention should have held out of this run; that upward
    # direction is what `retention_engaged`'s own `< repo_commits` check
    # actually watches for. This counter (`skipped_this_run`) is a DIFFERENT
    # signal, #326's own same-run skip-fast-path (a position retired via an
    # archived `:type/completed-region` without parsing or writing it) --
    # and after #325 that path is narrowed to the unresolvable-bounds
    # ("divergent-ref leak") case, mutually exclusive within one run with
    # what this probe's own resume can ever produce (see CLAUDE.md's "#326's
    # skip fast path is now VESTIGIAL" paragraph). The measured baseline
    # (results/325-resume-census.json) is exactly this: `skipped_this_run: 0`
    # alongside `retention_engaged: true` on a perfectly healthy resume -- 0
    # here is the expected reading for this probe's own scenario, not
    # evidence the mechanism failed to engage. Rendered for visibility
    # regardless, in case a future scenario (a divergent ref) does exercise
    # it.
    census["skipped_this_run"] = run.skipped if run is not None else 0
    # Rendered, never gated -- see retention_engaged's own docstring. A run
    # where this reads False is not a failure by itself (the census could
    # still be perfectly clean); it means THIS run proved nothing about
    # whether retention engaged, which is exactly the distinction #316's
    # `code_entities_scanned` and #317's `repo_commits` denominators exist to
    # preserve for their own checks.
    census["retention_engaged"] = retention_engaged(census)
    # Overrides collect_commit_census's own `ok` -- see resume_ok's docstring
    # for why that field is unsound on a resumed graph. `census["ok"]` (that
    # verdict) is kept alongside it under a distinct key rather than
    # discarded, so a reader can still see what the walk-based gate would
    # have said.
    census["commit_census_ok"] = census["ok"]
    census["ok"] = resume_ok(census)
    return census


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Census a RESUMED graph: ingest <branch>~<truncate-by>, then "
            "resume to <branch> in the same graph, then compare repo/walk/"
            "graph commit counts. The only at-scale check that can observe a "
            "wrongly-RETAINED frontier interval (#325) at all -- but see the "
            "module docstring for what a FIXED (--branch, --truncate-by) "
            "pair, as the nightly runs this, cannot observe: a newly landed "
            "commit inside an already-retained interval's bounds."
        )
    )
    ap.add_argument("--repo", required=True, help="Path to the repo to ingest.")
    ap.add_argument(
        "--branch", required=True,
        help="The RESOLVED branch name -- never 'HEAD'. See the module docstring.",
    )
    ap.add_argument("--graph", required=True, help="Graph path for the scratch resume graph.")
    ap.add_argument(
        "--truncate-by", type=int, required=True,
        help=(
            "Commits to hold back from the first ingestion (ingests "
            "<branch>~<truncate-by>, then resumes to <branch>). Large enough "
            "to exercise retention, small enough to stay affordable in a "
            "nightly -- see evals/at_scale/benchmark.md for the chosen value "
            "and its measured cost."
        ),
    )
    ap.add_argument("--out")
    ap.add_argument(
        "--fail-on-mismatch", action="store_true",
        help=(
            "Exit 1 if repo_vs_graph is nonzero -- a resume that lost a "
            "commit. OFF by default, matching probe_ident_collision_new_"
            "history.py's precedent: a finding must not be indistinguishable "
            "from an invalid run. A census_error fails the run regardless of "
            "this flag; see below."
        ),
    )
    args = ap.parse_args()

    result = asyncio.run(
        run_resume_census(args.repo, args.branch, args.graph, args.truncate_by)
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")

    # UNCONDITIONAL, like probe_ident_collision_new_history.py's
    # measurement_invalid: a census whose own collection failed must never
    # exit 0 just because nobody passed --fail-on-mismatch. `resume_ok`
    # already makes `result["ok"]` False on a census_error too (see its
    # docstring), so `args.fail_on_mismatch and result["ok"] is False` below
    # WOULD also catch this -- but only when the flag is passed. This check
    # is unconditional so a caller that omits --fail-on-mismatch (the
    # nightly's ident-collision census's own --fail-on-collision precedent:
    # a finding is gated, an invalid measurement never is) still cannot get a
    # green exit code out of a run that never measured anything.
    census_error: Optional[str] = result.get("census_error")
    if census_error is not None:
        print(f"\nCENSUS COLLECTION FAILED: {census_error}", file=sys.stderr)
        return 1
    if args.fail_on_mismatch and result["ok"] is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
