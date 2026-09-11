# evals/at_scale/run_ingestion_benchmark.py
"""In-process ingestion-performance benchmark harness (#120, Part A).

Drives mcp_server.py's real handlers directly -- no subprocess, no MCP
stdio transport, no LLM -- following this project's real-backend testing
convention (docs/testing-conventions.md) applied to a standalone script
instead of a pytest fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import multiprocessing.resource_tracker
import os
import resource
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale.commit_census import (  # noqa: E402
    collect_commit_census,
    walk_claimed_from_progress,
)
from evals.at_scale.fact_audit import audit_graph_against_index  # noqa: E402
from evals.at_scale.metrics import latency_stats, throughput_per_minute  # noqa: E402
from evals.at_scale.stderr_capture import (  # noqa: E402
    TeeStderrFailure,
    scan_ingestion_stderr,
    tee_stderr,
)

_STATUS_QUERY = "[:find (count ?e) :where [?e :entity-type :type/commit]]"


async def _poll_during_ingestion(
    ingest_task: "asyncio.Task[None]",
    poll_interval: float,
    duty_factor: float = 10.0,
) -> tuple[list[float], list[float], list[float]]:
    """Poll ingest_status and a graph query while ingest_task runs.

    Returns (status_latencies, query_latencies, poll_offsets); latencies in
    seconds, offsets in seconds since polling began.

    #242: both halves of this are load-bearing.

    The handlers run on a dedicated single-worker executor rather than inline,
    because handle_minigraf_query is synchronous -- calling it on the event
    loop stalls the _run_ingestion coroutine, the process-pool result
    collection, and every write_executor completion. Two full-history runs
    were killed at 3h54m and 36m on code measured at +3% before this was
    understood.

    Off-loop alone is NOT sufficient. handle_minigraf_query acquires
    _db_native_lock (mcp_server._db_execute), and _STATUS_QUERY counts every
    :type/commit entity, so its cost grows for the whole run -- a ~1s scan
    every 0.5s would still serialize against every ingestion write from
    another thread. The adaptive interval is what bounds the instrument's
    share of that lock: at duty_factor=10 the poller holds it for at most
    ~9% of the run however large the scan grows.

    _STATUS_QUERY is deliberately unchanged, so the QUERY being timed is the
    same one every entry already in benchmark.md timed. That is NOT the same
    claim as "the recorded latency series stays comparable", which an earlier
    version of this docstring made and which is false. The adaptive interval
    sleeps in proportion to the poll's own cost, so it undersamples exactly
    the late, expensive polls -- and _STATUS_QUERY's cost grows monotonically
    through a run, because it counts every :type/commit entity. Pre-fix
    entries polled every 0.5s regardless and sampled that expensive tail at
    full density. So query_latency p50/p99 recorded here are biased LOW
    against every pre-fix entry, in the opposite direction from the pre-fix
    wall-clock inflation. benchmark.md's 2026-08-07 note says both halves.

    A dedicated executor, rather than the loop default, keeps the poll off any
    thread the ingestion may want.

    Known limitation: a cancelled run still waits on an in-flight poll thread
    at executor shutdown. The previous implementation blocked the event loop
    outright, so this is strictly better, but it is not zero.
    """
    import mcp_server

    loop = asyncio.get_running_loop()
    status_latencies: list[float] = []
    query_latencies: list[float] = []
    poll_offsets: list[float] = []
    started = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="bench-poll"
    ) as poll_executor:
        while not ingest_task.done():
            poll_offsets.append(time.perf_counter() - started)

            t0 = time.perf_counter()
            await loop.run_in_executor(
                poll_executor, mcp_server.handle_minigraf_ingest_status
            )
            status_duration = time.perf_counter() - t0
            status_latencies.append(status_duration)

            t0 = time.perf_counter()
            await loop.run_in_executor(
                poll_executor, mcp_server.handle_minigraf_query, _STATUS_QUERY
            )
            query_duration = time.perf_counter() - t0
            query_latencies.append(query_duration)

            await asyncio.sleep(
                max(poll_interval, duty_factor * (status_duration + query_duration))
            )

    return status_latencies, query_latencies, poll_offsets


async def run_ingestion_benchmark(
    repo_path: str,
    branch: Optional[str],
    graph_path: Path,
    poll_interval: float = 0.5,
    duty_factor: float = 10.0,
    compare_ignore: bool = False,
    trace_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Run a full git ingestion against repo_path into an isolated graph at
    graph_path, measuring wall-clock, throughput, peak RSS, final graph/index
    size, and MCP responsiveness (status/query latency) while ingestion runs.

    graph_path must not already exist -- each call is a fresh, isolated run.

    trace_path, if given, arms mcp_server's per-commit cost trace (#260) via
    MINIGRAF_INGEST_TRACE_PATH for the duration of this call only, and is
    reported back as metrics["trace_path"]. Absent from metrics means the run
    was NOT traced -- never "traced and empty".
    """
    import fact_index
    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph_path))
    mcp_server._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }

    resolved_branch = branch or mcp_server._default_git_branch(repo_path)

    # Start the spawn-context resource_tracker BEFORE arming the tee (#256).
    # _run_ingestion builds a spawn ProcessPoolExecutor, and multiprocessing
    # starts the tracker lazily on that pool's first use -- i.e. INSIDE the
    # `with` below, where it would inherit the tee pipe's write end as its
    # fd 2 and hold that duplicate open for the parent's entire remaining
    # lifetime (the same hazard tee_stderr's docstring documents for its own
    # shutdown). Starting it here makes it inherit the real fd 2, which also
    # means its "leaked semaphore objects" warning -- a genuine signal for a
    # spawn-heavy 25-minute run -- reaches the real log instead of a pipe
    # that stops being drained at teardown.
    multiprocessing.resource_tracker.ensure_running()

    # Pre-bound, because the tee's teardown can raise on a path where the body
    # never finished. tee_stderr() raises TeeStderrFailure from its own
    # `finally`, which DISPLACES whatever the body was already raising
    # (verified: a RuntimeError inside the `with` comes out as
    # TeeStderrFailure, with the real cause reachable only via __context__).
    # The handler below still has to build a full metrics dict in that case.
    status_latencies: list[float] = []
    query_latencies: list[float] = []
    poll_offsets: list[float] = []
    captured = None
    tee_failure: Optional[TeeStderrFailure] = None

    # Armed only around THIS drive of _run_ingestion, and disarmed again
    # before returning to the caller -- never left set for the rest of the
    # process, and (critically) restored before the compare_ignore branch
    # below gets a chance to run its own, separate _run_ingestion (#260).
    # See that branch's comment for why it must stay untraced.
    _prior_trace_env = os.environ.get("MINIGRAF_INGEST_TRACE_PATH")
    if trace_path is not None:
        os.environ["MINIGRAF_INGEST_TRACE_PATH"] = str(Path(trace_path).resolve())
    start = time.perf_counter()
    try:
        # The tee spans the WHOLE run on purpose. Narrowing its scope to dodge
        # the teardown raise would also narrow what it can see, and a commit
        # dropped outside the narrowed window is exactly the event #256 exists
        # to catch.
        with tee_stderr() as captured:
            ingest_task = asyncio.create_task(
                mcp_server._run_ingestion(repo_path, resolved_branch)
            )
            try:
                status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
                    ingest_task, poll_interval, duty_factor
                )
                await ingest_task
            except BaseException:
                if not ingest_task.done():
                    ingest_task.cancel()
                    try:
                        await ingest_task
                    except (asyncio.CancelledError, Exception):
                        pass
                raise
    except TeeStderrFailure as exc:
        # Caught deliberately, and deliberately not re-raised. The raise
        # happens in tee_stderr()'s teardown, AFTER _run_ingestion has
        # returned, so _ingest_progress, the graph/index sizes and the latency
        # lists are all still readable -- letting it propagate would destroy a
        # ~25-minute run's entire record (no metrics JSON, no report row) to
        # report a failure of the instrument. It is recorded in the metrics
        # instead, and _exit_code() turns it into exit 1: catching it without
        # that clause would convert this failure back into a green run, which
        # is the precise fail-open #256 exists to close.
        tee_failure = exc
        # Print __context__ too, not just the TeeStderrFailure. The teardown
        # raise displaces any in-flight body exception, so a genuine ingestion
        # crash is reachable ONLY through the chain; a handler that reported
        # the tee failure alone would hide it. print_exception walks the chain
        # by default.
        #
        # Both writes are best-effort, and the suppression is NOT paranoia
        # (#256 fix round 1, Important 2). fd 2 is usually the caller's real
        # stderr again by the time this runs -- but not on the one path that
        # matters most: when guard.restore()'s dup2 is itself what failed,
        # fd 2 still points at the tee pipe, whose read end teardown then
        # closes, so writing to it raises BrokenPipeError. Unguarded, that
        # exception propagates out of this except clause and past the finally
        # below, destroying the metrics dict this handler exists to preserve
        # -- reproduced on 2 of 3 runs (racy with the pump's emergency
        # valve). Separate suppress blocks, not one: a broken first write
        # must not skip the traceback, which is the more informative of the
        # two. Nothing is lost if both fail; the same information is in the
        # returned metrics as tee_failure/tee_failure_context.
        with contextlib.suppress(BaseException):
            print(
                "[run_ingestion_benchmark] stderr capture did not complete; "
                "skipped_commits/error_signals below are LOWER BOUNDS:",
                file=sys.stderr,
            )
        with contextlib.suppress(BaseException):
            traceback.print_exception(exc)
    finally:
        wall_clock = time.perf_counter() - start
        if trace_path is not None:
            if _prior_trace_env is None:
                os.environ.pop("MINIGRAF_INGEST_TRACE_PATH", None)
            else:
                os.environ["MINIGRAF_INGEST_TRACE_PATH"] = _prior_trace_env

    scanned = scan_ingestion_stderr(captured.text() if captured is not None else "")

    poll_seconds = sum(status_latencies) + sum(query_latencies)
    poll_duty_fraction = (poll_seconds / wall_clock) if wall_clock > 0 else 0.0

    commits_ingested = walk_claimed_from_progress(mcp_server._ingest_progress)
    final_status = mcp_server._ingest_progress["status"]
    # Published by _run_ingestion's two policy-clearing finally blocks just
    # before _ingest_checkpoint_policy is discarded (#241 Task 6) -- the
    # policy itself does not survive the run, so this dict is the only
    # surviving source for realised checkpoint duty.
    checkpoint_summary = mcp_server._ingest_progress.get("checkpoint_summary")
    # #270. _run_ingestion catches its own exceptions, records them in
    # _ingest_progress["error"] and returns normally WITHOUT printing them, so
    # this dict is the only place the text of a failed run's exception exists:
    # it reaches neither this harness's stderr tee (nothing was written to fd
    # 2, so scan_ingestion_stderr finds no error_signals) nor the caller.
    # final_status alone says "error" without saying of what.
    #
    # It matters most for the failures early in the run -- the linearization,
    # the preload, `git rev-list`. Those used to publish no checkpoint_summary
    # either (both publishing `finally` blocks are guarded on the
    # _CheckpointPolicy being non-None, and it was constructed ~100 lines into
    # the try), so a metrics JSON from a run that died there carried no trace
    # of the cause but final_status. #270 moved the construction to the first
    # statement in the try, so such a run now carries an all-zeros summary --
    # which is an improvement in schema stability and NOT a second source for
    # the cause: zeros is also what a legitimate run that checkpointed nothing
    # reports. This key stays the only place the text lives. Absent from a
    # metrics file written by an older harness; None (not absent) on a clean
    # run, so a reader can tell "measured, no error" from "not measured".
    ingest_error = mcp_server._ingest_progress.get("error")
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    index_path = fact_index.index_path_for(str(graph_path))
    graph_size_bytes = os.path.getsize(graph_path) if graph_path.exists() else 0
    index_size_bytes = os.path.getsize(index_path) if os.path.exists(index_path) else 0

    # #302. Everything above this line is stderr-derived, and a graph that
    # silently loses facts prints nothing: #302 measured ~11% of a graph
    # vanishing with zero bytes on stderr and zero error_signals. This is the
    # only check here that reads the graph's CONTENT rather than its logs.
    #
    # Runs here, before the compare_ignore branch below rebinds the module to
    # a second graph, and after _run_ingestion's finally has committed and
    # closed the batched index writer -- an audit against a half-written index
    # would report the harness's own timing as divergence. Also deliberately
    # AFTER peak_rss_kb is sampled: the scan materialises the fact set, and
    # that is the instrument's memory, not the ingestion's.
    fact_audit = audit_graph_against_index(
        index_path, expected_graph_path=str(graph_path)
    )

    # #317. BESIDE the audit, not inside it. Every check above this line is
    # fact-level, and a commit that never reached the graph at all is invisible
    # to all of them: fact_audit's two witnesses are written from the same
    # triples in the same transaction boundary, so a commit that was never
    # written is absent from BOTH and they agree perfectly. This is the one
    # check here that holds a reference the graph did not produce -- the repo
    # itself -- which is also why it cannot ride fact_audit's scan the way the
    # two :introduced-by checks do. fact_audit deliberately takes no repo
    # handle, and giving it one for this would cost that signature its honesty.
    #
    # `resolved_branch`, never "HEAD": those are different refs whenever the
    # checkout is not on the branch being walked, which is the ordinary case
    # here (see mcp_server._run_ingestion's repo_total, fixed in the same
    # change). Two git subprocesses and one point query, so it costs
    # milliseconds and needs no placement caveat of its own beyond running
    # before the compare_ignore branch rebinds the module to a second graph.
    #
    # It counts whatever mcp_server is CURRENTLY BOUND TO, which is not visible
    # in its arguments -- the same exposure fact_audit takes an
    # expected_graph_path to close. It does not repeat that guard because the
    # audit immediately above already made the assertion for this graph: a
    # mismatch there is recorded as an audit_error and fails clause 5, so a
    # census of the wrong graph cannot reach a green run. Moving this call
    # above the audit, or reusing it anywhere the audit does not precede it,
    # breaks that and needs its own check.
    async with mcp_server.db_lease_async() as census_db:
        commit_census = collect_commit_census(
            repo_path=repo_path,
            ref=resolved_branch,
            walk_claimed=commits_ingested,
            db=census_db,
            final_status=final_status,
        )

    from evals.at_scale.report import runtime_versions

    versions = runtime_versions()
    result = {
        "repo_path": repo_path,
        "branch": resolved_branch,
        # #284 item 4: a wall-clock number is not comparable without the
        # minigraf version that produced it.
        "minigraf_version": versions["minigraf"],
        "python_version": versions["python"],
        # #256. Recorded so the pairing of a persisted graph to this metrics
        # file is auditable from the metrics side too: the probe recording the
        # path it was HANDED says nothing about whether that was the right one.
        "graph_path": str(Path(graph_path).resolve()),
        **({"trace_path": str(Path(trace_path).resolve())} if trace_path is not None else {}),
        "commits_ingested": commits_ingested,
        "wall_clock_seconds": wall_clock,
        "throughput_per_minute": throughput_per_minute(commits_ingested, wall_clock),
        "peak_rss_kb": peak_rss_kb,
        "graph_size_bytes": graph_size_bytes,
        "index_size_bytes": index_size_bytes,
        "status_latency": latency_stats(status_latencies),
        "query_latency": latency_stats(query_latencies),
        "final_status": final_status,
        "poll_count": len(poll_offsets),
        "poll_duty_fraction": poll_duty_fraction,
        "poll_offsets": poll_offsets,
        "checkpoint_summary": checkpoint_summary,
        "ingest_error": ingest_error,
        # #302. divergence == 0 is the pass; see fact_audit.py for why it is
        # exactly zero on a clean run rather than "small".
        "fact_audit": fact_audit,
        # #317. Its own key, beside the audit rather than inside it -- a
        # different witness (the repo), a different failure mode (a whole
        # commit missing, not a fact within one). `ok` is the gate's read; the
        # three raw counts and three deltas ship alongside it so a mismatch
        # says WHERE it happened.
        "commit_census": commit_census,
        # #256. Not derivable from commits_ingested or final_status: the
        # per-commit handler isolates failures and increments `processed`
        # anyway, so both are blind to a dropped commit.
        **scanned,
        # Whether the capture those four keys were scanned from ran to
        # completion. It matters independently of `tee_failure`: on False,
        # skipped_commits and error_signals are LOWER BOUNDS, and a downstream
        # reader must not read an empty list as proof of a clean run.
        "stderr_capture_complete": tee_failure is None,
    }
    if tee_failure is not None:
        result["tee_failure"] = repr(tee_failure)
        if tee_failure.__context__ is not None:
            # The body exception the teardown raise displaced. Kept in the
            # metrics, not only in the log, because the JSON is the artifact
            # that survives the run.
            result["tee_failure_context"] = repr(tee_failure.__context__)

    if compare_ignore:
        # Deliberately NOT tee'd (pre-existing, #256 fix round 1). This second
        # ingestion exists only to size a graph built with the ignore patterns
        # disabled; its stderr is not part of the measured run, and folding it
        # into `scanned` would attribute its skips to the run reported above.
        # Deliberately NOT traced either, for the same reason (#260): the
        # env var was already restored in the finally above, so this run is
        # untraced whether or not trace_path was given. Tracing it too would
        # append a second ingestion's records into the same JSONL file with
        # nothing marking where one run ended and the other began, corrupting
        # the per-commit regression the trace exists to feed.
        no_ignore_graph_path = graph_path.parent / f"{graph_path.stem}-no-ignore{graph_path.suffix}"
        original_patterns = mcp_server._DEFAULT_IGNORE_PATTERNS
        mcp_server._DEFAULT_IGNORE_PATTERNS = ()
        try:
            mcp_server._reset_db_state()
            mcp_server.open_db(str(no_ignore_graph_path))
            mcp_server._ingest_progress = {
                "status": "idle", "total": 0, "prior_ingested": 0,
                "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
            }
            await mcp_server._run_ingestion(repo_path, resolved_branch)
        finally:
            mcp_server._DEFAULT_IGNORE_PATTERNS = original_patterns

        without_ignore_size = (
            os.path.getsize(no_ignore_graph_path) if no_ignore_graph_path.exists() else 0
        )
        result["ignore_comparison"] = {
            "with_ignore_graph_size_bytes": graph_size_bytes,
            "without_ignore_graph_size_bytes": without_ignore_size,
            "delta_bytes": without_ignore_size - graph_size_bytes,
        }

    return result


def _exit_code(metrics: dict[str, Any]) -> int:
    """Return 1 if ingestion ended in an error state, dropped any commit,
    logged a #251 signature, diverged from its own fact index, lost a whole
    commit against the repo, or could not be verified at all; else 0.

    Clauses 2 and 3 matter because `final_status` cannot see either --
    _run_ingestion isolates per-commit failures by design rather than
    propagating them, so a run that skipped commits still reports "complete".

    Clause 4 is what keeps the verification from failing open. Catching
    TeeStderrFailure in run_ingestion_benchmark (so a tee failure does not
    destroy a 25-minute run's metrics) would otherwise turn an unverifiable
    run into a green one: with an incomplete capture, `skipped_commits ==
    []` means "nothing was seen", not "nothing happened".

    Clauses 5, 6 and 7 are the only ones that read the graph's CONTENT. The four
    above it are all derived from what the run PRINTED, and a graph that
    silently drops facts prints nothing -- measured, ~11% of a graph gone with
    every stderr pattern reading clean. It gates on `divergence` with NO
    tolerance because a clean audit diverges by exactly zero (fact_audit.py);
    it also fails on `audit_error`, because an audit that could not run is
    unverified, not verified-clean -- the same reasoning as clause 4.

    Every key is read with .get() so pre-#256 metrics files, which carry none
    of them, still evaluate. `is False` rather than `not ...` for
    stderr_capture_complete, so an ABSENT key (old file) stays clean while an
    explicit False fails. An absent `fact_audit` is treated the same way, and
    for the same reason: an old metrics file cannot be retro-audited.
    """
    if metrics.get("final_status") == "error":
        return 1
    if metrics.get("skipped_commits") or metrics.get("error_signals"):
        return 1
    if metrics.get("tee_failure") or metrics.get("stderr_capture_complete") is False:
        return 1
    audit = metrics.get("fact_audit")
    if audit and (audit.get("divergence") or audit.get("audit_error")):
        return 1
    # Clause 6 (#287) reads the same graph as clause 5 and asks a different
    # question, which is why it is a separate clause rather than another term
    # in `divergence`: a two-value :introduced-by reaches the fact index
    # faithfully too, so the two witnesses agree perfectly and clause 5 passes
    # on a graph that must be thrown away. Gated with NO tolerance on the
    # strength of a measurement, not by symmetry: the 831-commit at-scale
    # graph carries 3150 :introduced-by facts across 3150 entities, every one
    # holding exactly one. `.get("entities")` so a metrics file from a harness
    # that had the fact audit but not this check -- outer key present, inner
    # key absent -- still evaluates clean, exactly as an absent fact_audit
    # does. `None` (the audit could not scan) is already failed by
    # audit_error above, which is the only way this key becomes None.
    if audit and (audit.get("introduced_by_duplicates") or {}).get("entities"):
        return 1
    # Clause 7 (#316) is clause 6's opposite defect and needs its own clause
    # for the same reason: a live code entity holding NO :introduced-by is
    # missing from the fact index in exactly the same way it is missing from
    # the graph, so clause 5's two witnesses agree perfectly and clause 6 --
    # which skips anything with fewer than two values -- steps straight over
    # it. #313's runs reported `status: complete` with zero bytes on stderr,
    # so clauses 1-4 were clean too. Gated with NO tolerance, on the same
    # terms as clause 6: a healthy graph is zero, and the check reports its
    # own `code_entities_scanned` denominator so a run cannot read clean by
    # having scanned nothing. `.get("entities")` so an older metrics file --
    # fact audit present, this key absent -- still evaluates clean. `None`
    # (the audit could not scan) is already failed by audit_error above.
    if audit and (audit.get("entities_without_introduced_by") or {}).get("entities"):
        return 1
    # The audit's own blind spot, closed with the one reference it does not
    # have: a fact absent from BOTH witnesses is invisible to a comparison of
    # the two, and two empty witnesses agree perfectly. `commits_ingested` is
    # an independent count, so a run that processed commits and produced a
    # graph reading back completely empty is a failure however cleanly the
    # two sides matched. Guarded on commits_ingested being truthy, so a
    # zero-commit run is not failed for having no facts.
    if metrics.get("commits_ingested") and audit and audit.get("graph_facts") == 0:
        return 1
    # Clause 8 (#317) is the generalisation of the clause immediately above.
    # That one catches a graph that reads back COMPLETELY empty; this one
    # catches ONE commit missing out of 847, which is not zero facts and which
    # nothing else here can see. Every clause above is either stderr-derived
    # (1-4) or a comparison between the graph and its own index (5-7), and a
    # commit that was never written is absent from both witnesses equally --
    # they agree perfectly about a graph that lost history.
    #
    # Gated on `ok`, which commit_census computes, rather than on the deltas
    # directly: the deltas are not all believable on every run. A `stopped`
    # run walked fewer commits than the repo holds BY DESIGN, so
    # `repo_vs_walk` is gated only when final_status is "complete", while
    # `walk_vs_graph` is gated always -- however few commits the walk claimed,
    # the graph must hold that many. Putting that reasoning in the census
    # keeps it next to the numbers it is about.
    #
    # NO TOLERANCE, on the strength of a measurement rather than by symmetry
    # with its neighbours: the 847-commit at-scale graph reads 847 = 847 = 847,
    # every delta exactly zero (results/317-commit-census.json). The census
    # ships `repo_commits` as its own denominator, so a run cannot read clean
    # by having counted nothing -- and a census over a repo with no commits
    # reports `proved_nothing` and is NOT failed, since a repo holding no
    # commits is not a defect. `is False` rather than `not ...`, matching
    # stderr_capture_complete: an ABSENT key (a metrics file from a harness
    # that predates this census) stays clean, because a run that was never
    # asked cannot be retro-failed.
    if (metrics.get("commit_census") or {}).get("ok") is False:
        return 1
    return 0


@contextlib.contextmanager
def resolve_graph_path(graph_path_arg: Optional[str]):
    """Yield the graph path for one run.

    Without --graph-path this is a TemporaryDirectory, exactly as before
    (#120) -- the recurring benchmark's behaviour must not change. With it,
    the graph persists so a probe can query it after the run (#256); #251's
    occurrences were never inspectable precisely because the graph was
    already deleted.

    Refuses an existing path -- and, as of the #256 review round, its
    sidecars too. run_ingestion_benchmark's own docstring states the
    no-preexisting-path precondition, and CLAUDE.md's standing rule is that
    graphs are rebuilt into a fresh path, never re-ingested in place --
    re-running over an existing file repairs nothing and silently doubles
    the history. Checking only the main graph file missed that minigraf also
    writes `<path>.wal` and a fact index (default `<path>.fts.sqlite3`, or
    wherever MINIGRAF_INDEX_PATH points): a crashed run leaves those behind
    deliberately, since post-mortem inspection is the point of --graph-path,
    but minigraf's open() replays a leftover .wal automatically -- so
    deleting only the main file and re-running silently resurrects the dead
    run's writes into what looks like a fresh graph. The `.lock` file is
    deliberately NOT checked here: it self-heals via minigraf's stale-PID
    check, so a stale one does not cause silent corruption the way a stale
    .wal or index does. The fact index's own SQLite sidecars (`-wal`, `-shm`)
    were considered and ruled out for the same reason: SQLite discards an
    orphaned WAL once the main index file is gone, and the main index file IS
    checked here, so a leftover sidecar cannot resurrect anything on its own.
    """
    if graph_path_arg is None:
        with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-") as tmpdir:
            yield Path(tmpdir) / "bench.graph"
        return

    import fact_index

    path = Path(graph_path_arg)
    wal_path = Path(f"{path}.wal")
    index_path = Path(fact_index.index_path_for(str(path)))
    existing = [p for p in (path, wal_path, index_path) if p.exists()]
    if existing:
        named = ", ".join(str(p) for p in existing)
        raise SystemExit(
            f"--graph-path {path} already exists (found: {named}). Each run "
            f"needs a fully fresh graph -- re-ingesting into an existing one "
            f"is never correct, and minigraf replays a leftover .wal "
            f"automatically, so deleting only the main file is not enough. "
            f"Remove all of the listed paths, or pick a new --graph-path."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the at-scale ingestion benchmark (#120).")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument(
        "--branch", default=None,
        help="Branch or commit to walk. Omit to resolve via "
             "mcp_server._default_git_branch (main, then master). Never "
             "'HEAD': it defeats that resolution and is refused (#330).",
    )
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--poll-duty-factor", type=float, default=10.0,
        help="Sleep max(poll_interval, N * last_poll_duration) between polls, "
             "bounding the instrument's share of _db_native_lock (#242).",
    )
    parser.add_argument("--compare-ignore", action="store_true")
    parser.add_argument(
        "--trace-path", default=None,
        help="Append a per-commit cost trace (JSONL) here (#260). Read it with "
             "evals/at_scale/probe_per_commit_cost.py. Off by default.",
    )
    parser.add_argument(
        "--graph-path", default=None,
        help="Persist the graph at this path instead of using a temporary "
             "directory. Must not already exist. Required for the #256 "
             "provisional-residue probe, which queries the surviving graph.",
    )
    args = parser.parse_args()

    # #330. "HEAD" is the one value --branch must never receive. It is truthy,
    # so it does not select the checked-out ref *in addition to* the
    # resolution run_ingestion_benchmark performs -- it DEFEATS
    # `branch or _default_git_branch(repo_path)` entirely, and the literal
    # propagates to _git_commits' range spec, _run_ingestion's repo_total and
    # #317's commit census alike. The failure is quiet rather than loud:
    # everything downstream inherits the SAME wrong ref, so no count in the
    # tier disagrees with another; they simply all describe whatever happens
    # to be checked out instead of the branch being tracked. #317 caught the
    # identical shape in _run_ingestion only because the harness resolved
    # "master" while the checkout sat on a feature branch, making two numbers
    # that should have matched differ. The at-scale nightly passed this
    # literal for months and nothing went red, because it runs on a checkout
    # of the default branch where the two refs denote the same commit.
    #
    # Refused HERE, on the argument, and deliberately NOT downstream of the
    # `or`: _default_git_branch legitimately RETURNS "HEAD" as its documented
    # last-resort fallback when neither main nor master resolves, and
    # run_ingestion_benchmark(repo, "HEAD", ...) is the correct call for a
    # throwaway fixture repo with no branch name worth resolving (most tests
    # in tests/test_at_scale_ingestion_benchmark.py drive it that way). This
    # guards a human writing an argument, which is where the defect was.
    if args.branch == "HEAD":
        raise SystemExit(
            "--branch HEAD is refused (#330). HEAD is truthy, so it does not "
            "resolve to the tracked branch -- it defeats the resolution this "
            "harness performs and propagates the literal to every ref "
            "downstream, quietly, because they all inherit it and stay "
            "consistent with each other. Omit --branch entirely to let "
            "mcp_server._default_git_branch resolve it (this is what the "
            "at-scale nightly does), or pass an explicit branch name or "
            "commit sha if you mean to walk something else."
        )

    with resolve_graph_path(args.graph_path) as graph_path:
        metrics = asyncio.run(
            run_ingestion_benchmark(
                args.repo_path,
                args.branch,
                graph_path,
                poll_interval=args.poll_interval,
                duty_factor=args.poll_duty_factor,
                compare_ignore=args.compare_ignore,
                trace_path=Path(args.trace_path) if args.trace_path else None,
            )
        )

    from evals.at_scale.report import append_ingestion_report, write_json_result

    results_dir = REPO_ROOT / "evals" / "at_scale" / "results"
    report_path = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
    json_path = write_json_result(metrics, results_dir)
    append_ingestion_report(metrics, report_path, json_path)
    print(json.dumps(metrics, indent=2))
    print(f"\nWrote {json_path}")
    print(f"Appended to {report_path}")
    return _exit_code(metrics)


if __name__ == "__main__":
    sys.exit(main())
