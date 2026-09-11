# evals/at_scale/profile_forward_reconcile_attribution.py
"""Two-revision ingestion attribution harness (#235).

Runs the SAME bounded commit slice through the real `_run_ingestion` and
reports where the wall clock goes, so two revisions of mcp_server.py can be
compared side by side. Written for #235's 30x at-scale regression, but the
harness itself is revision-agnostic: it imports mcp_server from an explicit
--code-root, so a git worktree at any revision can be profiled without
checking anything out in the main working tree.

Covers Stage A (the two-stream converging walk) AND Stage B (the correction
sweep) -- unlike profile_reverse_walk_writes.py, which drives
_reverse_fill_claim_and_process directly and therefore sees Stage A only.
The phase boundary is sampled from _ingest_progress["phase"] by a polling
task, so Stage A and Stage B wall clock are reported separately.

Two measurement layers, because they answer different questions:

  1. Wrapper counters/timers on the interesting call sites (_transact,
     _retract, _db_execute, _lineage_is_provisional,
     _forward_reconcile_provisional, _re_date_structural_facts, ...).
     Cheap (sys._getframe, not traceback.extract_stack), so --no-profile
     runs give a near-clean wall clock while still attributing writes.
  2. cProfile, enabled on EVERY thread (via threading.setprofile) and
     merged, because _run_ingestion does all of its DB work on a dedicated
     write_executor thread -- a main-thread-only cProfile sees nothing.
     Extraction runs in a spawn ProcessPoolExecutor and is NOT visible to
     cProfile; its cost shows up in wall clock only.

Self-isolating: creates its own tempdir and points MINIGRAF_GRAPH_PATH and
MINIGRAF_INDEX_PATH at it, so it never touches memory.graph.

    .venv/bin/python evals/at_scale/profile_forward_reconcile_attribution.py \
        --code-root /path/to/worktree --repo /path/to/repo --ref <sha> \
        --label before --outdir /tmp/attr [--no-profile]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import cProfile
import io
import json
import os
import pstats
import sys
import tempfile
import threading
import time


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--code-root", required=True,
                   help="Directory to import mcp_server.py from (a git worktree).")
    p.add_argument("--repo", required=True, help="Repository whose history is ingested.")
    p.add_argument("--ref", required=True,
                   help="Explicit ref/SHA bounding the slice; both revisions must be given "
                        "the same one so they see identical input.")
    p.add_argument("--label", required=True, help="Short name for this run (before/after).")
    p.add_argument("--outdir", required=True, help="Where to write JSON + pstats output.")
    p.add_argument("--no-profile", action="store_true",
                   help="Skip cProfile (wrapper counters only) for a near-clean wall clock.")
    p.add_argument("--workers", default="4",
                   help="MINIGRAF_INGEST_WORKERS; pinned so both revisions match.")
    p.add_argument("--max-seconds", type=float, default=0.0,
                   help="Stop Stage A cleanly (via _shutdown_requested) after this many "
                        "seconds. 0 = run to completion. A capped run SKIPS Stage B -- "
                        "compare capped runs on the progress timeline, not on wall clock.")
    p.add_argument("--checkpoint-mode", choices=["normal", "noop", "every-N", "stage-b-only"],
                   default="normal",
                   help="Override ingestion checkpoint cadence for the #241 decision "
                        "ablation. normal (default): no override at all -- exactly today's "
                        "behaviour, whatever mcp_server.py's own gating does (on this branch "
                        "that is the shipped MINIGRAF_INGEST_CHECKPOINT_DUTY duty-cycle "
                        "policy). noop: suppress every ingestion checkpoint. every-N: let "
                        "only every --checkpoint-every-n-th checkpoint attempt actually run. "
                        "stage-b-only: keep the shipped duty policy's budget in force ONLY "
                        "while m._ingest_progress['phase'] == 'sweeping' (Stage B); every "
                        "other phase always checkpoints, ungated, as if Task 4 had never "
                        "widened the gate to Stage A. See _install_checkpoint_mode_override "
                        "for exactly what each mode monkeypatches.")
    p.add_argument("--checkpoint-every-n", type=int, default=25,
                   help="N for --checkpoint-mode every-N (default 25, matching the design "
                        "spec's original every25 ablation leg).")
    return p.parse_args()


ARGS = _parse_args()

# Import the revision under test, and nothing from anywhere else. This must
# happen before `import mcp_server`.
sys.path.insert(0, os.path.abspath(ARGS.code_root))

TMPDIR = tempfile.mkdtemp(prefix=f"attr-{ARGS.label}-")
GRAPH_PATH = os.path.join(TMPDIR, "bench.graph")
os.environ["MINIGRAF_GRAPH_PATH"] = GRAPH_PATH
os.environ["MINIGRAF_INDEX_PATH"] = GRAPH_PATH + ".fts.sqlite3"
os.environ["MINIGRAF_INGEST_WORKERS"] = ARGS.workers

import mcp_server as m  # noqa: E402

from evals.at_scale.commit_census import walk_claimed_from_progress  # noqa: E402

assert os.path.abspath(os.path.dirname(m.__file__)) == os.path.abspath(ARGS.code_root), (
    f"mcp_server resolved to {m.__file__}, not {ARGS.code_root}"
)

# --------------------------------------------------------------------------
# Layer 1: wrapper counters/timers.
# --------------------------------------------------------------------------

_lock = threading.Lock()
counts: collections.Counter = collections.Counter()
timings: collections.Counter = collections.Counter()
by_caller: collections.Counter = collections.Counter()
caller_timings: collections.Counter = collections.Counter()


def _wrap(name: str, attribute_caller: bool = False):
    """Replace m.<name> with a counting/timing wrapper. Module-global call
    sites inside mcp_server resolve the name at call time, so this is seen by
    every internal caller."""
    real = getattr(m, name)

    def wrapper(*a, **k):
        t0 = time.perf_counter()
        try:
            return real(*a, **k)
        finally:
            dt = time.perf_counter() - t0
            caller = sys._getframe(1).f_code.co_name if attribute_caller else None
            with _lock:
                counts[name] += 1
                timings[name] += dt
                if caller is not None:
                    by_caller[(name, caller)] += 1
                    caller_timings[(name, caller)] += dt

    wrapper.__name__ = f"wrapped_{name}"
    setattr(m, name, wrapper)
    return real


WRAPPED = [
    "_transact",
    "_retract",
    "_db_execute",
    "_lineage_is_provisional",
    "_forward_reconcile_provisional",
    "_re_date_structural_facts",
    "_lineage_confirm",
    "_lineage_confirm_batch",
    "_entity_introduced_by_query",
    "_forward_apply",
    "_reverse_apply",
    "_correction_sweep_apply",
    "_db_checkpoint",
]
ATTRIBUTE_CALLER = {"_transact", "_retract"}
for _n in WRAPPED:
    if hasattr(m, _n):
        _wrap(_n, attribute_caller=_n in ATTRIBUTE_CALLER)
        # Pre-seed so a mode that drives a wrapped call site's REAL count to
        # zero (--checkpoint-mode noop, or every-N on a run too short to hit
        # N attempts) reports 0 in call_counts/call_seconds, not a missing
        # key -- a consumer reading call_counts["_db_checkpoint"] should
        # never have to guard against it being absent.
        counts.setdefault(_n, 0)
        timings.setdefault(_n, 0.0)


def _install_checkpoint_mode_override() -> None:
    """Install --checkpoint-mode's override, if any, on top of the counting
    wrappers installed above (#241 Task 5's decision ablation).

    'normal' installs nothing at all -- the harness runs exactly as it did
    before this flag existed, so entries recorded with no flag stay
    byte-identical and comparable to entries recorded with
    --checkpoint-mode normal (the default) or with the flag simply omitted.

    Every other mode monkeypatches m._db_checkpoint and/or
    m._db_checkpoint_gated to the ALREADY-wrapped versions above, so the
    call counts and seconds these modes report still come from the same
    counting wrapper 'normal' uses -- what changes is only whether/when the
    real checkpoint underneath ever runs.

    'noop' and 'every-N' patch the LOW-LEVEL m._db_checkpoint, the exact
    patch point the original hand-worktree ablation used (design spec
    provenance: "one git worktree per leg patched only at _db_checkpoint").
    Every ingestion checkpoint funnels through it -- both call sites gated
    via m._db_checkpoint_gated (through _CheckpointPolicy.force()) and the
    unconditional final checkpoint on every terminal path (Task 3) -- so
    these two modes suppress uniformly across both stages, matching the
    spec's noop/every25 legs. The shipped duty-cycle policy (Task 4) stays
    installed and keeps calling _db_checkpoint_gated -> policy.maybe() at
    every site; with the underlying checkpoint made near-free, the policy's
    own budget math sees a near-zero last duration and so rarely if ever
    suppresses on its own -- nearly every gated attempt reaches this
    override, which is what actually decides whether real work happens.
    That is a deliberate difference from the pre-Task-4 code these modes
    were first measured against, not a bug: the two modes are not being
    used to reproduce old absolute call counts, only the uniform-suppression
    SHAPE, on top of whatever cadence today's code would otherwise pick.

    'stage-b-only' patches m._db_checkpoint_gated instead, and narrows
    rather than replaces: while m._ingest_progress['phase'] == 'sweeping'
    (Stage B) it defers to the real, unmodified _db_checkpoint_gated --
    same policy object, same budget math as 'duty'. Every other phase
    (Stage A's 'converging', or no phase yet) always checkpoints, via the
    counted m._db_checkpoint, exactly as sites did before Task 4 gated them.
    The unconditional final checkpoint is untouched either way -- it calls
    m._db_checkpoint directly, never m._db_checkpoint_gated.
    """
    mode = ARGS.checkpoint_mode
    if mode == "normal":
        return

    if mode == "noop":
        def _noop_checkpoint(db):
            return None
        m._db_checkpoint = _noop_checkpoint
        return

    if mode == "every-N":
        n = ARGS.checkpoint_every_n
        if n < 1:
            raise ValueError(f"--checkpoint-every-n must be >= 1, got {n}")
        real_checkpoint = m._db_checkpoint
        state = {"attempts": 0}

        def _every_n_checkpoint(db):
            state["attempts"] += 1
            if state["attempts"] % n != 0:
                return None
            return real_checkpoint(db)
        m._db_checkpoint = _every_n_checkpoint
        return

    if mode == "stage-b-only":
        real_checkpoint = m._db_checkpoint
        real_checkpoint_gated = m._db_checkpoint_gated

        def _stage_b_only_gate(db):
            if m._ingest_progress.get("phase") == "sweeping":
                return real_checkpoint_gated(db)
            real_checkpoint(db)
            return True
        m._db_checkpoint_gated = _stage_b_only_gate
        return

    raise ValueError(f"unknown --checkpoint-mode: {mode!r}")  # pragma: no cover - argparse guards this


_install_checkpoint_mode_override()

# --------------------------------------------------------------------------
# Layer 2: cProfile on every thread.
# --------------------------------------------------------------------------

_profilers: list = []
_prof_lock = threading.Lock()


_thread_profiled: set = set()


def _install_thread_profiler(*_a):
    """Give this thread its own cProfile.

    MUST NOT raise. threading.setprofile's hook runs inside Thread._bootstrap
    BEFORE the thread's target; an exception here kills the thread at
    bootstrap. On Python 3.14 a second install attempt on a thread that
    already has a profiler raises "ValueError: Another profiling tool is
    already active" -- which silently killed every write_executor /
    ProcessPoolExecutor management thread, hanging ingestion until the outer
    timeout rather than producing a profile. Guard on thread identity and
    swallow anything else.
    """
    tid = threading.get_ident()
    with _prof_lock:
        if tid in _thread_profiled:
            return
        _thread_profiled.add(tid)
        p = cProfile.Profile()
        _profilers.append(p)
    try:
        p.enable()  # replaces this hook with the profiler itself for this thread
    except Exception as e:  # pragma: no cover - defensive; see docstring
        print(f"[profile] could not enable on thread {tid}: {e}", file=sys.stderr)


def _start_profiling() -> None:
    threading.setprofile(_install_thread_profiler)
    main = cProfile.Profile()
    _profilers.append(main)
    main.enable()


def _stop_profiling() -> pstats.Stats | None:
    threading.setprofile(None)
    merged = None
    for p in _profilers:
        try:
            p.disable()
            p.create_stats()
        except Exception:
            continue
        try:
            if merged is None:
                merged = pstats.Stats(p)
            else:
                merged.add(p)
        except Exception:
            continue
    return merged


# --------------------------------------------------------------------------
# Drive the ingestion.
# --------------------------------------------------------------------------

phase_marks: list = []
progress_timeline: list = []


async def _watch_phase(task: "asyncio.Task[None]", t_start: float) -> None:
    last = None
    last_sample = 0.0
    capped = False
    while not task.done():
        now = time.perf_counter()
        phase = m._ingest_progress.get("phase")
        if phase != last:
            phase_marks.append((phase, now))
            last = phase
        if now - last_sample >= 1.0:
            last_sample = now
            progress_timeline.append({
                "t": round(now - t_start, 2),
                "phase": phase,
                "retired": (
                    m._ingest_progress["_run"].retired_count
                    if m._ingest_progress.get("_run") else None
                ),
                "graph_bytes": os.path.getsize(GRAPH_PATH) if os.path.exists(GRAPH_PATH) else 0,
                "reconciles": counts.get("_forward_reconcile_provisional", 0),
                "retracts": counts.get("_retract", 0),
                "queries": counts.get("_db_execute", 0),
            })
        if ARGS.max_seconds and not capped and now - t_start >= ARGS.max_seconds:
            capped = True
            print(f"[{ARGS.label}] max-seconds reached; requesting clean stop",
                  flush=True)
            m._shutdown_requested.set()
        await asyncio.sleep(0.05)


async def _main() -> dict:
    m._reset_db_state()
    m.open_db(GRAPH_PATH)
    m._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        "phase": None,
    }
    start = time.perf_counter()
    task = asyncio.create_task(m._run_ingestion(ARGS.repo, ARGS.ref))
    await _watch_phase(task, start)
    await task
    wall = time.perf_counter() - start
    return {
        "wall_clock_seconds": wall,
        "capped": bool(ARGS.max_seconds),
        "commits_processed": walk_claimed_from_progress(m._ingest_progress),
        "final_status": m._ingest_progress.get("status"),
        "error": m._ingest_progress.get("error"),
    }


def main() -> int:
    os.makedirs(ARGS.outdir, exist_ok=True)
    n_commits = len(m._git_commits(ARGS.repo, None, ARGS.ref))
    print(f"[{ARGS.label}] code-root={ARGS.code_root}")
    print(f"[{ARGS.label}] slice={ARGS.ref} -> {n_commits} commits, graph={GRAPH_PATH}")
    print(f"[{ARGS.label}] cProfile={'off' if ARGS.no_profile else 'on'}")

    if not ARGS.no_profile:
        _start_profiling()
    result = asyncio.run(_main())
    stats = None if ARGS.no_profile else _stop_profiling()

    result.update({
        "label": ARGS.label,
        "code_root": os.path.abspath(ARGS.code_root),
        "repo": os.path.abspath(ARGS.repo),
        "ref": ARGS.ref,
        "slice_commits": n_commits,
        "checkpoint_mode": ARGS.checkpoint_mode,
        "checkpoint_every_n": ARGS.checkpoint_every_n if ARGS.checkpoint_mode == "every-N" else None,
        "profiled": not ARGS.no_profile,
        "graph_size_bytes": os.path.getsize(GRAPH_PATH) if os.path.exists(GRAPH_PATH) else 0,
        "index_size_bytes": (
            os.path.getsize(GRAPH_PATH + ".fts.sqlite3")
            if os.path.exists(GRAPH_PATH + ".fts.sqlite3") else 0
        ),
        "phase_marks": [
            {"phase": p, "at_seconds": t - (phase_marks[0][1] if phase_marks else t)}
            for p, t in phase_marks
        ],
        "progress_timeline": progress_timeline,
        "call_counts": {k: counts[k] for k in sorted(counts)},
        "call_seconds": {k: round(timings[k], 3) for k in sorted(timings)},
        "by_caller": {
            f"{fn}<-{caller}": {"count": n, "seconds": round(caller_timings[(fn, caller)], 3)}
            for (fn, caller), n in by_caller.most_common()
        },
    })

    print(f"\n[{ARGS.label}] wall clock: {result['wall_clock_seconds']:.1f}s  "
          f"status={result['final_status']}  commits={result['commits_processed']}")
    print(f"[{ARGS.label}] phase marks: "
          + ", ".join(f"{p}@{d['at_seconds']:.1f}s"
                      for p, d in zip([x['phase'] for x in result['phase_marks']],
                                      result['phase_marks'])))
    print(f"\n{'call site':<38} {'count':>9} {'sec':>9}")
    for k in sorted(counts, key=lambda x: -timings[x]):
        print(f"{k:<38} {counts[k]:>9} {timings[k]:>9.1f}")

    print(f"\n{'write call site (by caller)':<58} {'count':>9} {'sec':>9}")
    for (fn, caller), n in by_caller.most_common(25):
        print(f"{fn + ' <- ' + caller:<58} {n:>9} {caller_timings[(fn, caller)]:>9.1f}")

    json_path = os.path.join(ARGS.outdir, f"attr-{ARGS.label}.json")
    with open(json_path, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nWrote {json_path}")

    if stats is not None:
        buf = io.StringIO()
        stats.stream = buf
        stats.sort_stats("cumulative").print_stats(45)
        buf.write("\n\n===== SORTED BY TOTTIME =====\n\n")
        stats.sort_stats("tottime").print_stats(35)
        text = buf.getvalue()
        prof_path = os.path.join(ARGS.outdir, f"attr-{ARGS.label}.prof.txt")
        with open(prof_path, "w") as fh:
            fh.write(text)
        stats.dump_stats(os.path.join(ARGS.outdir, f"attr-{ARGS.label}.pstats"))
        print(text[:20000])
        print(f"Wrote {prof_path}")

    print(f"\ngraph: {result['graph_size_bytes']} B  index: {result['index_size_bytes']} B "
          f"({TMPDIR})")
    return 0 if result["final_status"] != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
