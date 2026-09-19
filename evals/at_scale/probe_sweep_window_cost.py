"""Cost of Stage B's bounded yield window (#222 phase 5 item C).

One real `_run_ingestion` against a synthetic linear repo, reporting Stage B's
wall clock, how many commits it swept, and how many lease WINDOWS it opened.
The window count is the number of refcount 1->0 handle drops inside the sweep,
and each of those pays minigraf's `Drop for Inner` full O(graph size)
checkpoint (#280) -- which is the entire reason the yield is a bounded window
rather than a per-commit release.

This is the instrument behind the tables in the task-7 report: the numbers
justifying `_SWEEP_YIELD_COMMITS` (25) and `_SWEEP_YIELD_PAUSE_SECONDS` (0.1)
came from here, so it lives in the repo rather than in a scratchpad. Precedent:
`probe_lease_drop_cost.py` and its siblings (#281).

WHY NOT `probe_per_commit_cost.py`. It cannot see Stage B at all. `#260`'s
trace has exactly one `_ingest_trace.emit` call site, inside Stage A's pipeline
loop immediately before `run_progress.stage_a_finished`, so
`MINIGRAF_INGEST_TRACE_PATH` records no swept commit and the trace carries no
stage/sweep field to attribute one with.

HOW TO USE IT. Run the configurations you want to compare BACK TO BACK in one
invocation batch, and compare only within that batch -- see the comparability
caveat below. The baseline for "what did the window cost?" is
`--yield-commits 1000000000`, which never closes a window and is therefore
structurally identical to the single whole-sweep lease this work replaced.
The baseline for "what did the pause cost?" is `--pause-seconds 0` at the
same `--yield-commits`, which keeps every boundary but removes the interval.

WHAT THIS DOES NOT ESTABLISH.

  * Nothing about this repo's real graph. These synthetic runs produce graphs
    of roughly 0.9-3.2 MB; the repo's own ~957-commit graph is ~179 MB, two
    orders of magnitude larger. The per-drop checkpoint cost is O(graph size)
    -- measured here at 4.4 ms on a 0.9 MB graph and 14.4 ms on a 2.9 MB one,
    a 3.3x rise for a 3.2x larger graph -- so extrapolating to 179 MB is
    arithmetic, not measurement.
  * Nothing about repositories with merges, or where the sweep is much longer
    than the forward walk. The repo built here is strictly linear and sweeps
    almost exactly half its commits at the default stream ratio.
  * Nothing across invocation batches. ABSOLUTE NUMBERS ARE NOT COMPARABLE
    BETWEEN RUNS TAKEN AT DIFFERENT TIMES. Measured: the baseline
    configuration -- one window, zero boundaries, where neither the window nor
    the pause can apply by construction -- moved from 0.321 s to 0.758 s at 60
    commits and from 1.431 s to 3.458 s at 200 commits between two rounds on
    the same machine, a ~2.4x drift with no code change between them. Any
    claim of the form "before X it was A, after X it is B" must have A and B
    from the SAME batch or it measures the machine, not the change.
  * A single sample is not a measurement at this scale. Per-run spread within
    one arm was ~0.39 s where the effect under test was ~0.4 s; one sample of
    the pause cost read +0.957 s where the mean of three read +0.433 s against
    an arithmetic prediction of 0.4 s. Repeat each arm and interleave them.

Everything runs under a `__main__` guard, and that is load-bearing rather than
style: extraction uses a SPAWN-context ProcessPoolExecutor, so each worker
re-imports this module. With the body at module level the child re-runs the
repo construction, dies, and takes the pool with it as a BrokenProcessPool
that looks like an ingestion failure.

Example, isolating the pause at the shipped window size:

    for i in 1 2 3; do for p in 0.0 0.1; do
      .venv/bin/python evals/at_scale/probe_sweep_window_cost.py \
        --commits 200 --yield-commits 25 --pause-seconds $p \
        --workdir /tmp/probe_${i}_${p}
    done; done
"""
import argparse
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time


def _build_repo(repo: pathlib.Path, n: int) -> None:
    repo.mkdir(parents=True, exist_ok=True)

    def g(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    g("init", "-b", "master")
    g("config", "user.email", "t@t.com")
    g("config", "user.name", "T")
    for i in range(n):
        (repo / "auth.py").write_text(
            f"def login():\n    return {i}\n\ndef helper_{i % 5}():\n    return {i}\n"
        )
        g("add", ".")
        g("commit", "-m", f"c{i}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commits", type=int, default=200,
                        help="Commits in the synthetic repo. Roughly half are swept.")
    parser.add_argument("--yield-commits", type=int, default=25,
                        help="_SWEEP_YIELD_COMMITS. Pass 1000000000 for the "
                             "single-whole-sweep-lease baseline.")
    parser.add_argument("--yield-seconds", type=float, default=2.0,
                        help="_SWEEP_YIELD_SECONDS.")
    parser.add_argument("--pause-seconds", type=float, default=None,
                        help="_SWEEP_YIELD_PAUSE_SECONDS. Omit to use the "
                             "shipped default; pass 0 for the no-pause arm.")
    parser.add_argument("--workdir", required=True,
                        help="Empty directory for the repo and graph.")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    # Scrubbed for the same reason tests/conftest.py scrubs them: several of
    # these are read into module constants at import, so an ambient one from
    # the developer's shell silently changes what is measured.
    for key in [k for k in os.environ if k.startswith("MINIGRAF_")]:
        del os.environ[key]

    workdir = pathlib.Path(args.workdir)
    repo = workdir / "repo"
    _build_repo(repo, args.commits)

    graph = workdir / "g.graph"
    os.environ["MINIGRAF_GRAPH_PATH"] = str(graph)

    import mcp_server

    # Patched as CONSTANTS, not via the environment: all three are read at
    # import time, which has already happened by now.
    mcp_server._SWEEP_YIELD_COMMITS = args.yield_commits
    mcp_server._SWEEP_YIELD_SECONDS = args.yield_seconds
    if args.pause_seconds is not None:
        mcp_server._SWEEP_YIELD_PAUSE_SECONDS = args.pause_seconds

    mcp_server._reset_db_state()
    mcp_server._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
        "error": None, "owner_pid": None, "error_at": None, "phase": None,
    }

    stats = {"swept": 0, "windows": 0, "sweep_start": None, "sweep_end": None}
    real_lease = mcp_server.db_lease_async
    real_apply = mcp_server._correction_sweep_apply
    real_summary = mcp_server._correction_sweep_log_summary

    def lease_spy():
        # Bracketed on the sweep's real end, never on `phase` alone: `phase`
        # stays "sweeping" through the lineage fold, _ingest_tags and the
        # final checkpoint, so a phase-only filter counts 3 leases that are
        # not window boundaries and inflates every baseline by exactly 3.
        if (mcp_server._ingest_progress.get("phase") == "sweeping"
                and stats["sweep_end"] is None):
            stats["windows"] += 1
            if stats["sweep_start"] is None:
                stats["sweep_start"] = time.monotonic()
        return real_lease()

    def apply_spy(*a, **k):
        stats["swept"] += 1
        return real_apply(*a, **k)

    def summary_spy(skipped):
        if stats["sweep_end"] is None:
            stats["sweep_end"] = time.monotonic()
        return real_summary(skipped)

    mcp_server.db_lease_async = lease_spy
    mcp_server._correction_sweep_apply = apply_spy
    mcp_server._correction_sweep_log_summary = summary_spy

    started = time.monotonic()
    asyncio.run(mcp_server._run_ingestion(str(repo), "master"))
    total = time.monotonic() - started

    mcp_server._reset_db_state()
    status = mcp_server.handle_minigraf_ingest_status()
    mcp_server._reset_db_state()

    sweep_wall = (
        stats["sweep_end"] - stats["sweep_start"]
        if stats["sweep_start"] and stats["sweep_end"] else None
    )
    print(json.dumps({
        "n_commits": args.commits,
        "yield_commits": args.yield_commits,
        "yield_seconds": args.yield_seconds,
        "pause_s": mcp_server._SWEEP_YIELD_PAUSE_SECONDS,
        "swept": stats["swept"],
        # One window == one refcount 1->0 drop == one Drop for Inner
        # checkpoint. Boundaries are windows - 1.
        "sweep_windows": stats["windows"],
        "handle_drops_in_sweep": stats["windows"],
        "sweep_wall_s": round(sweep_wall, 3) if sweep_wall else None,
        "total_run_s": round(total, 3),
        "graph_bytes": graph.stat().st_size if graph.exists() else None,
        # Carried so a degraded run cannot be misread as a cheap one.
        "status": status["status"],
        "sweep_state": status["streams"]["sweep"]["state"],
        "lineage_complete": status["lineage"]["complete"],
        "visibility": status["visibility"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
