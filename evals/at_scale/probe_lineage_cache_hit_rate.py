#!/usr/bin/env python3
"""What would an in-run cache of lineage point queries actually save? (#239)

#239 is kept open for an in-repo cache on the attributes lineage
reconciliation rewrites (`:introduced-by`, `:modified-in`), which minigraf
2.0.2 left at O(their own history) per point query (upstream minigraf#379 is
the structural fix, v3.0.0 only). Before designing one, this measures what a
cache could earn and whether each candidate policy is even CORRECT, on a real
ingestion rather than by argument.

ONE REAL `_run_ingestion`, recorded. `_db_execute` is wrapped (taking
`_db_native_lock` exactly as the original does) and every call is logged in
order as one of:

  * READ  -- a bound-entity point query `[:find ?x :where [<e> <attr> ?x]]`
             on a WATCHED attribute, with its exec time and its result set;
  * WRITE -- a `(transact {...} ...)` or `(retract ...)`, parsed into its
             (entity, attribute, value) triples plus its valid-time window;
  * OPEN  -- a 0 -> 1 lease acquire (a fresh MiniGrafDb handle). Between a
             drop and the next open another PROCESS may have written, so a
             per-handle scope is what a cache must assume without some other
             cross-process signal.

Every graph write in mcp_server goes through `_transact`/`_retract`, so the
WRITE stream is complete for this process.

POLICIES are then SIMULATED offline over the log (`simulate`, pure):

  * `invalidate` -- memo keyed on (entity, attribute); any write touching the
    key drops it.
  * `write_through` -- a current transact (no :valid-to, :valid-from <= the
    write's wall time) ADDS the value to a cached key, a retract REMOVES it;
    any other write drops the key.

each under two scopes, `run` (never cleared) and `handle` (cleared on every
OPEN). A write whose entity is not a keyword (a `#uuid` reference) drops every
key on that attribute, since it cannot be keyed without a query.

`never_invalidate` (run scope) is the negative control: it ignores every
write, so it MUST mismatch -- a zero there means the oracle below is blind.

CORRECTNESS IS CHECKED, NOT ASSUMED. Every simulated hit is compared with the
result the real query returned at that moment. A policy with any mismatch is
wrong about minigraf's semantics, and its hit rate means nothing.

    PYTHONHASHSEED=0 .venv/bin/python evals/at_scale/probe_lineage_cache_hit_rate.py \\
        --repo . --ref <sha> --workdir ~/.cache/probe239-cache \\
        --out evals/at_scale/results/239-lineage-cache-hit-rate.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

WATCHED = (":introduced-by", ":modified-in", ":entity", ":ident")

_QUERY_RE = re.compile(
    r"^\(query \[:find \?(\w+) :where \[(\S+) (:[\w\-/.]+) \?\1\]\]\)$")
_TRANSACT_RE = re.compile(
    r'^\(transact \{:valid-from "([^"]+)"(?: :valid-to "([^"]+)")?\} (.*)\)$', re.S)
_RETRACT_RE = re.compile(r"^\(retract (.*)\)$", re.S)


def parse_point_query(datalog: str) -> Optional[Tuple[str, str]]:
    m = _QUERY_RE.match(datalog)
    if not m or m.group(3) not in WATCHED:
        return None
    return m.group(2), m.group(3)


def _parse_iso(s: str) -> float:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def simulate(events: Iterable[tuple], policy: str, scope: str) -> Dict[str, Any]:
    """Replay a recorded event log under one cache policy. Pure.

    Events: ("R", entity, attr, exec_s, frozenset(values)),
            ("W", kind, [(entity, attr, value)], valid_from_ts, valid_to_ts, wall_ts)
            ("O",)
    """
    cache: Dict[Tuple[str, str], set] = {}
    reads = hits = mismatches = 0
    read_s = hit_s = 0.0
    sample: List[Dict[str, Any]] = []
    by_attr: Dict[str, List[float]] = {}
    for ev in events:
        tag = ev[0]
        if tag == "O":
            if scope == "handle":
                cache.clear()
        elif tag == "R":
            _, e, a, dt, vals = ev
            reads += 1
            read_s += dt
            row = by_attr.setdefault(a, [0, 0, 0.0, 0.0])
            row[0] += 1
            row[2] += dt
            key = (e, a)
            if key in cache:
                hits += 1
                hit_s += dt
                row[1] += 1
                row[3] += dt
                if cache[key] != set(vals):
                    mismatches += 1
                    if len(sample) < 10:
                        sample.append({"key": list(key), "cached": sorted(map(str, cache[key])),
                                       "actual": sorted(map(str, vals))})
            cache[key] = set(vals)
        else:
            _, kind, triples, vf, vt, wall = ev
            for e, a, v in triples:
                if a not in WATCHED:
                    continue
                if not e.startswith(":"):
                    for k in [k for k in cache if k[1] == a]:
                        del cache[k]
                    continue
                key = (e, a)
                if key not in cache or policy == "never_invalidate":
                    continue
                if policy == "write_through":
                    if kind == "retract":
                        cache[key].discard(v)
                        continue
                    if vt is None and vf is not None and vf <= wall:
                        cache[key].add(v)
                        continue
                del cache[key]
    return {
        "policy": policy, "scope": scope, "reads": reads, "hits": hits,
        "hit_rate": hits / reads if reads else None,
        "read_exec_s": read_s, "hit_exec_s": hit_s,
        "mismatches": mismatches, "mismatch_sample": sample,
        "by_attr": {a: {"reads": r[0], "hits": r[1], "read_s": r[2], "hit_s": r[3]}
                    for a, r in sorted(by_attr.items())},
    }


def _value_text(v: Any) -> str:
    # minigraf returns keyword refs as ":commit/x" strings; render anything
    # else the way _parse_facts_block would have captured it.
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _arm(args: argparse.Namespace) -> int:
    if os.environ.get("PYTHONHASHSEED") != "0":
        print("refusing: PYTHONHASHSEED=0 must be set on the command line", file=sys.stderr)
        return 2
    for key in [k for k in os.environ if k.startswith("MINIGRAF_")]:
        del os.environ[key]
    workdir = pathlib.Path(args.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    for stale in workdir.glob("g.graph*"):
        stale.unlink()
    graph = workdir / "g.graph"
    os.environ["MINIGRAF_GRAPH_PATH"] = str(graph)
    sys.path.insert(0, str(_REPO_ROOT))
    import mcp_server as m

    events: List[tuple] = []
    lock = threading.Lock()
    unparsed_writes = [0]

    def recording_execute(db: Any, datalog: str) -> str:
        with m._db_native_lock:
            t0 = time.perf_counter()
            out = db.execute(datalog)
            dt = time.perf_counter() - t0
            pq = parse_point_query(datalog)
            if pq is not None:
                vals = frozenset(_value_text(r[0]) for r in json.loads(out).get("results", []))
                ev = ("R", pq[0], pq[1], dt, vals)
            elif datalog.startswith("(transact") or datalog.startswith("(retract"):
                tm = _TRANSACT_RE.match(datalog)
                rm = None if tm else _RETRACT_RE.match(datalog)
                if tm:
                    kind, body = "transact", tm.group(3)
                    vf, vt = _parse_iso(tm.group(1)), (_parse_iso(tm.group(2)) if tm.group(2) else None)
                elif rm:
                    kind, body, vf, vt = "retract", rm.group(1), None, None
                else:
                    unparsed_writes[0] += 1
                    kind, body, vf, vt = "unknown", "", None, None
                triples = [(e, a, v) for e, a, v in m._parse_facts_block(body)]
                ev = ("W", kind, triples, vf, vt, time.time())
            else:
                ev = None
            if ev is not None:
                with lock:
                    events.append(ev)
        return out

    real_open = m._open_for_lease

    def recording_open(path: str) -> Any:
        h = real_open(path)
        with lock:
            events.append(("O",))
        return h

    m._db_execute = recording_execute
    m._open_for_lease = recording_open
    m._reset_db_state()
    m._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
        "error": None, "owner_pid": None, "error_at": None, "phase": None,
    }
    started = time.monotonic()
    asyncio.run(m._run_ingestion(str(pathlib.Path(args.repo).resolve()), args.ref))
    wall = time.monotonic() - started

    # never_invalidate is the NEGATIVE control: a cache that ignores writes
    # must mismatch, or the mismatch oracle proves nothing about the others.
    results = [simulate(events, p, s)
               for p in ("invalidate", "write_through") for s in ("run", "handle")]
    results.append(simulate(events, "never_invalidate", "run"))
    out = {
        "issue": 239, "repo": args.repo, "ref": args.ref,
        "status": m._ingest_progress.get("status"),
        "wall_s": wall, "events": len(events),
        "opens": sum(1 for e in events if e[0] == "O"),
        "unparsed_writes": unparsed_writes[0],
        "policies": results,
    }
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2))
    for r in results:
        print(f"{r['policy']:14s} {r['scope']:7s} hit {r['hits']:>8}/{r['reads']:<8} "
              f"saved {r['hit_exec_s']:8.1f}s of {r['read_exec_s']:8.1f}s  "
              f"mismatches {r['mismatches']}")
    print(f"status {out['status']} wall {wall:.1f}s opens {out['opens']} "
          f"unparsed_writes {out['unparsed_writes']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if args.ref == "HEAD":
        print("refusing a literal HEAD ref (docs/design-notes.md, #330)", file=sys.stderr)
        return 2
    return _arm(args)


if __name__ == "__main__":
    sys.exit(main())
