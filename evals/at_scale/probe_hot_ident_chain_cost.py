#!/usr/bin/env python3
"""#260 attribution: does the per-commit FIXED tail grow with hot-ident chain depth?

#260 (PR #279) measured that ingestion's per-commit cost grows with history
length even after controlling for work size: fitting `apply_s = a + b*W` per
third of processed order gives a 0.158 -> 0.757 s/commit (4.81x). Checkpointing
explains only 10.1% of that rise, so ~0.54 s/commit of FIXED-cost growth is
unattributed. The sharpest evidence is model-free: 260 commits with W == 0
(merge commits taking no checkpoint) have non-overlapping cost distributions
across thirds, medians 0.209 -> 0.967 s. Whatever grows is not work-driven.

THE HYPOTHESIS THIS PROBE TESTS. _forward_apply's tail (mcp_server.py, the
`if not lifecycle_only:` blocks) runs three updates on THREE FIXED IDENTS on
every single commit, W == 0 included:

    _watermark_update                  -> :ingestion/watermark
    _frontier_persist_claim(low)       -> :ingestion/frontier-low :hi-hash
    _lineage_confirmed_through_update  -> :ingestion/lineage-confirmed-through

Each is read-then-retract-then-reassert on ONE entity. minigraf keeps retracted
facts as history, so after N commits each of those (entity, attribute) pairs
carries an N-deep version chain. If any of the read / retract / transact paths
walks that chain, per-commit cost is O(N) -- a fixed cost, growing linearly in
commits processed, paid identically by a merge commit that touches no files.
That matches every measured property of #260's residual.

WHAT WOULD REFUTE IT: flat per-cycle cost as chain depth grows. Then the fixed
tail is not the mechanism and the remaining candidates from #260's verdict
comment (the DB lease acquire/drop inside apply_s, Stage B's correction sweep)
take over.

THREE AXES, because "grows with depth" alone is confounded.

  A  DEPTH.    Filler held constant, DEPTH_CYCLES cycles of the real
               per-commit tail. Growth = mean(last decile)/mean(first decile).
  B  SIZE.     Chain depth held constant (SIZE_CYCLES, fresh chain each time),
               filler swept 100k -> 5M. Separates "the chain got deep" from
               "the graph got big" -- they are perfectly confounded during a
               real ingestion and only an independent sweep can tell them
               apart. This is the same two-axis design
               bench_introduced_by_query_cost.py used to exonerate #239's
               point queries.
  C  WAL.      Axis A again, but calling db.checkpoint() every cycle (the
               checkpoint itself EXCLUDED from the timing). A no-checkpoint
               loop lets the WAL grow without bound, which is its own possible
               cause of growth; if the depth growth survives a always-empty
               WAL, the chain is the mechanism and not the WAL.

ATTRIBUTION. mcp_server._db_execute and mcp_server._index_write are wrapped
with accumulating timers, so every cycle is split into minigraf time (further
split by query / retract / transact) and fact-index time. The fact index's
delete path is itself an unattributed candidate named in #260, so it is
measured here rather than assumed innocent.

VERDICT THRESHOLDS, fixed here before any data exists so they cannot be chosen
to fit the result. Same 2.0x the #239 bench and #260's own design spec
pre-registered:

    GROWS = ratio >= 2.0        FLAT = ratio < 2.0

CONTROL GATE. db.checkpoint() is documented O(graph size) and #260 measured its
duration growing 7.85x across a real run. Axis B therefore times a checkpoint at
each filler size; it MUST grow >= 2.0x across the 50x sweep. A method that
cannot see growth where growth is known to exist cannot be trusted reporting
flatness anywhere else -- if this gate fails the run is VOID, not flat.

FIDELITY NOTES.
 * index_con is a REAL batched writer connection, committed once per cycle,
   exactly as _forward_apply does. Passing None would take _index_write's
   open/commit/close-per-call path, which production ingestion never uses.
 * The three updates are called through their production functions, not
   reimplemented, so the query-before-write and retract-only-if-changed
   behaviour is whatever mcp_server actually does.
 * SINGLE-HANDLE INVARIANT (docs/design-notes.md): at most one live MiniGrafDb per
   process. Every fixture drops its handle before the next one opens.

Run with .venv/bin/python -- bare python on this machine carries minigraf 1.1.1
against a >=1.2.3 floor and every number here would be wrong (#260's own
methodological warning).
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from minigraf import MiniGrafDb
import fact_index
import mcp_server

TS = "2026-01-01T00:00:00Z"

GROWTH_THRESHOLD = 2.0
CONTROL_THRESHOLD = 2.0

DEPTH_CYCLES = 6000
SIZE_CYCLES = 400
WAL_CYCLES = 1000
DECILES = 10

FILLER_SIZES = (100_000, 1_000_000, 5_000_000)
FILLER_MID = 1_000_000

# Axis C runs a checkpoint per cycle, and a checkpoint costs O(graph size)
# (measured: ~0.37 s at 100k facts, ~1.9 s at 500k). At FILLER_MID that arm
# alone would run for hours, so axis C uses the smallest filler. It asks a
# within-arm question -- does depth growth survive an always-empty WAL --
# which does not need the graph to be large, only the two arms to match.
FILLER_WAL = 100_000


# --------------------------------------------------------------------------
# attribution timers
# --------------------------------------------------------------------------

class Timers:
    """Accumulating per-cycle split of where a cycle's wall clock goes.

    Wraps the two choke points every one of the three updates funnels through:
    _db_execute (all minigraf traffic) and _index_write (all fact-index
    traffic). Keyed by operation so a growing retract can be told from a
    growing query -- the distinction that decides where a fix would go.
    """

    def __init__(self) -> None:
        self.buckets = {}
        self._orig_db_execute = mcp_server._db_execute
        self._orig_index_write = mcp_server._index_write

    def reset(self) -> None:
        self.buckets = {"query": 0.0, "retract": 0.0, "transact": 0.0,
                        "index_insert": 0.0, "index_delete": 0.0}

    def install(self) -> None:
        orig_db, orig_ix = self._orig_db_execute, self._orig_index_write
        buckets = lambda: self.buckets  # noqa: E731 -- reads the live dict, not a snapshot

        def db_execute(db, datalog):
            # The op name comes from the s-expression head, which is the only
            # thing that distinguishes a query from a retract from a transact
            # at this choke point.
            head = datalog.lstrip()[1:].split(None, 1)[0] if datalog.lstrip().startswith("(") else "query"
            key = head if head in ("query", "retract", "transact") else "query"
            t = time.perf_counter()
            try:
                return orig_db(db, datalog)
            finally:
                buckets()[key] += time.perf_counter() - t

        def index_write(action, triples, index_con=None):
            t = time.perf_counter()
            try:
                return orig_ix(action, triples, index_con=index_con)
            finally:
                buckets()["index_" + action] += time.perf_counter() - t

        mcp_server._db_execute = db_execute
        mcp_server._index_write = index_write

    def uninstall(self) -> None:
        mcp_server._db_execute = self._orig_db_execute
        mcp_server._index_write = self._orig_index_write


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def fresh_fixture(tmpdir, name):
    """A real on-disk graph + a real batched fact-index writer, as ingestion has."""
    path = os.path.join(tmpdir, f"{name}.graph")
    for suffix in ("", ".wal", ".lock"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    index_path = fact_index.index_path_for(path)
    if os.path.exists(index_path):
        os.remove(index_path)
    db = MiniGrafDb.open(path)
    index_con = fact_index.open_writer(index_path)
    return db, index_con, path


def drop_fixture(db, index_con):
    """Release both handles. The MiniGrafDb drop is the half that matters:
    a second open while this one is live is the #251/#253 corruption.
    """
    try:
        index_con.commit()
        fact_index.close_writer(index_con)
    except Exception:
        pass
    del db
    gc.collect()


def populate_filler(db, n, chunk=1000):
    """Bulk unrelated facts on distinct entities, setting total graph size
    without touching any of the three hot idents.
    """
    for start in range(0, n, chunk):
        batch = [f'[:e/n{i} :attr "v{i}"]' for i in range(start, min(start + chunk, n))]
        db.execute(f'(transact {{:valid-from "{TS}"}} [' + " ".join(batch) + "])")
    db.checkpoint()


def fake_hash(i):
    """A 40-char hex hash, so the stored value's LENGTH matches a real one --
    the retract/transact strings embed it verbatim.
    """
    return f"{i:040x}"


# --------------------------------------------------------------------------
# the measured cycle
# --------------------------------------------------------------------------

def run_cycles(db, index_con, linearization, n_cycles, timers, checkpoint_each=False):
    """Run _forward_apply's per-commit fixed tail n_cycles times, verbatim in
    production order, returning one record per cycle.

    Deliberately NOT the whole of _forward_apply: everything above this tail
    is work-scaled (it iterates extracted_files), and #260 already established
    that the growth is in the fixed term. This is the W == 0 commit's entire
    graph-write footprint.
    """
    records = []
    for i in range(n_cycles):
        commit_hash = linearization[i]
        timers.reset()
        t0 = time.perf_counter()
        mcp_server._watermark_update(db, commit_hash, TS, "probe", index_con)
        mcp_server._frontier_persist_claim(
            db, linearization, i, from_low=True, commit_ts_iso=TS, index_con=index_con,
        )
        mcp_server._lineage_confirmed_through_update(db, commit_hash, TS, index_con=index_con)
        mcp_server._commit_index_writer_safe(index_con)
        total = time.perf_counter() - t0
        rec = {"cycle": i, "total_s": total}
        rec.update(timers.buckets)
        records.append(rec)
        if checkpoint_each:
            # Excluded from the timing on purpose: this arm asks whether the
            # depth growth survives an always-empty WAL, not what a checkpoint
            # costs (#260 already measured that, and it is only 10% of the rise).
            db.checkpoint()
    return records


def decile_summary(records, key="total_s"):
    """Mean of `key` per decile of cycle order, plus the last/first ratio.

    Deciles rather than thirds because this probe controls its own cycle count
    and can afford the resolution; #260's thirds were forced by needing enough
    commits per group to fit two parameters.
    """
    n = len(records)
    size = n // DECILES
    means = []
    for d in range(DECILES):
        chunk = records[d * size:(d + 1) * size]
        means.append(statistics.fmean(r[key] for r in chunk))
    ratio = means[-1] / means[0] if means[0] > 0 else float("inf")
    return {"decile_means_s": means, "growth": ratio,
            "verdict": "GROWS" if ratio >= GROWTH_THRESHOLD else "FLAT"}


def component_split(records):
    """Total seconds per attribution bucket, plus each bucket's own decile
    growth -- so a flat total made of one growing and one shrinking component
    cannot hide.
    """
    keys = ("query", "retract", "transact", "index_insert", "index_delete")
    out = {}
    for k in keys:
        total = sum(r[k] for r in records)
        out[k] = {"total_s": total,
                  "share": total / sum(r["total_s"] for r in records),
                  **decile_summary(records, k)}
    return out


# --------------------------------------------------------------------------
# axes
# --------------------------------------------------------------------------

def axis_a_depth(tmpdir, timers, cycles, filler):
    db, index_con, _ = fresh_fixture(tmpdir, "depth")
    populate_filler(db, filler)
    lin = [fake_hash(i) for i in range(cycles)]
    timers.install()
    try:
        records = run_cycles(db, index_con, lin, cycles, timers)
    finally:
        timers.uninstall()
    drop_fixture(db, index_con)
    return {"filler": filler, "cycles": cycles,
            "summary": decile_summary(records),
            "components": component_split(records),
            "records": records}


def axis_b_size(tmpdir, timers, cycles, sizes):
    per_size = []
    for filler in sizes:
        db, index_con, _ = fresh_fixture(tmpdir, f"size{filler}")
        populate_filler(db, filler)
        # Control gate: a checkpoint on this graph, timed. Documented
        # O(graph size); if it does not grow across the sweep the harness
        # cannot see growth and every FLAT below is meaningless.
        #
        # The WAL is deliberately dirtied by exactly ONE fact first.
        # populate_filler already checkpointed, and minigraf's checkpoint on a
        # CLEAN WAL is a no-op that returns in microseconds at every size --
        # timing that would have produced a flat control and voided the run
        # for a harness bug. One dirty fact is also the sharpest statement of
        # the O(graph size) claim: the work is proportional to the graph, not
        # to what changed.
        db.execute(f'(transact {{:valid-from "{TS}"}} [[:probe/ckpt-dirty :attr "x"]])')
        t = time.perf_counter()
        db.checkpoint()
        ckpt_s = time.perf_counter() - t
        lin = [fake_hash(i) for i in range(cycles)]
        timers.install()
        try:
            records = run_cycles(db, index_con, lin, cycles, timers)
        finally:
            timers.uninstall()
        drop_fixture(db, index_con)
        per_size.append({
            "filler": filler,
            "checkpoint_s": ckpt_s,
            "mean_total_s": statistics.fmean(r["total_s"] for r in records),
            "median_total_s": statistics.median(r["total_s"] for r in records),
            "components": component_split(records),
        })
    growth = per_size[-1]["mean_total_s"] / per_size[0]["mean_total_s"]
    ckpt_growth = per_size[-1]["checkpoint_s"] / per_size[0]["checkpoint_s"]
    return {
        "cycles": cycles,
        "per_size": per_size,
        "growth": growth,
        "verdict": "GROWS" if growth >= GROWTH_THRESHOLD else "FLAT",
        "control_gate": {
            "checkpoint_growth": ckpt_growth,
            "threshold": CONTROL_THRESHOLD,
            "passed": ckpt_growth >= CONTROL_THRESHOLD,
        },
    }


def axis_c_wal(tmpdir, timers, cycles, filler):
    arms = {}
    for label, ckpt in (("no_checkpoint", False), ("checkpoint_each", True)):
        db, index_con, _ = fresh_fixture(tmpdir, f"wal_{label}")
        populate_filler(db, filler)
        lin = [fake_hash(i) for i in range(cycles)]
        timers.install()
        try:
            records = run_cycles(db, index_con, lin, cycles, timers, checkpoint_each=ckpt)
        finally:
            timers.uninstall()
        drop_fixture(db, index_con)
        arms[label] = {"summary": decile_summary(records),
                       "components": component_split(records)}
    return {"filler": filler, "cycles": cycles, "arms": arms}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=None, help="Write the full result JSON here.")
    p.add_argument("--smoke", action="store_true",
                   help="Tiny sizes for a correctness check of the harness itself. "
                        "Smoke numbers are NOT a measurement and are labeled so in the JSON.")
    p.add_argument("--axes", default="abc", help="Subset of axes to run, e.g. 'a'.")
    args = p.parse_args()

    depth_cycles, size_cycles, wal_cycles = DEPTH_CYCLES, SIZE_CYCLES, WAL_CYCLES
    sizes, filler_mid, wal_filler = FILLER_SIZES, FILLER_MID, FILLER_WAL
    if args.smoke:
        depth_cycles, size_cycles, wal_cycles = 200, 40, 100
        sizes, filler_mid, wal_filler = (1_000, 10_000), 10_000, 1_000

    timers = Timers()
    result = {
        "issue": 260,
        "smoke": args.smoke,
        "minigraf_version": getattr(__import__("minigraf"), "__version__", "unknown"),
        "python": sys.executable,
        "thresholds": {"growth": GROWTH_THRESHOLD, "control": CONTROL_THRESHOLD},
    }
    with tempfile.TemporaryDirectory(prefix="probe260-") as tmpdir:
        os.environ["MINIGRAF_GRAPH_PATH"] = os.path.join(tmpdir, "unused.graph")
        if "a" in args.axes:
            t = time.perf_counter()
            result["axis_a_depth"] = axis_a_depth(tmpdir, timers, depth_cycles, filler_mid)
            result["axis_a_depth"]["wall_s"] = time.perf_counter() - t
            print(f"axis A: {result['axis_a_depth']['summary']['verdict']} "
                  f"{result['axis_a_depth']['summary']['growth']:.2f}x", flush=True)
        if "b" in args.axes:
            t = time.perf_counter()
            result["axis_b_size"] = axis_b_size(tmpdir, timers, size_cycles, sizes)
            result["axis_b_size"]["wall_s"] = time.perf_counter() - t
            print(f"axis B: {result['axis_b_size']['verdict']} "
                  f"{result['axis_b_size']['growth']:.2f}x  "
                  f"control {result['axis_b_size']['control_gate']['checkpoint_growth']:.2f}x "
                  f"{'PASS' if result['axis_b_size']['control_gate']['passed'] else 'FAIL'}", flush=True)
        if "c" in args.axes:
            t = time.perf_counter()
            result["axis_c_wal"] = axis_c_wal(tmpdir, timers, wal_cycles, wal_filler)
            result["axis_c_wal"]["wall_s"] = time.perf_counter() - t
            for label, arm in result["axis_c_wal"]["arms"].items():
                print(f"axis C {label}: {arm['summary']['verdict']} "
                      f"{arm['summary']['growth']:.2f}x", flush=True)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
