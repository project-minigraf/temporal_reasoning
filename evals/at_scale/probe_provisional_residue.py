"""#256: cross-check a surviving graph's provisional residue against the
correction sweep's own accounting.

A SEPARATE PROCESS by design. It opens the persisted graph with no other
handle live anywhere in the process -- querying in-process while the
benchmark's handle lifecycle unwinds is the exact hazard class that produced
#251/#253, and this probe exists to confirm that bug is gone.

It only ever QUERIES, but it is not a read-only probe in the sense docs/design-notes.md
uses for the `evals/at_scale/probe_*.py` convention: minigraf exposes no
read-only open, so this takes the graph's file lock and replays/compacts the
WAL like any other opener. Harmless for the disposable at-scale graph, and
stated here so nobody points it at a graph they care about believing it
cannot touch the bytes.

The comparison is `M <= N`, not `M == N`:

  M = live :type/lineage-marker entities carrying :status :provisional
  N = the correction sweep's own "left provisional/unreconciled" total

N counts entities left provisional OR unreconciled. _correction_sweep_apply's
case 2 leaves an already-authoritative entity with an ambiguous
:introduced-by unreconciled without marking it provisional, so it raises N
without raising M. M is therefore a subset of N, and equality would fail on
a healthy graph -- as would asserting M == 0, since a non-empty residue is
the documented fail-safe, not a defect.

`M > N` means an entity sits provisional in the graph that the sweep never
accounted for: state left inconsistent by something other than the designed
fail-safe. That is the signature this probe detects.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def read_sweep_total(metrics: dict[str, Any]) -> int:
    """N, from the benchmark's metrics JSON.

    A missing key is fatal, not zero. Zero is a legitimate MEASURED value --
    _correction_sweep_log_summary prints only `if skipped_events:`, so an
    absent stderr line genuinely means no residue. A missing JSON key means
    something else entirely: metrics written before this instrumentation
    existed. Defaulting it to 0 would silently turn `M <= N` into `M == 0`
    and fail against a perfectly healthy graph.
    """
    if "correction_sweep_skipped" not in metrics:
        raise SystemExit(
            "metrics JSON has no 'correction_sweep_skipped' key -- it predates "
            "the #256 instrumentation. Re-run the benchmark; this probe will "
            "not guess N."
        )
    try:
        return int(metrics["correction_sweep_skipped"])
    except (TypeError, ValueError) as e:
        value = metrics["correction_sweep_skipped"]
        raise SystemExit(
            f"'correction_sweep_skipped' has invalid value {value!r} "
            f"(type: {type(value).__name__}) -- expected an integer. "
            f"Check the metrics JSON for corruption."
        )


def require_complete_run(metrics: dict[str, Any]) -> None:
    """Refuse a graph whose run did not finish, or whose stderr capture did.

    Two distinct refusals, deliberately worded differently -- they mean
    different things and a reader must be able to tell which fired:

    1. final_status != "complete". Residue on an aborted run is not evidence
       of anything.
    2. The stderr capture was incomplete (stderr_capture_complete is False, or
       a tee_failure key is present). This mirrors run_ingestion_benchmark's
       _exit_code, which already fails such a run as unverifiable, and it is
       load-bearing HERE because N comes from that same capture: a truncation
       makes N a lower bound of unknown extent, and a capture that died before
       the sweep summary was ever emitted makes N read 0. Combined with M == 0
       -- which an aborted run makes likely -- the probe would otherwise write
       `ok: true` for a run the benchmark itself already failed.

    `is False` rather than `not ...` for stderr_capture_complete, matching
    _exit_code: an ABSENT key means a pre-#256 metrics file, which
    read_sweep_total refuses on its own with a better message.
    """
    status = metrics.get("final_status")
    if status != "complete":
        raise SystemExit(
            f"run finished with final_status={status!r}, not 'complete' -- "
            f"provisional residue on an unfinished run means nothing."
        )
    if metrics.get("stderr_capture_complete") is False or "tee_failure" in metrics:
        raise SystemExit(
            f"run's stderr capture did not complete "
            f"(stderr_capture_complete="
            f"{metrics.get('stderr_capture_complete')!r}, "
            f"tee_failure={metrics.get('tee_failure')!r}) -- N was scanned "
            f"from that truncated capture, so it is a lower bound of unknown "
            f"extent and may read 0 simply because the capture died before "
            f"the sweep summary. `M <= N` against it proves nothing. Re-run "
            f"the benchmark."
        )


def breakdown_by_entity_type(idents: Sequence[str]) -> dict[str, int]:
    """Count provisional entities per entity-type namespace.

    The type is taken from the ident's own namespace (":function/foo" ->
    "function"), which is how idents are constructed, rather than a second
    query per entity.
    """
    return dict(Counter(ident.lstrip(":").split("/", 1)[0] for ident in idents))


def residue_verdict(m: int, n: int) -> dict[str, Any]:
    """Apply `M <= N` and keep both raw numbers.

    Both survive in the output on purpose: `M <= N` weakens as N grows -- if
    the sweep legitimately skips thousands, M could hide real corruption
    underneath -- so a future run must be able to compare N itself across
    runs. A jump in N is its own signal.
    """
    return {
        "provisional_entities": m,
        "sweep_skipped": n,
        "ok": m <= n,
        "interpretation": (
            "M <= N: provisional residue is within the correction sweep's own "
            "accounting."
            if m <= n
            else "M > N: provisional state the sweep never accounted for -- "
                 "the #251 signature."
        ),
    }


def provisional_entity_idents(db: Any) -> list[str]:
    """Every entity currently carrying a live provisional lineage marker.

    Mirrors _lineage_is_provisional's definition (mcp_server.py:5927) --
    a :type/lineage-marker companion entity exists for the entity -- but as
    one set-returning query instead of one existence check per entity.
    _lineage_confirm retracts the marker's facts wholesale, so "marker
    present" and "provisional" are the same predicate.

    The [?m :status :provisional] clause is redundant under the current
    schema: confirm retracts the marker entity wholesale rather than
    flipping its status, so existence alone already implies provisional --
    exactly the reasoning _preload_provisional_idents' docstring
    (mcp_server.py:8650) gives for omitting this same clause, and confirmed
    by ablation here (dropping the clause did not change any test result).
    It is kept anyway as defense against a future schema where confirm
    updates :status in place instead of retracting the marker; if that ever
    happens, dropping this clause would silently start counting confirmed
    entities again.
    """
    import mcp_server

    raw = mcp_server._db_execute(
        db,
        "(query [:find ?e :where "
        f"[?m :entity-type {mcp_server._LINEAGE_MARKER_ENTITY_TYPE}] "
        "[?m :status :provisional] "
        "[?m :entity ?e]])",
    )
    return sorted(row[0] for row in json.loads(raw).get("results", []))


def require_ingested_graph(db: Any, graph_path: str) -> None:
    """Refuse a graph this probe cannot prove was written by an ingestion run
    at this code version.

    THE FAIL-OPEN THIS CLOSES: minigraf's open() CREATES the file. Before this
    guard, a typo'd --graph-path produced an empty graph, zero provisional
    entities, an empty breakdown, `ok: true` and exit 0 -- byte-for-byte the
    shape of a genuine clean result. Nothing in the artifact distinguished
    "verified a real graph" from "examined nothing".

    _graph_format_version_read, NOT _graph_format_version_verify. The verify
    half deliberately passes silently on a genuinely-new graph, because its job
    is to let ingestion ADOPT one. That is exactly the case this guard must
    catch, so reusing it would reinstate the bug. Requiring the stamp to be
    present AND equal to GRAPH_FORMAT_VERSION closes the fail-open and, for
    free, catches a graph built by a different code version -- which under
    docs/design-notes.md's no-migration rule is not a graph whose residue means anything
    to this build either.
    """
    import mcp_server

    stamped = mcp_server._graph_format_version_read(db)
    if stamped == mcp_server.GRAPH_FORMAT_VERSION:
        return
    found = (
        "no format-version stamp at all"
        if stamped is None
        else f"format version {stamped}"
    )
    raise SystemExit(
        f"{graph_path} has {found}, but this build stamps ingested graphs at "
        f"version {mcp_server.GRAPH_FORMAT_VERSION}. An UNSTAMPED graph is "
        f"almost always a graph minigraf's open() just created for you because "
        f"--graph-path pointed at nothing -- check the path for a typo. A "
        f"graph stamped at another version predates this build's ident rule "
        f"and is not comparable to its metrics. Either way this probe has "
        f"nothing to measure and will not report a verdict."
    )


def commit_entity_count(db: Any) -> int:
    """Count :type/commit entities, using the benchmark's own _STATUS_QUERY.

    Imported rather than re-typed so the two can never drift into counting
    different things -- the whole value of the cross-check is that it is the
    SAME question the benchmark answered.
    """
    import mcp_server
    from evals.at_scale.run_ingestion_benchmark import _STATUS_QUERY

    raw = mcp_server._db_execute(db, f"(query {_STATUS_QUERY})")
    results = json.loads(raw).get("results", [])
    return int(results[0][0]) if results else 0


def require_commit_count_matches(db: Any, metrics: dict[str, Any]) -> int:
    """Cross-check the graph's contents against the metrics that describe it,
    and return the count so it lands in the output JSON.

    The format-version stamp proves the graph was ingested by this build. It
    does NOT prove it was ingested by THIS RUN -- another persisted graph from
    another run is stamped identically. This is the half that makes the
    artifact self-evidencing rather than resting on the operator having typed
    the right two paths: a probe whose graph holds a different number of
    commits than the metrics claim was ingested is measuring a different run's
    residue against this run's N.

    A missing commits_ingested key is fatal for the same reason a missing
    correction_sweep_skipped is: it means a metrics file that predates this
    instrumentation, and guessing would restore the fail-open.
    """
    if "commits_ingested" not in metrics:
        raise SystemExit(
            "metrics JSON has no 'commits_ingested' key -- it predates the "
            "#256 instrumentation. Re-run the benchmark; this probe will not "
            "verify the graph against metrics it cannot read."
        )
    try:
        expected = int(metrics["commits_ingested"])
    except (TypeError, ValueError):
        raise SystemExit(
            f"'commits_ingested' has invalid value "
            f"{metrics['commits_ingested']!r} "
            f"(type: {type(metrics['commits_ingested']).__name__}) -- "
            f"expected an integer. Check the metrics JSON for corruption."
        )
    actual = commit_entity_count(db)
    if actual != expected:
        raise SystemExit(
            f"graph holds {actual} :type/commit entities but the metrics JSON "
            f"reports commits_ingested={expected}. The graph and the metrics "
            f"describe different runs, so N does not account for this graph's "
            f"residue. Check that --graph-path and --metrics-json come from "
            f"the same benchmark invocation."
        )
    return actual


def warn_on_graph_path_mismatch(metrics: dict[str, Any], graph_path: str) -> None:
    """Warn -- loudly, but do not fail -- if the metrics recorded a different
    graph path than the one this probe was handed.

    Not fatal: a persisted graph legitimately moves (copied off a run host,
    archived beside its metrics), and the commit-count cross-check above is the
    real evidence of pairing. A mismatch is still worth shouting about, because
    the ordinary cause is two runs' artifacts being crossed. An ABSENT key is
    silent: metrics files written before the path was recorded are simply older,
    not wrong.
    """
    recorded = metrics.get("graph_path")
    if recorded is None:
        return
    if str(Path(recorded)) != str(Path(graph_path).resolve()):
        print(
            f"WARNING: metrics JSON records graph_path={recorded!r} but this "
            f"probe was pointed at {str(Path(graph_path).resolve())!r}. If the "
            f"graph was moved since the run, this is expected; otherwise the "
            f"two artifacts are crossed.",
            file=sys.stderr,
        )


_DEFAULT_JSON_OUT = (
    REPO_ROOT / "evals" / "at_scale" / "results" / "256-provisional-residue.json"
)
_DEFAULT_REPORT_PATH = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-check a persisted graph's provisional residue "
                    "against the correction sweep's accounting (#256).",
    )
    parser.add_argument(
        "--graph-path", required=True,
        help="A graph produced by run_ingestion_benchmark.py --graph-path.",
    )
    parser.add_argument(
        "--metrics-json", required=True,
        help="That run's results/ingestion-<ts>.json, which carries N.",
    )
    parser.add_argument(
        "--json-out", "--output", dest="json_out", default=str(_DEFAULT_JSON_OUT),
        help="Where to write the verdict JSON. Defaults to the committed "
             "artifact path; override it so a re-run does not overwrite the "
             "recorded one.",
    )
    parser.add_argument(
        "--report-path", default=str(_DEFAULT_REPORT_PATH),
        help="benchmark.md to append the verdict section to (#276). "
             "Overridable for the same reason --json-out is: the default is "
             "a tracked file, and the tests drive main() end to end.",
    )
    args = parser.parse_args(argv)

    metrics = json.loads(Path(args.metrics_json).read_text())
    require_complete_run(metrics)
    n = read_sweep_total(metrics)
    warn_on_graph_path_mismatch(metrics, args.graph_path)

    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(args.graph_path)
    try:
        with mcp_server.db_lease() as db:
            # Both guards BEFORE the measurement: a verdict computed against a
            # graph that failed either one is not a number worth printing.
            require_ingested_graph(db, args.graph_path)
            commits_in_graph = require_commit_count_matches(db, metrics)
            idents = provisional_entity_idents(db)
    finally:
        mcp_server._reset_db_state()

    result = residue_verdict(len(idents), n)
    result["commits_in_graph"] = commits_in_graph
    # Record the inputs so the pairing of graph to metrics file is auditable
    # after the fact -- a probe run against the wrong graph or a stale JSON
    # is otherwise indistinguishable from a correct one.
    result["graph_path"] = str(Path(args.graph_path).resolve())
    result["metrics_json"] = str(Path(args.metrics_json).resolve())
    result["breakdown_by_entity_type"] = breakdown_by_entity_type(idents)
    result["correction_sweep_summaries"] = metrics.get("correction_sweep_summaries")

    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")

    # After the JSON, and only on a path that reached a verdict: every guard
    # above raises SystemExit before `result` exists, and a section rendered
    # from a refusal would be a verdict about a graph the probe declined to
    # read. A FAILING verdict is appended too -- that is the one the record
    # most needs (#276).
    from evals.at_scale.report import append_residue_report

    report_path = Path(args.report_path)
    # Mirrors the --json-out handling above: without this, a verdict that was
    # successfully computed is lost to a missing directory instead of being
    # written out.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    append_residue_report(result, report_path, out_path)

    print(json.dumps(result, indent=2))
    print(f"\nWrote {out_path}")
    print(f"Appended to {report_path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
