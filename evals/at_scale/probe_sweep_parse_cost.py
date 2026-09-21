"""What does Stage B's second parse of every reverse-region commit cost? (#352)

One real `_run_ingestion` over a real repository at the shipped defaults,
with `_extract_commit` wrapped so every call records its own wall and CPU
time and the pickled size of its result. The first extraction of a hash is
Stage A's; a second extraction of the same hash is Stage B's re-parse. Stage
B's write steps are timed in the parent the same way.

WHY THE STAGE B PARSE IS ON THE CRITICAL PATH. Stage B is strictly serial:
it awaits `_extract_commit` for one commit, then its writes, then selects the
next. Nothing overlaps the parse, so the sum of Stage B's extraction wall
times is wall clock that removing the re-parse would save outright (less
whatever reading a persisted result back would cost). Stage A's parse is
pipelined over the process pool, so its sum is NOT its critical-path cost.

`pickled_bytes` is the size a sidecar holding `_extract_commit`'s output
would need per commit, before compression -- the #352 open question.

Everything runs under a `__main__` guard for the same reason as
`probe_sweep_window_cost.py`: extraction uses a SPAWN-context pool, whose
workers re-import this module. `_timed_extract` is at module level so it
pickles by reference into those workers.

    .venv/bin/python evals/at_scale/probe_sweep_parse_cost.py \\
        --repo /path/to/clone --branch master --workdir /tmp/probe352
"""
import argparse
import asyncio
import json
import os
import pathlib
import pickle
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

_LOG_ENV = "PROBE_352_EXTRACT_LOG"


def _timed_extract(repo_path, commit_hash, ignore_patterns):
    import mcp_server
    wall0, cpu0 = time.time(), time.process_time()
    result = mcp_server._extract_commit(repo_path, commit_hash, ignore_patterns)
    wall1, cpu1 = time.time(), time.process_time()
    rec = {
        "hash": commit_hash, "t0": wall0, "wall": wall1 - wall0,
        "cpu": cpu1 - cpu0, "pickled_bytes": len(pickle.dumps(result)),
        "files": len(result[0]),
    }
    with open(os.environ[_LOG_ENV], "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return result


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)

    for key in [k for k in os.environ if k.startswith("MINIGRAF_")]:
        del os.environ[key]
    workdir = pathlib.Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    graph = workdir / "g.graph"
    log = workdir / "extract.jsonl"
    log.unlink(missing_ok=True)
    os.environ["MINIGRAF_GRAPH_PATH"] = str(graph)
    os.environ[_LOG_ENV] = str(log)

    import mcp_server

    mcp_server._reset_db_state()
    mcp_server._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
        "error": None, "owner_pid": None, "error_at": None, "phase": None,
    }

    # Parent-side timing of Stage B's thread-executor steps.
    steps = {}
    marks = {"sweep_start": None, "sweep_end": None}

    def timed(name, fn):
        def spy(*a, **k):
            # Stage B only: bracketed on the sweep's own start/end marks.
            if marks["sweep_start"] is None or marks["sweep_end"] is not None:
                return fn(*a, **k)
            t = time.monotonic()
            try:
                return fn(*a, **k)
            finally:
                steps.setdefault(name, []).append(time.monotonic() - t)
        return spy

    real_lease = mcp_server.db_lease_async
    real_summary = mcp_server._correction_sweep_log_summary

    def lease_spy():
        if (mcp_server._ingest_progress.get("phase") == "sweeping"
                and marks["sweep_start"] is None):
            marks["sweep_start"] = time.monotonic()
        return real_lease()

    def summary_spy(skipped):
        if marks["sweep_end"] is None:
            marks["sweep_end"] = time.monotonic()
        return real_summary(skipped)

    mcp_server.db_lease_async = lease_spy
    mcp_server._correction_sweep_log_summary = summary_spy
    for name, attr in [
        ("sweep_apply", "_correction_sweep_apply"),
        ("forward_apply", "_forward_apply"),
        ("through_update", "_correction_sweep_through_update"),
        ("sweep_next", "_correction_sweep_next"),
        ("checkpoint", "_db_checkpoint_gated"),
    ]:
        setattr(mcp_server, attr, timed(name, getattr(mcp_server, attr)))
    mcp_server._extract_commit_real = mcp_server._extract_commit
    mcp_server._extract_commit = _timed_extract

    started = time.monotonic()
    asyncio.run(mcp_server._run_ingestion(args.repo, args.branch))
    total = time.monotonic() - started

    mcp_server._reset_db_state()
    status = mcp_server.handle_minigraf_ingest_status()
    mcp_server._reset_db_state()

    recs = [json.loads(line) for line in log.read_text().splitlines()]
    seen, stage_a, stage_b = set(), [], []
    for r in sorted(recs, key=lambda r: r["t0"]):
        (stage_b if r["hash"] in seen else stage_a).append(r)
        seen.add(r["hash"])

    def summ(rs, key):
        xs = [r[key] for r in rs]
        return {
            "n": len(xs), "sum": round(sum(xs), 3),
            "mean": round(sum(xs) / len(xs), 5) if xs else None,
            "p50": _pct(xs, 0.5), "p99": _pct(xs, 0.99), "max": max(xs) if xs else None,
        }

    sweep_wall = (marks["sweep_end"] - marks["sweep_start"]
                  if marks["sweep_start"] and marks["sweep_end"] else None)
    b_parse = sum(r["wall"] for r in stage_b)
    out = {
        "repo": args.repo, "branch": args.branch,
        "total_run_s": round(total, 3),
        "stage_b_wall_s": round(sweep_wall, 3) if sweep_wall else None,
        "stage_b_parse_wall_s": round(b_parse, 3),
        "stage_b_parse_share_of_stage_b": round(b_parse / sweep_wall, 4) if sweep_wall else None,
        "stage_b_parse_share_of_run": round(b_parse / total, 4),
        "stage_a_extract": {"wall": summ(stage_a, "wall"), "cpu": summ(stage_a, "cpu")},
        "stage_b_extract": {"wall": summ(stage_b, "wall"), "cpu": summ(stage_b, "cpu")},
        "stage_b_steps": {k: {"n": len(v), "sum": round(sum(v), 3)} for k, v in steps.items()},
        "pickled_bytes_all_commits": summ(stage_a, "pickled_bytes"),
        "pickled_bytes_reverse_region": summ(stage_b, "pickled_bytes"),
        "graph_bytes": graph.stat().st_size if graph.exists() else None,
        "status": status["status"],
        "sweep_state": status["streams"]["sweep"]["state"],
        "lineage_complete": status["lineage"]["complete"],
    }
    (workdir / "result.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
