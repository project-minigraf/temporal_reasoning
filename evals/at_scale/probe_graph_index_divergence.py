#!/usr/bin/env python3
"""#302: can anything OTHER than stderr see a graph that silently lost facts?

`stderr_capture.py` only detects corruption that PRINTS. #302 measured a fact
page garbled to `0xff` costing ~11% of the facts with zero bytes on stderr and
zero `error_signals`. This probe measures the two candidate replacements named
in that issue, on a real ingested graph, clean and corrupted:

  1. FACT COUNT. `[:find (count ?e) :where [?e ?a ?v]]` -- what the graph can
     still produce. On its own it is only a number; it needs a reference.
  2. GRAPH vs FACT INDEX. `<graph>.fts.sqlite3` is written by a different
     storage engine (SQLite) from the same triples, in the same transaction
     boundary, so it is an INDEPENDENT witness to what was written. Every
     current index row whose (entity, attribute, value) the graph can no
     longer produce is a candidate loss.

WHAT DECIDES THE DESIGN. #302's detector is meant to be a HARD GATE (exit 1 on
the nightly), so the number that matters is not the corrupted divergence -- it
is the CLEAN one. A cross-check with a nonzero, drifting clean residue cannot
gate anything without a calibrated tolerance, and a mis-calibrated tolerance on
an observational tier means recurring false red. This probe is what established
that the clean divergence is exactly zero, and it found two normalizations
needed to get there (entity space and value type); both now live in
`evals/at_scale/fact_audit.py`, which this probe calls rather than
reimplements, and both are documented there with the artifacts they cost.

CORRUPTION TARGETS. #302 records that garbling an INDEX ROOT page loses
nothing and that an earlier sweep which only hit roots wrongly concluded the
scanner was fine. Roots sit at the top of the file, so this probe garbles pages
at fractions of the way THROUGH it, and reports each target separately -- a
target that loses nothing is a fact about that page, not about the detector.

EVERY MEASUREMENT RUNS IN A FRESH SUBPROCESS, for three reasons: the
single-handle invariant (only one live `MiniGrafDb` per process) makes
measuring several graph files in one process impossible; page caching in a
process that has already read the graph would mask an on-disk garble; and the
subprocess's stderr is captured whole, which is what lets this probe reproduce
#302's "0 bytes on stderr" column rather than taking it on faith.

SELF-ISOLATING: ingests into its own tempdir, never touches memory.graph.

    .venv/bin/python evals/at_scale/probe_graph_index_divergence.py \
        --repo . --ref <sha> [--out evals/at_scale/results/302-graph-index-divergence.json]

Run with .venv/bin/python -- bare python on the development machine carries a
minigraf below this project's floor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PAGE_SIZE = 4096

# Where to garble, as a fraction of the file. Deliberately not near 0: #302's
# index roots live at the top, and a sweep that only hits them measures
# nothing (that mistake is recorded in the issue).
_CORRUPT_FRACTIONS = tuple(round(0.05 * i, 2) for i in range(2, 20))


def measure(graph_path: str, index_path: str) -> dict[str, Any]:
    """Scan one graph file and compare it against its fact index.

    Delegates to `evals.at_scale.fact_audit`, which is the SHIPPED detector --
    this probe measures the code the nightly gate actually runs, not a
    reimplementation of it that could agree with the gate by luck.

    Runs in a subprocess (see the module docstring). A failure of the scan
    itself is recorded by the audit rather than raised: a graph too damaged to
    query is a result, and a probe that dies on it reports nothing at all.
    """
    import mcp_server as m
    from evals.at_scale.fact_audit import audit_graph_against_index

    m._reset_db_state()
    m.open_db(graph_path)

    result = audit_graph_against_index(index_path)
    result["graph_path"] = graph_path
    result["graph_size_bytes"] = os.path.getsize(graph_path)
    result["graph_pages"] = os.path.getsize(graph_path) // PAGE_SIZE
    return result


def _measure_in_subprocess(graph_path: str, index_path: str) -> dict[str, Any]:
    """Run measure() in a fresh interpreter and capture its stderr whole.

    stderr_bytes and error_signals reproduce #302's own columns: the claim
    under test is that a graph can lose facts while printing nothing, so the
    printing has to be measured, not assumed.
    """
    from evals.at_scale.stderr_capture import scan_ingestion_stderr

    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--measure", graph_path,
         "--index", index_path],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ,
             "MINIGRAF_GRAPH_PATH": graph_path,
             "MINIGRAF_INDEX_PATH": index_path},
    )
    if proc.returncode != 0:
        return {
            "measure_failed": True,
            "returncode": proc.returncode,
            "stderr": proc.stderr[-4000:],
        }
    result = json.loads(proc.stdout)
    result["stderr_bytes"] = len(proc.stderr.encode())
    result["error_signals"] = scan_ingestion_stderr(proc.stderr)["error_signals"]
    result["stderr_tail"] = proc.stderr[-2000:] if proc.stderr else ""
    return result


def _corrupt_copy(src_graph: str, src_index: str, dest_dir: str, page: int) -> tuple[str, str]:
    """Copy graph+index into dest_dir and overwrite one page with 0xff.

    The `.wal` is deliberately NOT copied -- the caller checkpoints first, so
    everything is in the graph file. Copying a WAL would let a replay repair
    the garbled page and turn a real loss into a clean reading.
    """
    os.makedirs(dest_dir, exist_ok=True)
    graph = os.path.join(dest_dir, os.path.basename(src_graph))
    index = os.path.join(dest_dir, os.path.basename(src_index))
    shutil.copyfile(src_graph, graph)
    shutil.copyfile(src_index, index)
    with open(graph, "r+b") as f:
        f.seek(page * PAGE_SIZE)
        f.write(b"\xff" * PAGE_SIZE)
    return graph, index


def _ingest(repo_path: str, ref: str, graph_path: str) -> dict[str, Any]:
    import mcp_server as m

    m._reset_db_state()
    m.open_db(graph_path)
    t0 = time.perf_counter()
    asyncio.run(m._run_ingestion(repo_path, ref))
    wall = time.perf_counter() - t0
    # Checkpoint so the graph FILE holds everything: the corrupted copies below
    # take the graph without its WAL, and an un-checkpointed graph would
    # measure the WAL's absence rather than the garbled page.
    with m.db_lease() as db:
        db.checkpoint()
    m._reset_db_state()
    from evals.at_scale.commit_census import walk_claimed_from_progress

    return {
        "wall_seconds": wall,
        "commits_ingested": walk_claimed_from_progress(m._ingest_progress),
        "final_status": m._ingest_progress["status"],
    }


def report(result: dict[str, Any]) -> None:
    clean = result["clean"]
    print(f"commits ingested       {result['ingestion'].get('commits_ingested', 'reused')}")
    print(f"graph pages            {clean['graph_pages']}")
    print(f"audit seconds          {clean['audit_seconds']:.1f}")
    print()
    print(f"{'target':>16}  {'graph facts':>12} {'idx current':>12} "
          f"{'idx-graph':>10} {'graph-idx':>10} {'stderr B':>9} {'signals':>8}")
    for label, m_ in [("clean", clean)] + [(f"page {c['page']}", c) for c in result["corrupted"]]:
        if m_.get("measure_failed"):
            print(f"{label:>16}  measure FAILED rc={m_['returncode']}")
            continue
        print(f"{label:>16}  {m_['graph_facts']:>12} {m_['index_current_rows']:>12} "
              f"{m_['missing_from_graph']:>10} {m_['missing_from_index']:>10} "
              f"{m_['stderr_bytes']:>9} {len(m_['error_signals']):>8}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--measure", default=None,
                   help="Internal: measure this graph and print JSON to stdout.")
    p.add_argument("--index", default=None, help="Internal: index path for --measure.")
    p.add_argument("--repo", default=None, help="Repository whose history is ingested.")
    p.add_argument("--ref", default=None, help="Ref/SHA bounding the ingested slice.")
    p.add_argument("--graph", default=None,
                   help="Reuse an already-ingested graph instead of ingesting "
                        "(the corruption sweep is the cheap half; re-ingesting "
                        "to re-sweep is not).")
    p.add_argument("--reused-commits", type=int, default=None,
                   help="With --graph: how many commits the reused graph "
                        "holds, for the record. Not verified.")
    p.add_argument("--out", default=None, help="Write the result JSON here.")
    args = p.parse_args()

    if args.measure:
        json.dump(measure(args.measure, args.index), sys.stdout)
        return 0

    if not args.graph and (not args.repo or not args.ref):
        p.error("--repo and --ref are required unless --measure or --graph is given")

    if args.graph:
        graph_path = os.path.abspath(args.graph)
        tmpdir = tempfile.mkdtemp(prefix="probe302-")
        index_path = f"{graph_path}.fts.sqlite3"
        os.environ["MINIGRAF_GRAPH_PATH"] = graph_path
        os.environ["MINIGRAF_INDEX_PATH"] = index_path
        # --repo/--ref are recorded but NOT acted on here: they describe where
        # the reused graph came from, which is provenance this run cannot
        # otherwise state. A results file whose only note is "reused" cannot
        # be tied back to a history.
        ingestion = {
            "reused_graph": graph_path,
            "provenance": (
                f"pre-existing graph, built by an earlier run of this probe "
                f"from {args.repo} @ {args.ref}"
                if args.repo and args.ref else
                "pre-existing graph, provenance not recorded"
            ),
            **({"commits_ingested": args.reused_commits}
               if args.reused_commits else {}),
        }
    else:
        tmpdir = tempfile.mkdtemp(prefix="probe302-")
        graph_path = os.path.join(tmpdir, "bench.graph")
        index_path = f"{graph_path}.fts.sqlite3"
        os.environ["MINIGRAF_GRAPH_PATH"] = graph_path
        os.environ["MINIGRAF_INDEX_PATH"] = index_path
        ingestion = _ingest(args.repo, args.ref, graph_path)
    clean = _measure_in_subprocess(graph_path, index_path)

    pages = os.path.getsize(graph_path) // PAGE_SIZE
    corrupted = []
    for fraction in _CORRUPT_FRACTIONS:
        page = int(pages * fraction)
        dest = os.path.join(tmpdir, f"corrupt-{page}")
        cg, ci = _corrupt_copy(graph_path, index_path, dest, page)
        try:
            entry = _measure_in_subprocess(cg, ci)
        finally:
            # Deleted immediately, not at the end. Each copy is the graph plus
            # its index -- ~310 MB at at-scale size -- so keeping the whole
            # sweep on disk needs several GB of /tmp for no reason. Only the
            # measurements are wanted.
            shutil.rmtree(dest, ignore_errors=True)
        entry["page"] = page
        entry["page_fraction"] = fraction
        corrupted.append(entry)

    result = {
        "issue": 302,
        "repo": os.path.abspath(args.repo) if args.repo else None,
        "ref": args.ref,
        "ingestion": ingestion,
        "clean": clean,
        "corrupted": corrupted,
    }
    report(result)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
        print(f"\nwrote {args.out}")
    print(f"graph dir (not cleaned up, inspect or rm): {tmpdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
