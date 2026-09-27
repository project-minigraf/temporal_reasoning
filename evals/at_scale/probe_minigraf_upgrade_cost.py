#!/usr/bin/env python3
"""Does minigraf 2.0.2 remove #239's point-query cost? A same-batch A/B.

#239's spike (2026-09-19, minigraf 2.0.0, 957 commits) put per-ident point
queries -- `:introduced-by`, `:modified-in`, `:ident` liveness, lineage
markers -- at 32.6% of ingestion wall clock, and found their per-call cost
scaled with the ENTITY's history rather than the attribute's. minigraf 2.0.2's
#380 narrows a bound-entity point query `[:e :attr ?v]` to the `(e, :attr)`
EAVT range, so other attributes' history is never read -- exactly that shape.
Upstream's own residual: a heavily rewritten ATTRIBUTE still costs O(its own
history), and `:introduced-by` is rewritten by lineage reconciliation. So the
size of the effect here is measured, not assumed.

WHY ONE BATCH, INTERLEAVED. This repo has measured 2.4x drift between
invocation batches on IDENTICAL code (probe_sweep_window_cost.py), so a 2.0.2
number compared against the spike's 2.0.0 number means nothing. `batch` runs
both interpreters back to back, A B A B, and the verdict reports each arm's
own spread beside every delta so drift stays visible. The interleaving is
enforced here, not left to whoever runs it.

WHAT IS MEASURED, per arm (`arm` mode, one real `_run_ingestion`):

  * LEAF DB TIME. `_db_execute` is replaced by a timed copy that takes
    `_db_native_lock` exactly as the original does and times only
    `db.execute` INSIDE it; lock wait is recorded separately. Every call is
    bucketed by (ingestion phase, `classify(datalog)`), so the buckets are
    exhaustive over `_db_execute` and sum without double counting.
  * NAMED INCLUSIVE TIMERS on the functions the spike attributed, keyed by
    phase, so each row compares to the spike's breakdown. These NEST (a
    retract inside the sweep is in both) and are never summed.
  * HANDLE DROPS. Dropping the handle runs a full checkpoint inside
    minigraf's `Drop for Inner` (#280). minigraf#315, in 2.0.2, makes
    checkpoints copy untouched index leaves; timed here because the corpus is
    already paid for. Explicit `_db_checkpoint` calls are timed separately.
  * PARITY WITNESSES: `fact_audit` (graph vs index, zero-tolerance
    divergence) and `commit_census` (repo vs walk vs graph). An upgrade that
    changes what ingestion writes is a finding, not a speedup.

`classify` and `verdict` are pure and unit-tested
(tests/test_at_scale_minigraf_upgrade_probe.py).

THE TWO INTERPRETERS SHOULD DIFFER ONLY IN MINIGRAF. Build twin venvs from the
same base interpreter and diff their `pip freeze` before trusting a delta.
`batch` refuses two interpreters reporting the same minigraf version unless
`--allow-same-version` is passed -- which is what the positive control (one
version against itself, on a short slice) needs.

PYTHONHASHSEED=0 IS REQUIRED in every arm: several ingestion sites iterate
string sets, so hash randomization reorders writes between runs of identical
code (probe_forward_apply_write_parity.py). `batch` sets it for its children;
`arm` refuses to run without it, since setting it from inside a running
process changes nothing.

Each arm runs in its own process group and the whole group is killed when it
exits: `_run_ingestion`'s spawn-context pool leaves orphaned workers otherwise
(CLAUDE.md, #313). Put `--workroot` on a real disk, never tmpfs -- graph I/O
is part of what is measured, and every arm must share one filesystem.

    .venv/bin/python evals/at_scale/probe_minigraf_upgrade_cost.py batch \\
        --repo /path/to/repo --ref master \\
        --python /path/to/venv-2.0.0/bin/python \\
        --python /path/to/venv-2.0.2/bin/python \\
        --repeats 2 --workroot ~/.cache/probe239 \\
        --out evals/at_scale/results/239-minigraf-2.0.2-ab.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Timers on the functions #239's spike attributed, plus #336's singleton reads.
NAMED = [
    "_entity_introduced_by_values_query",
    "_entity_introduced_by_query",
    "_entity_ident_is_live",
    "_lineage_is_provisional",
    "_retract",
    "_correction_sweep_apply",
    "_reverse_apply",
    "_forward_apply",
    "_watermark_query",
    "_frontier_read_bounds",
    "_frontier_read_pos_count",
    "_db_checkpoint",
]

# B's median wall clock may exceed A's by at most this factor. Fixed before any
# data exists, so it cannot be chosen to fit the result.
NO_REGRESSION_FACTOR = 1.05

_CLAUSE = re.compile(r'\[(#uuid "[^"]*"|\S+)\s+(\S+)')


def classify(datalog: str) -> str:
    """A bounded label for one `_db_execute` command. Pure.

    `transact` / `retract` for writes, `rule` for rule registration. A query is labelled by its where-clause
    count (`q1` or `qN`), the class of its FIRST clause's entity position, and
    that clause's attribute, with `:avt` appended for `:any-valid-time`. The
    entity class, not the entity, keeps the label set bounded:
    `scan` (a variable), `uuid`, `control` (`:ingestion/...`), `lineage`
    (`:lineage/...`), `entity` (any other keyword ident). A bound-entity point
    query -- #380's shape -- is therefore `q1:entity:<attr>`.
    """
    text = datalog.lstrip()
    if text.startswith("(transact"):
        return "transact"
    if text.startswith("(retract"):
        return "retract"
    if text.startswith("(rule"):
        # SESSION_RULES, re-registered on every 0 -> 1 handle open.
        return "rule"
    if not text.startswith("(query"):
        return "other"
    _, sep, where = text.partition(":where")
    if not sep:
        return "query:unparsed"
    clauses = _CLAUSE.findall(where)
    if not clauses:
        return "query:unparsed"
    ent, attr = clauses[0]
    if ent.startswith("?"):
        cls = "scan"
    elif ent.startswith("#uuid"):
        cls = "uuid"
    elif ent.startswith(":ingestion/"):
        cls = "control"
    elif ent.startswith(":lineage/"):
        cls = "lineage"
    elif ent.startswith(":"):
        cls = "entity"
    else:
        cls = "other"
    label = f"{'q1' if len(clauses) == 1 else 'qN'}:{cls}:{attr}"
    if ":any-valid-time" in text:
        label += ":avt"
    return label


def is_point_query(label: str) -> bool:
    """The spike's category: a single-clause query on one bound code entity
    or lineage marker."""
    return label.startswith(("q1:entity:", "q1:lineage:", "q1:uuid:"))


# --------------------------------------------------------------------------
# arm mode: one instrumented ingestion under the running interpreter
# --------------------------------------------------------------------------


def _arm(args: argparse.Namespace) -> int:
    if os.environ.get("PYTHONHASHSEED") != "0":
        print("refusing: PYTHONHASHSEED=0 must be set on the command line "
              "(see module docstring)", file=sys.stderr)
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

    import fact_index
    import mcp_server as m
    from evals.at_scale.commit_census import (
        collect_commit_census,
        walk_claimed_from_progress,
    )
    from evals.at_scale.fact_audit import audit_graph_against_index

    lock = threading.Lock()
    leaf: Dict[str, List[float]] = {}   # "phase|label" -> [n, exec_s, wait_s]
    named: Dict[str, List[float]] = {}  # "phase|name"  -> [n, s]
    drops: List[float] = []

    def phase() -> str:
        return m._ingest_progress.get("phase") or "other"

    def timed_execute(db: Any, datalog: str) -> str:
        t_wait = time.perf_counter()
        with m._db_native_lock:
            t0 = time.perf_counter()
            out = db.execute(datalog)
            t1 = time.perf_counter()
        key = f"{phase()}|{classify(datalog)}"
        with lock:
            row = leaf.setdefault(key, [0, 0.0, 0.0])
            row[0] += 1
            row[1] += t1 - t0
            row[2] += t0 - t_wait
        return out

    def wrap(name: str, fn: Any) -> Any:
        def spy(*a: Any, **k: Any) -> Any:
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                dt = time.perf_counter() - t
                with lock:
                    row = named.setdefault(f"{phase()}|{name}", [0, 0.0])
                    row[0] += 1
                    row[1] += dt
        return spy

    m._db_execute = timed_execute
    for name in NAMED:
        setattr(m, name, wrap(name, getattr(m, name)))

    # Instance-level patch: safe only because this process exits after one
    # run (#272 -- never copy into tests). Count read BEFORE release, since a
    # 1 -> 0 drop is indistinguishable from 2 -> 1 afterwards.
    real_release = m._lease_manager.release

    def release() -> None:
        will_drop = m._lease_manager.lease_count == 1
        t = time.perf_counter()
        real_release()
        if will_drop:
            drops.append(time.perf_counter() - t)

    m._lease_manager.release = release

    m._reset_db_state()
    m._ingest_progress = {
        "status": "idle", "total": 0, "prior_ingested": 0, "current_commit": "",
        "error": None, "owner_pid": None, "error_at": None, "phase": None,
    }
    started = time.monotonic()
    asyncio.run(m._run_ingestion(args.repo, args.ref))
    wall = time.monotonic() - started

    final_status = m._ingest_progress.get("status")
    walk_claimed = walk_claimed_from_progress(m._ingest_progress)
    m._lease_manager.release = real_release
    graph_bytes = graph.stat().st_size if graph.exists() else None

    audit = audit_graph_against_index(
        fact_index.index_path_for(str(graph)), expected_graph_path=str(graph)
    )

    async def census() -> dict:
        async with m.db_lease_async() as db:
            return collect_commit_census(
                repo_path=args.repo, ref=args.ref, walk_claimed=walk_claimed,
                db=db, final_status=final_status,
            )

    cen = asyncio.run(census())
    m._reset_db_state()

    def entities(sub: Any) -> Any:
        return sub.get("entities") if isinstance(sub, dict) else sub

    k = len(drops) // 3
    out = {
        "minigraf_version": md.version("minigraf"),
        "python_version": sys.version.split()[0],
        "repo": args.repo,
        "ref": args.ref,
        "wall_s": wall,
        "graph_bytes": graph_bytes,
        "leaf": {key: {"n": v[0], "exec_s": v[1], "wait_s": v[2]}
                 for key, v in sorted(leaf.items())},
        "named": {key: {"n": v[0], "s": v[1]} for key, v in sorted(named.items())},
        "drops": {
            "n": len(drops),
            "total_s": sum(drops),
            "thirds_mean_s": (
                [statistics.fmean(drops[:k]), statistics.fmean(drops[k:2 * k]),
                 statistics.fmean(drops[2 * k:])] if k else None
            ),
        },
        "parity": {
            "final_status": final_status,
            "graph_facts": audit.get("graph_facts"),
            "graph_distinct_entities": audit.get("graph_distinct_entities"),
            "index_current_rows": audit.get("index_current_rows"),
            "divergence": audit.get("divergence"),
            "audit_error": audit.get("audit_error"),
            "introduced_by_duplicates": entities(audit.get("introduced_by_duplicates")),
            "entities_without_introduced_by": entities(
                audit.get("entities_without_introduced_by")),
            # #316's denominator, here as this probe's positive control: an
            # arm with no tree-sitter grammars writes no code entities, so it
            # issues none of the point queries under test -- and two such
            # arms agree perfectly on parity while measuring nothing.
            "code_entities_scanned": (
                audit.get("entities_without_introduced_by") or {}
            ).get("code_entities_scanned"),
            "repo_commits": cen.get("repo_commits"),
            "graph_commit_entities": cen.get("graph_commit_entities"),
            "census_ok": cen.get("ok"),
            "census_error": cen.get("census_error"),
        },
    }
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2))
    if not args.keep_graph:
        for f in workdir.glob("g.graph*"):
            f.unlink()
    return 0


# --------------------------------------------------------------------------
# verdict: pure, over the per-arm results
# --------------------------------------------------------------------------

# Fields that must be identical across every run of a batch. Wall-clock-free
# by construction: these describe what was WRITTEN, not how fast.
PARITY_FIELDS = [
    "final_status", "graph_facts", "graph_distinct_entities",
    "index_current_rows", "introduced_by_duplicates",
    "entities_without_introduced_by", "code_entities_scanned", "repo_commits",
    "graph_commit_entities",
]


def _spread(xs: List[float]) -> Optional[float]:
    """(max - min) / median, the within-arm drift beside every delta."""
    if len(xs) < 2:
        return None
    med = statistics.median(xs)
    return (max(xs) - min(xs)) / med if med else None


def _compare(a: List[float], b: List[float]) -> Dict[str, Any]:
    ma = statistics.median(a) if a else 0.0
    mb = statistics.median(b) if b else 0.0
    return {
        "a": a, "b": b, "a_median": ma, "b_median": mb,
        "b_over_a": (mb / ma) if ma else None,
        "a_spread": _spread(a), "b_spread": _spread(b),
    }


def verdict(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare arm "A" runs against arm "B" runs. Pure.

    `runs` items carry `arm` ("A"/"B") and an `arm` mode result under
    `result`. `parity_ok` requires every PARITY_FIELDS value identical across
    ALL runs, both arms, plus a clean audit (divergence 0, no audit_error),
    a passing census and a nonzero code-entity denominator in each.
    `no_regression` requires B's median wall clock within
    NO_REGRESSION_FACTOR of A's. Bucket deltas are reported, never gated.
    """
    by = {"A": [r["result"] for r in runs if r["arm"] == "A"],
          "B": [r["result"] for r in runs if r["arm"] == "B"]}
    problems: List[str] = []
    fingerprints = {
        json.dumps({f: r["parity"].get(f) for f in PARITY_FIELDS}, sort_keys=True)
        for r in by["A"] + by["B"]
    }
    if len(fingerprints) != 1:
        problems.append(f"{len(fingerprints)} distinct parity fingerprints")
    for arm, results in by.items():
        for i, r in enumerate(results):
            p = r["parity"]
            if p.get("divergence") != 0:
                problems.append(f"{arm}[{i}] divergence {p.get('divergence')}")
            if p.get("audit_error") is not None:
                problems.append(f"{arm}[{i}] audit_error {p.get('audit_error')}")
            if p.get("census_ok") is not True:
                problems.append(f"{arm}[{i}] census_ok {p.get('census_ok')}")
            if not p.get("code_entities_scanned"):
                problems.append(f"{arm}[{i}] no code entities "
                                f"({p.get('code_entities_scanned')}): measured nothing")
    if not by["A"] or not by["B"]:
        problems.append("an arm has no runs")

    def series(results: List[Dict[str, Any]], fn: Any) -> List[float]:
        return [fn(r) for r in results]

    def leaf_sum(r: Dict[str, Any], pred: Any) -> float:
        return sum(v["exec_s"] for k, v in r["leaf"].items()
                   if pred(k.split("|", 1)[1]))

    labels = sorted({k for r in by["A"] + by["B"] for k in r["leaf"]})
    names = sorted({k for r in by["A"] + by["B"] for k in r["named"]})
    wall = _compare(series(by["A"], lambda r: r["wall_s"]),
                    series(by["B"], lambda r: r["wall_s"]))
    point = _compare(
        series(by["A"], lambda r: leaf_sum(r, is_point_query)),
        series(by["B"], lambda r: leaf_sum(r, is_point_query)))

    def share(results: List[Dict[str, Any]]) -> List[float]:
        return [leaf_sum(r, is_point_query) / r["wall_s"] for r in results]

    return {
        "versions": {arm: sorted({r["minigraf_version"] for r in rs})
                     for arm, rs in by.items()},
        "parity_ok": not problems,
        "parity_problems": problems,
        "no_regression_factor": NO_REGRESSION_FACTOR,
        # Both medians must exist: an arm with no runs has median 0, which
        # would otherwise read as the fastest possible B.
        "no_regression": bool(
            wall["a_median"] and wall["b_median"]
            and wall["b_median"] <= NO_REGRESSION_FACTOR * wall["a_median"]),
        "wall_s": wall,
        "point_query_exec_s": point,
        "point_query_share_of_wall": _compare(share(by["A"]), share(by["B"])),
        "db_exec_total_s": _compare(
            series(by["A"], lambda r: leaf_sum(r, lambda _l: True)),
            series(by["B"], lambda r: leaf_sum(r, lambda _l: True))),
        "drops_total_s": _compare(
            series(by["A"], lambda r: r["drops"]["total_s"]),
            series(by["B"], lambda r: r["drops"]["total_s"])),
        "leaf_exec_s": {
            k: _compare(
                series(by["A"], lambda r, k=k: r["leaf"].get(k, {}).get("exec_s", 0.0)),
                series(by["B"], lambda r, k=k: r["leaf"].get(k, {}).get("exec_s", 0.0)))
            for k in labels
        },
        "named_s": {
            k: _compare(
                series(by["A"], lambda r, k=k: r["named"].get(k, {}).get("s", 0.0)),
                series(by["B"], lambda r, k=k: r["named"].get(k, {}).get("s", 0.0)))
            for k in names
        },
    }


# --------------------------------------------------------------------------
# batch mode: interleaved subprocess arms
# --------------------------------------------------------------------------


def _child_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGRAF_")}
    env["PYTHONHASHSEED"] = "0"
    return env


def _minigraf_version(python: str) -> str:
    return subprocess.check_output(
        [python, "-c",
         "import importlib.metadata as m; print(m.version('minigraf'))"],
        env=_child_env(), text=True).strip()


def _run_arm(python: str, repo: str, sha: str, workdir: pathlib.Path) -> Dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "result.json"
    out.unlink(missing_ok=True)
    cmd = [python, str(pathlib.Path(__file__).resolve()), "arm", "--repo", repo,
           "--ref", sha, "--workdir", str(workdir), "--out", str(out)]
    with open(workdir / "console.log", "w") as log:
        proc = subprocess.Popen(cmd, env=_child_env(), stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                cwd=str(_REPO_ROOT))
        try:
            rc = proc.wait()
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if rc != 0 or not out.exists():
        raise RuntimeError(f"arm failed (rc={rc}); see {workdir / 'console.log'}")
    return json.loads(out.read_text())


def _batch(args: argparse.Namespace) -> int:
    if len(args.python) != 2:
        print("need exactly two --python interpreters (A then B)", file=sys.stderr)
        return 2
    if args.ref == "HEAD":
        print("refusing a literal HEAD ref (CLAUDE.md, #330)", file=sys.stderr)
        return 2
    sha = subprocess.check_output(
        ["git", "-C", args.repo, "rev-parse", "--verify", f"{args.ref}^{{commit}}"],
        text=True).strip()
    pa, pb = args.python
    va, vb = _minigraf_version(pa), _minigraf_version(pb)
    if va == vb and not args.allow_same_version:
        print(f"refusing: both interpreters report minigraf {va}", file=sys.stderr)
        return 2
    root = pathlib.Path(args.workroot).expanduser()
    runs = []
    for rep in range(args.repeats):
        for arm, py in (("A", pa), ("B", pb)):
            wd = root / f"{rep}-{arm}"
            t = time.strftime("%H:%M:%S")
            print(f"[{t}] rep {rep} arm {arm} ({py})", flush=True)
            res = _run_arm(py, args.repo, sha, wd)
            print(f"    wall {res['wall_s']:.1f}s  minigraf {res['minigraf_version']}",
                  flush=True)
            runs.append({"rep": rep, "arm": arm, "python": py, "result": res})
    out = {
        "issue": 239,
        "repo": args.repo, "ref": args.ref, "sha": sha,
        "order": [f"{r['rep']}-{r['arm']}" for r in runs],
        "verdict": verdict(runs),
        "runs": runs,
    }
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2))
    v = out["verdict"]
    print(json.dumps({k: v[k] for k in (
        "versions", "parity_ok", "parity_problems", "no_regression")}, indent=2))
    for key in ("wall_s", "db_exec_total_s", "point_query_exec_s",
                "point_query_share_of_wall", "drops_total_s"):
        c = v[key]
        print(f"{key:28s} A {c['a_median']:10.3f}  B {c['b_median']:10.3f}  "
              f"B/A {c['b_over_a'] if c['b_over_a'] is None else round(c['b_over_a'], 3)}"
              f"  spread A {c['a_spread']}  B {c['b_spread']}")
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
    b = sub.add_parser("batch", help="interleaved A/B over two interpreters")
    b.add_argument("--repo", required=True)
    b.add_argument("--ref", required=True)
    b.add_argument("--python", action="append", required=True)
    b.add_argument("--repeats", type=int, default=2)
    b.add_argument("--workroot", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--allow-same-version", action="store_true")
    args = p.parse_args(argv)
    return _arm(args) if args.mode == "arm" else _batch(args)


if __name__ == "__main__":
    sys.exit(main())
