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

STAGE B ATTRIBUTION (#372). Stage B's wall is split into named, serial,
non-overlapping components, and whatever they do not cover is reported as
`unaccounted_s` rather than silently absorbed:

  * `extract_await` -- the PARENT's wait on each Stage B extraction, from
    submission to the future resolving. This, not the worker's own `wall`,
    is the critical-path cost: it adds pool dispatch and result transfer.
    The probe's own extra `pickle.dumps` in the worker inflates it, so that
    time is recorded per call (`probe_pickle_s`) and subtracted.
  * the thread-executor write steps (`sweep_apply`, `forward_apply`,
    `through_update`, `sweep_next`, `checkpoint`, `intervals_read_extra`).
  * the WINDOW BOUNDARY: `index_commit` (the committing lease's exit),
    `release_pre_drop_ckpt` (#239's `_lineage_cache.before_drop`),
    `release_drop` (the rest of the 1 -> 0 release: minigraf's Drop
    checkpoint), `acquire_open` (the 0 -> 1 open, rules and `on_open`) and
    `pause` (the gap between one window's release and the next acquire,
    i.e. `_SWEEP_YIELD_PAUSE_SECONDS` plus scheduling).

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
    pickled = len(pickle.dumps(result))
    rec = {
        "hash": commit_hash, "t0": wall0, "wall": wall1 - wall0,
        "cpu": cpu1 - cpu0, "pickled_bytes": pickled,
        "probe_pickle_s": time.time() - wall1,
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

    # Nesting depth of timed steps. Stage B's steps all run on the single
    # write_executor thread, and some call others (_forward_apply commits the
    # index itself), so only the OUTERMOST timed call records -- otherwise a
    # nested call is counted twice and the remainder goes negative.
    depth = {"n": 0}

    def timed(name, fn):
        def spy(*a, **k):
            # Stage B only: bracketed on the sweep's own start/end marks.
            if (marks["sweep_start"] is None or marks["sweep_end"] is not None
                    or depth["n"]):
                return fn(*a, **k)
            depth["n"] += 1
            t = time.monotonic()
            try:
                return fn(*a, **k)
            finally:
                depth["n"] -= 1
                steps.setdefault(name, []).append(time.monotonic() - t)
        return spy

    real_lease = mcp_server.db_lease_async
    real_summary = mcp_server._correction_sweep_log_summary

    def lease_spy(**kw):
        if (mcp_server._ingest_progress.get("phase") == "sweeping"
                and marks["sweep_start"] is None):
            marks["sweep_start"] = time.monotonic()
        return real_lease(**kw)

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
    ]:
        setattr(mcp_server, attr, timed(name, getattr(mcp_server, attr)))
    mcp_server._extract_commit_real = mcp_server._extract_commit
    mcp_server._extract_commit = _timed_extract

    def in_sweep():
        return marks["sweep_start"] is not None and marks["sweep_end"] is None

    def add(name, dt):
        steps.setdefault(name, []).append(dt)

    # _intervals_read_extra is read once per sweep; time it with the rest.
    mcp_server._intervals_read_extra = timed(
        "intervals_read_extra", mcp_server._intervals_read_extra)

    # Gated checkpoints split by whether one actually ran.
    real_gated = mcp_server._db_checkpoint_gated

    def gated_spy(db):
        if not in_sweep() or depth["n"]:
            return real_gated(db)
        t = time.monotonic()
        ran = real_gated(db)
        add("checkpoint_ran" if ran else "checkpoint_skipped", time.monotonic() - t)
        return ran
    mcp_server._db_checkpoint_gated = gated_spy

    # Parent-side wait on each Stage B extraction future.
    import asyncio.base_events as _be
    real_rie = _be.BaseEventLoop.run_in_executor

    def rie_spy(self, executor, func, *args):
        fut = real_rie(self, executor, func, *args)
        if func is _timed_extract and in_sweep():
            t = time.monotonic()
            fut.add_done_callback(
                lambda _f, t=t: add("extract_await", time.monotonic() - t))
        return fut
    _be.BaseEventLoop.run_in_executor = rie_spy

    # Window boundary: the index commit, the release (pre-drop checkpoint
    # vs the Drop itself), the 0 -> 1 open, and the pause between windows.
    real_commit = mcp_server._commit_index_writer_safe
    mcp_server._commit_index_writer_safe = timed("index_commit", real_commit)

    lm = mcp_server._lease_manager
    cache = mcp_server._lineage_cache
    real_release, real_acquire = lm.release, lm.try_acquire
    real_before_drop = cache.before_drop
    last_release_end = {"t": None}
    pre_drop = {"dt": 0.0}

    def before_drop_spy(handle, path):
        t = time.monotonic()
        try:
            return real_before_drop(handle, path)
        finally:
            pre_drop["dt"] = time.monotonic() - t

    def release_spy():
        if not in_sweep() or lm._count != 1:
            return real_release()
        pre_drop["dt"] = 0.0
        t = time.monotonic()
        try:
            return real_release()
        finally:
            end = time.monotonic()
            add("release_pre_drop_ckpt", pre_drop["dt"])
            add("release_drop", end - t - pre_drop["dt"])
            last_release_end["t"] = end

    def acquire_spy(path=None):
        if not in_sweep() or lm._count != 0:
            return real_acquire(path)
        t = time.monotonic()
        if last_release_end["t"] is not None:
            add("pause", t - last_release_end["t"])
            last_release_end["t"] = None
        try:
            return real_acquire(path)
        finally:
            add("acquire_open", time.monotonic() - t)

    cache.before_drop = before_drop_spy
    lm.release = release_spy
    lm.try_acquire = acquire_spy

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
    b_probe_pickle = sum(r["probe_pickle_s"] for r in stage_b)
    # Serial, non-overlapping components of Stage B's wall. extract_await
    # has the probe's own extra pickle subtracted (it is not shipped cost).
    comp = {k: sum(v) for k, v in steps.items()}
    if "extract_await" in comp:
        comp["extract_await"] -= b_probe_pickle
    accounted = sum(comp.values())
    attribution = {
        "components_s": {k: round(v, 3) for k, v in
                         sorted(comp.items(), key=lambda kv: -kv[1])},
        "components_share": ({k: round(v / sweep_wall, 4) for k, v in comp.items()}
                             if sweep_wall else None),
        "accounted_s": round(accounted, 3),
        "unaccounted_s": round(sweep_wall - accounted, 3) if sweep_wall else None,
        "probe_pickle_subtracted_s": round(b_probe_pickle, 3),
        "windows": len(steps.get("acquire_open", [])),
    }
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
        "stage_b_attribution": attribution,
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
