#!/usr/bin/env python3
"""Which call sites issue ingestion's retracts, and are they needed? (#369)

After #239's lineage cache, the correction sweep's (Stage B, phase
`sweeping`) retracts were the largest remaining bucket: 241,438 calls, ~605 s
of exec time over full history, ~37% of wall clock
(`results/239-lineage-cache-ab.json`). That harness buckets by command SHAPE,
not caller, so it cannot say which site to change. This one can.

ONE instrumented `_run_ingestion` (no A/B -- nothing here is compared across
builds). `_db_execute` is replaced by a timed copy that takes
`_db_native_lock` exactly as the original does and times only `db.execute`
inside it -- the leaf-timing approach of `probe_minigraf_upgrade_cost.py`.
Every write command (`transact`/`retract`) is keyed by:

  * the ingestion PHASE (`_ingest_progress["phase"]`),
  * its CALL SITE: the first two frames above the write plumbing
    (`_retract`, `_transact`, `_db_execute`), as `outer>inner:line` --
    `call_site` below, pure and unit-tested,
  * the sorted set of ATTRIBUTES it carries.

Per key it records calls, facts, exec seconds, and per-call exec samples, so
per-call vs per-fact cost is visible. Keys are exhaustive over writes and sum
without double counting.

THE LIVE-FACT TRACKER answers "are they needed at all". On a fresh graph
every fact is written by this run, so an in-memory set of live
`(entity, attribute, value)` triples -- a current transact adds, a retract
removes, a bounded (`:valid-to`) transact adds nothing -- is complete for
keyword entities. Each retracted triple is then classified:

  * `noop`: not live when retracted. A retract that removes nothing.
  * `same_commit`: live, and written by the SAME apply unit -- same phase,
    same commit -- churn a call site might avoid by not writing in the first
    place.
  * `earlier`: live, written by an earlier commit's work.

The commit is a THREAD-LOCAL tag set by wrapping `_forward_apply`,
`_reverse_apply` and `_correction_sweep_apply` (the parity probe's approach):
each issues its commands synchronously on the write-executor thread, and
`_ingest_progress["current_commit"]` is never updated in Stage B. Writes
outside any apply (frontier, watermark, control facts) carry the tag `-`.

and each retract records the phase/site that WROTE the fact it removes, so
"the sweep retracting what Stage A wrote" and "the sweep retracting what the
sweep wrote" are separate rows. `#uuid` entities cannot be tracked by ident
and are counted as `untracked`, never guessed.

A RETRACT THEN AN IDENTICAL RE-TRANSACT at the same site (the re-date shape,
`_re_date_structural_facts`) is counted per site as `retransacted`: the next
transact from that site carrying the same triple within the same commit.

PYTHONHASHSEED=0 is required (set-iteration order of emitted triples; see
`probe_forward_apply_write_parity.py`). `run` sets it for its child and kills
the child's whole process group on exit (CLAUDE.md, #313). The frame walk and
tracker add overhead: absolute times are inflated uniformly per call, so read
SHARES and per-call ratios, not wall clock.

    .venv/bin/python evals/at_scale/probe_sweep_retract_attribution.py run \\
        --repo . --ref master --workdir ~/.cache/probe369 \\
        --out evals/at_scale/results/369-retract-attribution.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import signal
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Frames that are write plumbing, not call sites.
PLUMBING = frozenset({"_retract", "_transact", "_db_execute", "timed_execute", "spy"})

SAMPLE_CAP = 20000  # per-key per-call exec samples kept for percentiles


def call_site(frames: Iterable[Tuple[str, int]]) -> str:
    """`outer>inner:line` for the first two non-plumbing frames. Pure.

    frames: (function name, line number) pairs, innermost first, as walked
    from the timed execute upward. `inner` is the function that called the
    write helper, with the line of that call; `outer` is its caller, name
    only (its line varies with the inner function's own call sites and
    would split one site into many rows).
    """
    found: List[Tuple[str, int]] = []
    for name, line in frames:
        if name in PLUMBING:
            continue
        found.append((name, line))
        if len(found) == 2:
            break
    if not found:
        return "?"
    inner = f"{found[0][0]}:{found[0][1]}"
    return f"{found[1][0]}>{inner}" if len(found) == 2 else inner


def site_function(site: str) -> str:
    """`call_site` output with the inner line number dropped. Pure."""
    return site.rsplit(":", 1)[0] if ":" in site else site


def same_unit(a: Optional[str], b: str) -> bool:
    """Whether two `phase@commit` unit tags name the same apply unit. A write
    outside any apply carries commit `-`, which is no unit at all, so two
    such writes are never the same one. Pure."""
    return a is not None and a == b and not a.endswith("@-")


def split_command(datalog: str) -> Tuple[str, str]:
    """(op, body) for a write command: op is `transact`, `transact-bounded`
    (carries `:valid-to`), `retract`, or `other`. body is the facts block. Pure."""
    s = datalog.lstrip()
    if s.startswith("(retract "):
        return "retract", s[len("(retract "):-1]
    if s.startswith("(transact "):
        rest = s[len("(transact "):]
        if rest.startswith("{"):
            end = rest.index("}")
            opts, rest = rest[: end + 1], rest[end + 1:]
            op = "transact-bounded" if ":valid-to" in opts else "transact"
        else:
            op = "transact"
        return op, rest.strip()[:-1]
    return "other", ""


def summarize(rows: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten per-key rows into a list sorted by exec time, with per-call
    and per-fact means and per-call percentiles. Pure."""
    out = []
    for key, r in rows.items():
        samples = sorted(r["samples"])
        n = r["n"]

        def pct(p: float) -> Optional[float]:
            if not samples:
                return None
            return samples[min(len(samples) - 1, int(p * len(samples)))]

        out.append({
            "key": key,
            "n": n,
            "facts": r["facts"],
            "exec_s": r["exec_s"],
            "ms_per_call": 1000 * r["exec_s"] / n if n else None,
            "ms_per_fact": 1000 * r["exec_s"] / r["facts"] if r["facts"] else None,
            "p50_ms": None if pct(0.5) is None else 1000 * pct(0.5),
            "p90_ms": None if pct(0.9) is None else 1000 * pct(0.9),
            "p99_ms": None if pct(0.99) is None else 1000 * pct(0.99),
            **{k: v for k, v in r.items()
               if k not in ("n", "facts", "exec_s", "samples")},
        })
    out.sort(key=lambda d: -d["exec_s"])
    return out


# --------------------------------------------------------------------------
# arm: one instrumented ingestion (runs in the child)
# --------------------------------------------------------------------------


def _arm(args: argparse.Namespace) -> int:
    if os.environ.get("PYTHONHASHSEED") != "0":
        print("refusing: PYTHONHASHSEED=0 must be set on the command line",
              file=sys.stderr)
        return 2
    for key in [k for k in os.environ if k.startswith("MINIGRAF_")]:
        del os.environ[key]
    workdir = pathlib.Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    graph = workdir / "g.graph"
    for stale in workdir.glob("g.graph*"):
        stale.unlink()
    os.environ["MINIGRAF_GRAPH_PATH"] = str(graph)
    sys.path.insert(0, str(_REPO_ROOT))

    import importlib.metadata as md

    import mcp_server as m

    lock = threading.Lock()
    rows: Dict[str, Dict[str, Any]] = {}
    # (e, a, v) -> (phase|site, commit) of the write that made it live.
    live: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
    # site -> {triple: commit} retracted and not yet re-transacted there.
    pending_retransact: Dict[str, Dict[Tuple[str, str, str], str]] = {}
    removed_from: Dict[str, Dict[str, int]] = {}  # retract key -> writer -> n
    totals = {"db_exec_s": 0.0, "write_exec_s": 0.0}

    def phase() -> str:
        return m._ingest_progress.get("phase") or "other"

    tag = threading.local()

    def tagged(fn: Any, commit_of: Any) -> Any:
        def spy(*a: Any, **k: Any) -> Any:
            prev = getattr(tag, "commit", None)
            tag.commit = commit_of(a)
            try:
                return fn(*a, **k)
            finally:
                tag.commit = prev
        return spy

    # Positional indexing is stable: every defaulted parameter of the two
    # apply functions is keyword-only (#346); Stage B passes the sweep's
    # commit hash as positional argument 1.
    m._forward_apply = tagged(m._forward_apply, lambda a: a[3][0])
    m._reverse_apply = tagged(m._reverse_apply, lambda a: a[2][a[4]])
    m._correction_sweep_apply = tagged(m._correction_sweep_apply, lambda a: a[1])

    def frames() -> Iterable[Tuple[str, int]]:
        f = sys._getframe(2)
        while f is not None:
            yield f.f_code.co_name, f.f_lineno
            f = f.f_back

    def timed_execute(db: Any, datalog: str) -> str:
        with m._db_native_lock:
            t0 = time.perf_counter()
            out = db.execute(datalog)
            dt = time.perf_counter() - t0
        op, body = split_command(datalog)
        with lock:
            totals["db_exec_s"] += dt
            if op == "other":
                return out
            totals["write_exec_s"] += dt
            ph = phase()
            site = call_site(frames())
            commit = f"{ph}@{getattr(tag, 'commit', None) or '-'}"
            triples = m._parse_facts_block(body)
            attrs = ",".join(sorted({a for _e, a, _v in triples}))
            key = f"{ph}|{op}|{site}|{attrs}"
            row = rows.setdefault(key, {
                "n": 0, "facts": 0, "exec_s": 0.0, "samples": [],
                "noop": 0, "same_commit": 0, "earlier": 0, "untracked": 0,
                "retransacted": 0,
            })
            row["n"] += 1
            row["facts"] += len(triples)
            row["exec_s"] += dt
            if len(row["samples"]) < SAMPLE_CAP:
                row["samples"].append(dt)
            writer_key = f"{ph}|{site}"
            # Re-transact pairing ignores the inner LINE: a retract and its
            # re-assert are two lines of one function (_re_date_structural_facts).
            pair_key = f"{ph}|{site_function(site)}"
            if op == "retract":
                pend = pending_retransact.setdefault(pair_key, {})
                for t in triples:
                    if t[0].startswith("#") or not t[0].startswith(":"):
                        row["untracked"] += 1
                        continue
                    prev = live.pop(t, None)
                    if prev is None:
                        row["noop"] += 1
                    else:
                        row["same_commit" if same_unit(prev[1], commit) else "earlier"] += 1
                        w = removed_from.setdefault(key, {})
                        w[prev[0]] = w.get(prev[0], 0) + 1
                    pend[t] = commit
            elif op == "transact":
                pend = pending_retransact.get(pair_key)
                for t in triples:
                    if pend and same_unit(pend.pop(t, None), commit):
                        row["retransacted"] += 1
                    if t[0].startswith(":"):
                        live[t] = (writer_key, commit)
        return out

    m._db_execute = timed_execute
    m._reset_db_state()
    m._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
        "error": None, "owner_pid": None, "error_at": None, "phase": None,
    }
    started = time.monotonic()
    asyncio.run(m._run_ingestion(args.repo, args.ref))
    wall = time.monotonic() - started
    final_status = m._ingest_progress.get("status")
    m._reset_db_state()

    by_writer = {k: dict(sorted(v.items(), key=lambda kv: -kv[1]))
                 for k, v in removed_from.items()}
    summary = summarize(rows)
    for r in summary:
        r["removed_facts_written_by"] = by_writer.get(r["key"], {})
    out = {
        "issue": 369,
        "minigraf_version": md.version("minigraf"),
        "python_version": sys.version.split()[0],
        "lineage_cache": m._LINEAGE_CACHE_ENABLED,
        "repo": args.repo,
        "ref": args.ref,
        "final_status": final_status,
        "wall_s": wall,
        "db_exec_s": totals["db_exec_s"],
        "write_exec_s": totals["write_exec_s"],
        "rows": summary,
    }
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2))
    if not args.keep_graph:
        for f in workdir.glob("g.graph*"):
            f.unlink()
    return 0


# --------------------------------------------------------------------------
# run: spawn the arm in its own process group
# --------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    if args.ref == "HEAD":
        print("refusing a literal HEAD ref (CLAUDE.md, #330)", file=sys.stderr)
        return 2
    sha = subprocess.check_output(
        ["git", "-C", args.repo, "rev-parse", "--verify", f"{args.ref}^{{commit}}"],
        text=True).strip()
    workdir = pathlib.Path(args.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGRAF_")}
    env["PYTHONHASHSEED"] = "0"
    cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), "arm",
           "--repo", str(pathlib.Path(args.repo).resolve()), "--ref", sha,
           "--workdir", str(workdir), "--out", str(pathlib.Path(args.out).resolve())]
    if args.keep_graph:
        cmd.append("--keep-graph")
    with open(workdir / "console.log", "w") as log:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, cwd=str(_REPO_ROOT))
        try:
            rc = proc.wait()
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if rc != 0:
        print(f"arm failed (rc={rc}); see {workdir / 'console.log'}", file=sys.stderr)
        return rc or 1
    res = json.loads(pathlib.Path(args.out).read_text())
    print(f"status {res['final_status']}  wall {res['wall_s']:.1f}s  "
          f"db exec {res['db_exec_s']:.1f}s  writes {res['write_exec_s']:.1f}s")
    for r in res["rows"][: args.top]:
        print(f"{r['exec_s']:8.1f}s {r['n']:8d} calls {r['facts']:8d} facts "
              f"{r['ms_per_call']:6.2f} ms/call  noop {r['noop']:6d} "
              f"same {r['same_commit']:6d} earlier {r['earlier']:6d} "
              f"retx {r['retransacted']:6d}  {r['key']}")
    print(f"wrote {args.out}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("arm", help="one instrumented ingestion (internal)")
    a.add_argument("--repo", required=True)
    a.add_argument("--ref", required=True)
    a.add_argument("--workdir", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--keep-graph", action="store_true")
    r = sub.add_parser("run", help="spawn one instrumented ingestion")
    r.add_argument("--repo", required=True)
    r.add_argument("--ref", required=True)
    r.add_argument("--workdir", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--keep-graph", action="store_true")
    r.add_argument("--top", type=int, default=25)
    args = p.parse_args(argv)
    return _arm(args) if args.mode == "arm" else _run(args)


if __name__ == "__main__":
    sys.exit(main())
