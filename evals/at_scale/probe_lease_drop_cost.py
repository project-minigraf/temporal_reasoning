#!/usr/bin/env python3
"""#260 attribution: how often is the DB handle dropped, and what does it cost?

#260's fit put ~0.54 s/commit of FIXED-cost growth outside every mechanism its
own instrument could see. `_CheckpointPolicy`'s explicit checkpoints are
recorded in the trace's `ckpt_d_seconds` and account for ~10% of the rise; the
rest was unattributed.

THIS PROBE'S TARGET. `_run_ingestion` wraps every commit in `async with
db_lease_async() as db:`. `_DbLeaseManager.release()` frees the handle at
refcount 1 -> 0, and dropping a `MiniGrafDb` runs a full `do_checkpoint` inside
minigraf's `Drop for Inner` (`src/db.rs:196`) -- O(graph size) regardless of
dirty bytes (project-minigraf/minigraf#315). Two things make that invisible to
#260's trace:

  * `db_lease_async()`'s `finally` calls `release()` BEFORE `apply_s`'s timer is
    read, so the compaction is charged to `apply_s`.
  * `ckpt_d_seconds` is sourced from `_ingest_checkpoint_policy`, which never
    sees this call, so the trace reports the commit as taking no checkpoint.

That combination is exactly the residual's shape: work-independent,
graph-size-driven, growing, landing in the intercept. #260's own verdict comment
named the lease span as a candidate; this measures it.

WHAT IS MEASURED. `try_acquire` and `release` are wrapped to count 0 -> 1 opens
and time 1 -> 0 drops across a real bounded `_run_ingestion`, with
`MINIGRAF_INGEST_TRACE_PATH` set so drop time can be stated as a share of the
same `apply_s` the #260 fit was built on.

WHY `release()` IS TIMED WHOLE. At 1 -> 0 the handle is freed before `release()`
returns (see its own comment about the strong reference in that frame), so the
Rust Drop checkpoint is inside the timed span. Timing anything narrower would
miss it.

SELF-ISOLATING: creates its own tempdir and points MINIGRAF_GRAPH_PATH,
MINIGRAF_INDEX_PATH and MINIGRAF_INGEST_TRACE_PATH at it, so it never touches
memory.graph.

INSTANCE-LEVEL MONKEYPATCHING (#272): the wrappers are attached to the
`_lease_manager` INSTANCE, which #272 records as a session-wide booby trap in
the test suite. Safe here only because this is a standalone process that exits
when the run ends. Do not copy the pattern into tests.

    .venv/bin/python evals/at_scale/probe_lease_drop_cost.py \
        --repo /path/to/repo --ref <sha> [--out results/280-lease-drop-cost.json]

Run with .venv/bin/python -- bare python on the development machine carries
minigraf 1.1.1 against this project's >=1.2.3 floor (#260's methodological
warning).

PHASE SPLIT AND AUTO-CHECKPOINTS (#280). The lease-drop numbers above are a
single pooled total across the whole run, which cannot separate Stage A
(forward+reverse walk, `_ingest_progress["phase"] == "converging"`) from
Stage B (the correction sweep, `"sweeping"`) or from work outside either
(`None`, reported here as `"other"`) -- exactly the split #280's Stage A lease
window needs, since it changes Stage A's open/drop rate without touching
Stage B's. `install_wal_counters()` adds a second, independent detector:
minigraf deletes `<graph>.wal` on every checkpoint (`do_checkpoint`,
src/db.rs), and auto-checkpoint runs *inside* the transact/retract call that
crosses `wal_checkpoint_threshold` (1000 entries, one per transact/retract).
So a write after which the on-disk WAL shrank was an auto-checkpoint --
something a lease window that holds the handle open for many commits could in
principle trigger more or less often than the old per-commit lease, and
nothing else in this probe would notice it.

`PROBE_CODE_DIR` selects which checkout's `mcp_server` is measured, so a
master worktree can serve as the "A" arm (old per-commit lease) while this
probe file itself -- and its instrumentation -- stays identical across both
arms:

    PROBE_CODE_DIR=/path/to/master-worktree PYTHONHASHSEED=0 \
        .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref <sha>

Positive control for the auto-checkpoint detector -- force checkpointing to
duty near zero and the Stage B window to never close, so the run relies on
minigraf's own auto-checkpoint far more than a normal run would:

    MINIGRAF_INGEST_CHECKPOINT_DUTY=0.000001 MINIGRAF_SWEEP_YIELD_COMMITS=1000000000 \
    MINIGRAF_SWEEP_YIELD_SECONDS=1000000000 PYTHONHASHSEED=0 \
      .venv/bin/python evals/at_scale/probe_lease_drop_cost.py --repo . --ref <250th commit>

With it, `converging.auto_ckpt` must be >= 1. In the spike it was 3 out of
3940 writes. This is legitimate here (and would not be for the tests that use
these same constants) because this probe process imports `mcp_server` AFTER
setting these env vars, so the module-level constants they gate bake in the
overridden values at import time -- unlike `tests/conftest.py`'s scrub, which
cannot reach a value already baked into a constant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# PROBE_CODE_DIR selects WHICH checkout's mcp_server is measured, so a
# master worktree can be the A arm while this probe file stays the same
# (#280). Defaults to this repo.
CODE_DIR = os.environ.get("PROBE_CODE_DIR", REPO)
sys.path.insert(0, CODE_DIR)

_TMPDIR = tempfile.mkdtemp(prefix="probe260lease-")
os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(_TMPDIR, "m.graph")
os.environ["MINIGRAF_INDEX_PATH"] = os.path.join(_TMPDIR, "m.fts.sqlite3")
TRACE_PATH = os.path.join(_TMPDIR, "trace.jsonl")
os.environ["MINIGRAF_INGEST_TRACE_PATH"] = TRACE_PATH

import mcp_server as m  # noqa: E402 -- must follow the env setup above

WAL_PATH = os.environ["MINIGRAF_GRAPH_PATH"] + ".wal"
assert os.path.dirname(os.path.abspath(m.__file__)) == os.path.abspath(CODE_DIR), m.__file__

T0 = time.perf_counter()
opens: list = []   # t_since_start of each 0 -> 1 open
drops: list = []   # [t_since_start, seconds] of each 1 -> 0 drop

_lock = threading.Lock()
per_phase: dict = {}
phase_wall_s: dict = {}


def _phase() -> str:
    return m._ingest_progress.get("phase") or "other"


def _bump(phase, key, secs=None):
    with _lock:
        s = per_phase.setdefault(phase, {})
        s[key] = s.get(key, 0) + 1
        if secs is not None:
            s[key + "_s"] = s.get(key + "_s", 0.0) + secs


def _wal_size() -> int:
    try:
        return os.path.getsize(WAL_PATH)
    except OSError:
        return 0


def install_counters() -> None:
    orig_acquire = m._lease_manager.try_acquire
    orig_release = m._lease_manager.release

    def try_acquire(path=None):
        was_idle = m._lease_manager.lease_count == 0
        t = time.perf_counter()
        handle = orig_acquire(path)
        acquire_s = time.perf_counter() - t
        if handle is not None and was_idle:
            opens.append(time.perf_counter() - T0)
            _bump(_phase(), "opens", acquire_s)
        return handle

    def release():
        # Read the count -- and the phase -- BEFORE releasing: after the call
        # the count is already decremented and 1 -> 0 is indistinguishable
        # from 2 -> 1, and the phase may itself change once the handle is
        # freed and the caller resumes.
        will_drop = m._lease_manager.lease_count == 1
        phase_before = _phase() if will_drop else None
        t = time.perf_counter()
        orig_release()
        elapsed = time.perf_counter() - t
        if will_drop:
            drops.append([time.perf_counter() - T0, elapsed])
            _bump(phase_before, "drops", elapsed)

    m._lease_manager.try_acquire = try_acquire
    m._lease_manager.release = release


def install_wal_counters() -> None:
    """Auto-checkpoint detection: minigraf deletes <graph>.wal on every
    checkpoint (do_checkpoint, src/db.rs), and auto-checkpoint runs inside
    the transact that crosses wal_checkpoint_threshold (1000 entries,
    one per transact/retract). So a write after which the WAL SHRANK was
    an auto-checkpoint. Positive control: explicit checkpoints are counted
    the same way (explicit_ckpt_wal_shrank). Measured directly: forcing
    MINIGRAF_INGEST_CHECKPOINT_DUTY near zero and disabling the Stage B
    window yield (MINIGRAF_SWEEP_YIELD_COMMITS/_SECONDS huge) produced
    converging.auto_ckpt == 3 of 3940 writes on a 250-commit slice, so the
    detector is not blind."""
    orig_exec, orig_ckpt = m._db_execute, m._db_checkpoint

    def db_execute(db, datalog):
        if not (datalog.startswith("(transact") or datalog.startswith("(retract")):
            return orig_exec(db, datalog)
        p, before, t = _phase(), _wal_size(), time.perf_counter()
        out = orig_exec(db, datalog)
        el = time.perf_counter() - t
        _bump(p, "wal_writes", el)
        if _wal_size() < before:
            _bump(p, "auto_ckpt", el)
        return out

    def db_checkpoint(db):
        p, before, t = _phase(), _wal_size(), time.perf_counter()
        orig_ckpt(db)
        _bump(p, "explicit_ckpt", time.perf_counter() - t)
        if before and _wal_size() < before:
            _bump(p, "explicit_ckpt_wal_shrank")

    m._db_execute = db_execute
    m._db_checkpoint = db_checkpoint


async def _run_with_phase_watch(repo_path: str, branch: str) -> None:
    """Run _run_ingestion while a 50 ms watcher accumulates phase_wall_s.

    Each iteration attributes the elapsed time since the previous poll (or
    since start) to the phase that was current over that interval, then reads
    the phase again. The stop event is checked with the same wait, so the
    final iteration -- the one whose wait is cut short by the event -- still
    performs its accounting before the loop exits, folding in the interval
    between the last poll and _run_ingestion's actual completion.
    """
    stop = asyncio.Event()

    async def watch():
        last_phase = _phase()
        last_t = time.perf_counter()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
            now = time.perf_counter()
            with _lock:
                phase_wall_s[last_phase] = phase_wall_s.get(last_phase, 0.0) + (now - last_t)
            last_t = now
            last_phase = _phase()
            if stop.is_set():
                break

    watcher = asyncio.create_task(watch())
    try:
        await m._run_ingestion(repo_path, branch)
    finally:
        stop.set()
        await watcher


def summarize(wall: float) -> dict:
    records = []
    if os.path.exists(TRACE_PATH):
        with open(TRACE_PATH) as f:
            records = [json.loads(line) for line in f if line.strip()]

    # Snapshot the live counters under the same lock _bump() uses, and return
    # only the snapshots below -- never the module-level opens/drops/per_phase
    # objects themselves. main() takes further leases (the post-run audit,
    # _count_commit_entities) AFTER calling this function, and those leases
    # run through the very wrappers installed by install_counters()/
    # install_wal_counters(), so the live objects keep growing after this
    # call returns. Returning them by reference would make the persisted
    # JSON's opens/drops/per_phase silently drift from the handle_opens/
    # handle_drops counts computed right here and from what report() prints.
    with _lock:
        opens_snapshot = list(opens)
        drops_snapshot = list(drops)
        per_phase_snapshot = {p: dict(s) for p, s in per_phase.items()}

    n = len(records)
    apply_total = sum(r["apply_s"] for r in records)
    ckpt_total = sum(r["ckpt_d_seconds"] for r in records)
    durations = [d for _, d in drops_snapshot]
    drop_total = sum(durations)

    thirds = {}
    if len(durations) >= 3:
        k = len(durations) // 3
        first, mid, last = durations[:k], durations[k:2 * k], durations[2 * k:]
        thirds = {
            "first_mean_s": statistics.fmean(first),
            "middle_mean_s": statistics.fmean(mid),
            "last_mean_s": statistics.fmean(last),
            "growth": statistics.fmean(last) / statistics.fmean(first),
        }

    return {
        "issue": 260,
        "wall_s": wall,
        "commits_traced": n,
        "handle_opens": len(opens_snapshot),
        "handle_drops": len(drops_snapshot),
        "opens_per_commit": len(opens_snapshot) / n if n else None,
        "apply_total_s": apply_total,
        "apply_share_of_wall": apply_total / wall if wall else None,
        "explicit_ckpt_total_s": ckpt_total,
        "explicit_ckpt_share_of_apply": ckpt_total / apply_total if apply_total else None,
        "drop_total_s": drop_total,
        "drop_share_of_wall": drop_total / wall if wall else None,
        "drop_share_of_apply": drop_total / apply_total if apply_total else None,
        "drop_thirds": thirds,
        "drops": drops_snapshot,
        "opens": opens_snapshot,
        "per_phase": per_phase_snapshot,
        "code_dir": CODE_DIR,
        "phase_wall_s": dict(phase_wall_s),
    }


def report(result: dict) -> None:
    print(f"wall                 {result['wall_s']:8.1f} s")
    print(f"code dir             {result['code_dir']}")
    print(f"commits traced       {result['commits_traced']:8d}")
    print(f"handle opens         {result['handle_opens']:8d}   "
          f"({result['opens_per_commit']:.2f} per commit)")
    print(f"handle drops         {result['handle_drops']:8d}")
    print(f"sum apply_s          {result['apply_total_s']:8.1f} s  "
          f"({result['apply_share_of_wall']:.1%} of wall)")
    print(f"sum ckpt_d_seconds   {result['explicit_ckpt_total_s']:8.1f} s  "
          f"({result['explicit_ckpt_share_of_apply']:.1%} of apply_s, explicit only)")
    print(f"sum drop time        {result['drop_total_s']:8.1f} s  "
          f"({result['drop_share_of_wall']:.1%} of wall, "
          f"{result['drop_share_of_apply']:.1%} of apply_s)")
    t = result["drop_thirds"]
    if t:
        print(f"drop mean by third   {t['first_mean_s']*1000:.1f} -> "
              f"{t['middle_mean_s']*1000:.1f} -> {t['last_mean_s']*1000:.1f} ms   "
              f"growth {t['growth']:.2f}x")

    print("per-phase (opens drops drop_s writes auto_ckpt explicit_ckpt | wall_s):")
    for phase, s in sorted(result["per_phase"].items(), key=lambda kv: str(kv[0])):
        wall_for_phase = result["phase_wall_s"].get(phase, 0.0)
        print(f"  {str(phase):12s} "
              f"opens={s.get('opens', 0):5d} "
              f"drops={s.get('drops', 0):5d} "
              f"drop_s={s.get('drops_s', 0.0):8.2f} "
              f"writes={s.get('wal_writes', 0):6d} "
              f"auto_ckpt={s.get('auto_ckpt', 0):4d} "
              f"explicit_ckpt={s.get('explicit_ckpt', 0):4d} "
              f"| wall_s={wall_for_phase:8.2f}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", required=True, help="Repository whose history is ingested.")
    p.add_argument("--ref", required=True, help="Ref/SHA bounding the slice.")
    p.add_argument("--out", default=None, help="Write the result JSON here.")
    args = p.parse_args()

    install_counters()
    install_wal_counters()
    t = time.perf_counter()
    asyncio.run(_run_with_phase_watch(args.repo, args.ref))
    result = summarize(time.perf_counter() - t)

    report(result)

    # The audit takes its own lease (through handle_minigraf_query), and
    # _count_commit_entities below takes another, both AFTER `result` is
    # already built. Those leases run through the SAME install_counters()/
    # install_wal_counters() wrappers as the ingestion run -- by then
    # _ingest_progress["phase"] is back to None ("other") -- so they keep
    # appending to the live module-level opens/drops/per_phase objects. That
    # is harmless only because summarize() returned snapshots of those
    # objects rather than the objects themselves; `result` does not observe
    # this further growth.
    from evals.at_scale.fact_audit import audit_graph_against_index
    audit = audit_graph_against_index(
        os.environ["MINIGRAF_INDEX_PATH"],
        expected_graph_path=os.environ["MINIGRAF_GRAPH_PATH"],
    )
    with m.db_lease() as db:
        commit_entities = m._count_commit_entities(db)
    result["audit"] = {k: audit.get(k) for k in ("divergence", "audit_error", "graph_facts")}
    result["commit_entities"] = commit_entities
    print(f"audit                {result['audit']}")
    print(f"commit_entities      {result['commit_entities']}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.out}")
    print(f"graph dir (not cleaned up, inspect or rm): {_TMPDIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
