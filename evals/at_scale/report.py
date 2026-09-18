"""JSON and Markdown report writers for the at-scale benchmark tier (#120)."""

from __future__ import annotations

import sys
import datetime
import json
from pathlib import Path
from typing import Any, Optional

_REPORT_HEADER = "# At-Scale Code-Graph Benchmark\n\nSee issue #120 and `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.\nObservational only -- no pass/fail thresholds.\n"


def _utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _relative_to_report(path: Any, report_path: Path) -> str:
    """Render an artifact path relative to the report's own directory.

    benchmark.md lives at evals/at_scale/benchmark.md and the artifacts it
    names live at evals/at_scale/results/, so the useful rendering is
    `results/ingestion-<ts>.json`. Falls back to the absolute path when the
    artifact is not under the report's directory -- an artifact copied off a
    run host, or a tmp_path under test -- because a `../../../tmp/...` chain
    is worse than an absolute path for a human reader.

    Both sides are resolved first so a symlinked temp root (macOS's
    /var -> /private/var) does not defeat the relative case.
    """
    resolved = Path(path).resolve()
    base = Path(report_path).resolve().parent
    try:
        return str(resolved.relative_to(base))
    except ValueError:
        return str(resolved)


def _artifact_bullet(label: str, json_path: Any, report_path: Path) -> str:
    """The "- <label>:" provenance bullet (#276), parameterised on the label
    so callers whose artifact is not a benchmark metrics JSON (#260's probe
    artifact, e.g.) can say so rather than mislabel it.

    ALWAYS emitted, for the reason _poll_duty_row is: an omitted line is
    invisible, so a reader could not tell a harness that did not record the
    path from one that was never asked to.
    """
    if json_path is None:
        return f"- {label}: not recorded (this harness did not write one)"
    return f"- {label}: `{_relative_to_report(json_path, report_path)}`"


def _metrics_json_bullet(json_path: Any, report_path: Path) -> str:
    return _artifact_bullet("Metrics JSON", json_path, report_path)


def write_json_result(metrics: dict[str, Any], results_dir: Path, prefix: str = "ingestion") -> Path:
    """Write metrics as machine-readable JSON to results_dir/<prefix>-<ts>.json."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{prefix}-{_utc_timestamp()}.json"
    path.write_text(json.dumps(metrics, indent=2) + "\n")
    return path


def _poll_duty_row(metrics: dict[str, Any]) -> str:
    """The "Poll duty cycle" row, rendered whether or not the metrics carry it.

    The row is ALWAYS emitted: benchmark.md's own note tells the reader to
    treat a missing duty cycle as "unmeasured, assume inflated", which only
    works if the absence is visible. Reading metrics['poll_duty_fraction']
    unconditionally instead raised KeyError on any result JSON produced before
    2026-08-07 (#242), making every pre-fix artifact un-re-renderable -- the
    opposite of the note's intent.
    """
    fraction = metrics.get("poll_duty_fraction")
    if fraction is None:
        return (
            "| Poll duty cycle (#242) | not measured "
            "(pre-2026-08-07 harness; assume inflated) |"
        )
    return (
        f"| Poll duty cycle (#242) | {fraction*100:.2f}% "
        f"over {metrics.get('poll_count', 'unknown')} polls |"
    )


def _checkpoint_duty_row(metrics: dict[str, Any]) -> str:
    """The "Checkpoint duty cycle" row (#241), rendered the same defensive
    way _poll_duty_row is: metrics from a pre-#241 run simply lack
    'checkpoint_summary', and re-rendering that JSON must not raise.

    A run that failed before checkpointing is reported as such rather than as
    a duty figure, whether it left no summary or an all-zeros one -- see the
    #270 comment below for why both shapes exist and why the second one
    appeared only once that window was closed.
    """
    summary = metrics.get("checkpoint_summary")
    ingest_error = metrics.get("ingest_error")
    # #270: a failed run that never checkpointed must SAY so, and it can now
    # arrive in either of two shapes -- so this is tested before the summary
    # is, not only when the summary is missing.
    #
    # It used to arrive only as an absent summary: _run_ingestion publishes
    # from two `finally` blocks, both guarded on its _CheckpointPolicy being
    # non-None, and the policy was constructed ~100 lines into the try, so a
    # run dying in the preload published nothing. #270 moved the construction
    # to the first statement in the try, which closes that window -- and
    # therefore makes the SAME run render an all-zeros summary instead. Left
    # to the branch below that is "0.00% over 0 checkpoints (0.00s total, 0
    # suppressed)": a formatted measurement, in benchmark.md, for a run that
    # measured nothing.
    #
    # Keyed on `checkpoints == 0` rather than on ingest_error alone. A run
    # that failed in Stage B after checkpointing 40 times has a real duty
    # figure and keeps it; only a zeros summary is uninformative enough that
    # naming the error is strictly more useful than rendering it.
    if ingest_error and (summary is None or summary.get("checkpoints", 0) == 0):
        return (
            "| Checkpoint duty cycle (#241) | not measured -- the run "
            f"failed before it checkpointed: {ingest_error} |"
        )
    if summary is None:
        # No ingest_error, so this is a metrics JSON from a harness that
        # predates the key: "assume once-per-commit cadence" describes the
        # pre-#241 code, and would be a false claim about any run whose own
        # code did install a policy.
        return (
            "| Checkpoint duty cycle (#241) | not measured "
            "(pre-2026-08-08 harness; assume once-per-commit cadence) |"
        )
    return (
        f"| Checkpoint duty cycle (#241) | {summary['realised_duty']*100:.2f}% "
        f"over {summary['checkpoints']} checkpoints "
        f"({summary['total_seconds']:.2f}s total, "
        f"{summary['suppressed']} suppressed) |"
    )


def _stderr_tee_row(metrics: dict[str, Any]) -> str:
    """The "Stderr tee" row (#256).

    Renders the MEASUREMENT BASIS, not a result. As of 2026-08-16 the run is
    wrapped in an fd-level tee, so wall-clock, throughput and the latency
    series in this table were all measured with a pump thread copying every
    fd-2 write. Every earlier entry in benchmark.md was measured without one,
    and nothing else in this table marks which is which -- so a reader
    comparing a new row against an old one has no way to see that the
    instrument changed.

    Presence of 'stderr_capture_complete' is the discriminator: only the
    post-#256 harness emits it, on both the clean and the failed path.
    """
    if "stderr_capture_complete" not in metrics:
        return (
            "| Stderr tee (#256) | none (pre-2026-08-16 harness; "
            "wall-clock and latencies measured with no tee active) |"
        )
    return (
        "| Stderr tee (#256) | active (wall-clock and latencies measured "
        "with an fd-level tee in place) |"
    )


def _stderr_capture_row(metrics: dict[str, Any]) -> str:
    """The "Stderr capture" row (#256), rendered the same defensive way
    _poll_duty_row is.

    This row governs how the two rows below it may be read. On an incomplete
    capture the dropped-commit and error-signal counts are LOWER BOUNDS: the
    tee stopped collecting at some unknown point, so "0 dropped" means
    "none seen", not "none happened". Without this row, benchmark.md renders a
    truncated run as visually identical to a clean one -- which is the exact
    ambiguity #256 exists to remove, and the one the #255 acceptance claim
    rested on.
    """
    complete = metrics.get("stderr_capture_complete")
    if complete is None:
        return (
            "| Stderr capture (#256) | not measured "
            "(pre-2026-08-16 harness; dropped commits were never captured) |"
        )
    if complete:
        return "| Stderr capture (#256) | complete |"
    detail = metrics.get("tee_failure", "reason not recorded")
    return (
        f"| Stderr capture (#256) | **INCOMPLETE** -- the rows below are LOWER "
        f"BOUNDS, not counts: `{detail}` |"
    )


def _skipped_commits_row(metrics: dict[str, Any]) -> str:
    """The "Commits dropped" row (#256).

    Not derivable from any other row in this table. _run_ingestion isolates a
    per-commit failure rather than propagating it and increments `processed`
    anyway, so "Final status | complete" and "Commits ingested | N" are both
    blind to a dropped commit.
    """
    skipped = metrics.get("skipped_commits")
    if skipped is None:
        return (
            "| Commits dropped (#256) | not measured "
            "(pre-2026-08-16 harness; assume unknown, NOT zero) |"
        )
    if not skipped:
        return "| Commits dropped (#256) | 0 |"
    shown = ", ".join(f"`{sha}`" for sha in skipped[:5])
    more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    return f"| Commits dropped (#256) | **{len(skipped)}**: {shown}{more} |"


def _error_signals_row(metrics: dict[str, Any]) -> str:
    """The "Error signatures" row (#256): #251 corruption signatures plus the
    tee's own pump-failure marker, as counted by scan_ingestion_stderr."""
    signals = metrics.get("error_signals")
    if signals is None:
        return (
            "| Error signatures (#251/#256) | not measured "
            "(pre-2026-08-16 harness; assume unknown, NOT zero) |"
        )
    if not signals:
        return "| Error signatures (#251/#256) | 0 |"
    names = sorted({s.get("pattern", "unknown") for s in signals})
    return (
        f"| Error signatures (#251/#256) | **{len(signals)}**: "
        f"{', '.join(names)} |"
    )


def _fact_audit_row(metrics: dict[str, Any]) -> str:
    """The "Fact-index divergence" row (#302).

    Deliberately adjacent to the "Error signatures" row, because the two are
    easy to confuse and only one of them can see a silent loss. A `0` there
    means nothing was PRINTED; a `0` here means the graph still produces every
    fact its own index witnessed. #302 measured ~11% of a graph vanishing with
    the first reading 0 throughout.
    """
    audit = metrics.get("fact_audit")
    if audit is None:
        return (
            "| Fact-index divergence (#302) | not measured "
            "(pre-2026-09-01 harness; a silent loss would not have been seen) |"
        )
    if audit.get("audit_error"):
        return (
            f"| Fact-index divergence (#302) | **UNVERIFIED** -- the audit "
            f"could not run: `{audit['audit_error']}` |"
        )
    # No "excluded" note any more: #303 taught the index to hold
    # boolean-valued facts, so nothing is set aside and every fact the graph
    # produces is cross-checked. A count that is always zero would read like a
    # covered case while covering nothing -- see fact_audit.py's docstring.
    divergence = audit.get("divergence", 0)
    if not divergence:
        return (
            f"| Fact-index divergence (#302) | 0 "
            f"({audit.get('graph_facts', '?')} facts cross-checked) |"
        )
    return (
        f"| Fact-index divergence (#302) | **{divergence}**: "
        f"{audit.get('missing_from_graph', '?')} in the index but not the graph, "
        f"{audit.get('missing_from_index', '?')} in the graph but not the index |"
    )


def _commit_census_row(metrics: dict[str, Any]) -> str:
    """The "Commits: repo / walk / graph" row (#317).

    Its own row, beside the fact-audit rows rather than inside them, because
    it is the only content check here whose reference is not the graph itself.
    A commit that never reached the graph is absent from the fact index in
    exactly the same way -- the two witnesses agree perfectly about a graph
    that lost history -- so nothing above this row can report it.

    The clean rendering carries all THREE counts, not a bare tick. `847 / 847
    / 847` says which of the three numbers a future run moved; a single "0
    divergence" would not, and three counts that are all zero also agree
    perfectly. Same argument as the orphan row's denominator, and the same
    reason it is restated on every run rather than once in a results file.

    "not measured" is for a metrics file written before this census existed.
    Absence is not zero, and such a graph was never asked.
    """
    census = metrics.get("commit_census")
    if census is None:
        return (
            "| Commits: repo / walk / graph (#317) | not measured "
            "(pre-2026-09-02 harness; this run was never asked) |"
        )
    if census.get("census_error"):
        return (
            f"| Commits: repo / walk / graph (#317) | **UNVERIFIED** -- the "
            f"census could not run: `{census['census_error']}` |"
        )
    counts = (
        f"{census.get('repo_commits')} / {census.get('walk_claimed')} / "
        f"{census.get('graph_commit_entities')}"
    )
    if census.get("proved_nothing"):
        # Both halves are 0. Reported as a non-result rather than a pass, on
        # the same terms as the orphan row: the census ran and found nothing
        # to census, which is what a broken ref looks like as well as what an
        # empty repo looks like. The gate does not fail it.
        return (
            "| Commits: repo / walk / graph (#317) | 0 / 0 / 0 -- the repo "
            "holds **no commits**, so this census proved nothing about the "
            "graph |"
        )
    if census.get("ok"):
        ref = census.get("ref")
        suffix = "" if ref is None else f" (ref `{ref}`)"
        return f"| Commits: repo / walk / graph (#317) | {counts}{suffix} |"
    return (
        f"| Commits: repo / walk / graph (#317) | **{counts}** -- "
        f"{census.get('interpretation', 'census failed')} |"
    )


def _orphaned_commits_row(metrics: dict[str, Any]) -> str:
    """The "Orphaned commit entities" row (#222 phase 5, gate clause 9).

    Its own row rather than part of the census row above: an orphan is a graph
    holding MORE than the repo, which matches none of the census's three delta
    diagnoses, so that row reads clean over a graph that kept rewritten history.

    Same idioms as the #316 row. The clean rendering carries the DENOMINATOR
    (`0 of 957`), never a bare 0. And `entities` is never rendered without
    `proved_nothing`: when the recorded branch is absent or is not the audited
    ref, the count is still computed and shipped, but it may be another
    ingested branch's legitimate history, so it is shown as uninterpretable
    rather than as findings or as clean. The gate does not fail it.

    "not measured" is for a metrics file from a harness predating this check,
    and "UNVERIFIED" for a census that could not gather its inputs -- in which
    case the sets it did hold are partial, so no number is quoted.
    """
    label = "| Orphaned commit entities (#222 phase 5) |"
    census = metrics.get("commit_census")
    if census is None or "orphaned_commits" not in census:
        return (
            f"{label} not measured (pre-2026-09-14 harness; this graph was "
            f"never asked) |"
        )
    if census.get("census_error"):
        return (
            f"{label} **UNVERIFIED** -- the census could not gather its "
            f"inputs: `{census['census_error']}` |"
        )
    orphans = census["orphaned_commits"] or {}
    entities = orphans.get("entities", 0)
    scanned = orphans.get("commit_entities_scanned", 0)
    recorded = orphans.get("recorded_branch")
    audited = orphans.get("audited_ref")
    if orphans.get("proved_nothing"):
        if not scanned:
            why = "0 commit entities were scanned -- this graph holds none"
        elif recorded is None:
            why = "the graph records no `:ingestion/branch`"
        else:
            why = (
                f"the graph was ingested against `{recorded}`, not the audited "
                f"`{audited}`, so commits outside `{audited}` may be another "
                f"branch's real history"
            )
        return (
            f"{label} {entities} of {scanned} absent from `{audited}`, but "
            f"**{why}**, so the check proved nothing about it (not gated) |"
        )
    if not entities:
        return f"{label} 0 of {scanned} (ref `{audited}`) |"
    sample = ", ".join(f"`{h}`" for h in orphans.get("sample", []))
    return (
        f"{label} **{entities}** of {scanned} hold a hash `{audited}` no longer "
        f"contains -- this graph must be **rebuilt into a fresh graph path**, "
        f"not repaired or re-ingested in place"
        + (f". e.g. {sample}" if sample else "")
        + " |"
    )


def _introduced_by_duplicates_row(metrics: dict[str, Any]) -> str:
    """The "Duplicate :introduced-by" row (#287).

    Adjacent to the fact-index row, and NOT part of it, because #287's shape
    is the one the row above cannot see: both values are faithfully in the
    index too, so the two witnesses agree perfectly about a graph that is
    wrong. A run can read `divergence | 0` and be condemned.

    "not measured" covers two different pasts and says the same true thing
    about both -- a metrics file with no `fact_audit` at all, and one from a
    harness that had the fact audit but not this check. Neither graph was
    ever asked, and absence is not zero.
    """
    audit = metrics.get("fact_audit")
    if audit is None or "introduced_by_duplicates" not in audit:
        return (
            "| Duplicate :introduced-by (#287) | not measured "
            "(pre-2026-09-01 harness; this graph was never asked) |"
        )
    duplicates = audit["introduced_by_duplicates"]
    if duplicates is None:
        return (
            f"| Duplicate :introduced-by (#287) | **UNVERIFIED** -- the audit "
            f"could not scan the graph: `{audit.get('audit_error', 'unknown')}` |"
        )
    entities = duplicates.get("entities", 0)
    if not entities:
        return "| Duplicate :introduced-by (#287) | 0 |"
    sample = ", ".join(
        f"`{name}` ({', '.join(values)})" for name, values in duplicates.get("sample", [])
    )
    return (
        f"| Duplicate :introduced-by (#287) | **{entities}** entities carry more "
        f"than one -- this graph must be **rebuilt into a fresh graph path**, "
        f"not repaired or re-ingested in place"
        + (f". e.g. {sample}" if sample else "")
        + " |"
    )


def _orphan_introduced_by_row(metrics: dict[str, Any]) -> str:
    """The "Code entities with no :introduced-by" row (#316).

    Adjacent to the duplicates row, and NOT part of it: #287's shape is two
    values and this one is zero, and `introduced_by_duplicates` skips anything
    holding fewer than two. It is out of the fact-index row above for the
    stronger version of that row's own caveat -- the index is missing exactly
    what the graph is missing, so the two witnesses agree perfectly about an
    entity that has no lineage at all.

    The clean rendering carries the DENOMINATOR, not a bare 0. A check that
    matched no code entities would also report 0 entities, so "0" alone is not
    evidence; "0 of 3150 code entities" is. That is the half CLAUDE.md
    requires alongside a measured baseline before a zero-tolerance gate is
    believed, and putting it in the row means every run re-states it.

    "not measured" covers two different pasts and says the same true thing
    about both -- a metrics file with no `fact_audit` at all, and one from a
    harness that had the fact audit but not this check. Neither graph was ever
    asked, and absence is not zero.
    """
    audit = metrics.get("fact_audit")
    if audit is None or "entities_without_introduced_by" not in audit:
        return (
            "| Code entities with no :introduced-by (#316) | not measured "
            "(pre-2026-09-02 harness; this graph was never asked) |"
        )
    orphans = audit["entities_without_introduced_by"]
    if orphans is None:
        return (
            f"| Code entities with no :introduced-by (#316) | **UNVERIFIED** -- "
            f"the audit could not scan the graph: "
            f"`{audit.get('audit_error', 'unknown')}` |"
        )
    entities = orphans.get("entities", 0)
    scanned = orphans.get("code_entities_scanned", 0)
    if not entities:
        if not scanned:
            # Both halves are 0. Reported as a non-result rather than a pass:
            # the check ran and found nothing to check, which is what a
            # broken narrowing looks like as well as what an ingestion-free
            # graph looks like. The gate does not fail on it -- there is
            # nothing wrong with a graph that holds no code -- but the report
            # must not call it clean.
            return (
                "| Code entities with no :introduced-by (#316) | 0, but **0 "
                "code entities were scanned** -- this graph holds none, so "
                "the check proved nothing about it |"
            )
        return (
            f"| Code entities with no :introduced-by (#316) | 0 of {scanned} "
            f"code entities |"
        )
    sample = ", ".join(f"`{name}`" for name in orphans.get("sample", []))
    return (
        f"| Code entities with no :introduced-by (#316) | **{entities}** of "
        f"{scanned} are live with no lineage -- invisible to `:as-of` "
        f"reasoning and to every lineage traversal. This graph must be "
        f"**rebuilt into a fresh graph path**, not repaired or re-ingested "
        f"in place"
        + (f". e.g. {sample}" if sample else "")
        + " |"
    )


def _residue_verdict_row(result: dict[str, Any]) -> str:
    """The "Verdict" row (#256/#276): the M <= N reading, in words.

    The numbers alone do not carry the verdict -- `M <= N` is not the
    comparison a reader would guess (equality would fail on a healthy graph,
    and so would M == 0, since a non-empty residue is the correction sweep's
    documented fail-safe). Spelling the reading out is the point of putting
    this in a human record at all.
    """
    ok = result.get("ok")
    if ok is None:
        return (
            "| Verdict (#256) | not measured "
            "(result JSON carries no `ok` key) |"
        )
    if ok:
        return (
            "| Verdict (#256) | OK -- M <= N: provisional residue is within "
            "the correction sweep's own accounting |"
        )
    return (
        "| Verdict (#256) | **FAILED** -- M > N: provisional state the sweep "
        "never accounted for (the #251 signature) |"
    )


def _residue_count_row(label: str, result: dict[str, Any], key: str) -> str:
    """One of the residue section's plain integer rows, rendered the same
    defensive way _poll_duty_row is: an absent key says so rather than
    rendering 0, which here would read as a clean measurement of an empty
    graph.
    """
    value = result.get(key)
    if value is None:
        return f"| {label} | not measured (absent from the result JSON) |"
    return f"| {label} | {value} |"


def _residue_breakdown_row(result: dict[str, Any]) -> str:
    """The per-entity-type breakdown of M.

    Three distinct states, and collapsing any two of them loses information:
    an ABSENT key is unmeasured; an EMPTY dict is a measured zero (the
    healthy case, and the common one); a populated dict names where the
    residue sits.
    """
    breakdown = result.get("breakdown_by_entity_type")
    if breakdown is None:
        return (
            "| Provisional by entity type | not measured "
            "(absent from the result JSON) |"
        )
    if not breakdown:
        return "| Provisional by entity type | none |"
    rendered = ", ".join(f"{name}: {count}" for name, count in sorted(breakdown.items()))
    return f"| Provisional by entity type | {rendered} |"


def _residue_path_row(label: str, value: Any, report_path: Path) -> str:
    """An artifact-path row. Absence renders "not recorded", never an empty
    cell, so a reader can tell a path that was not captured from one that was
    captured as blank.
    """
    if value is None:
        return f"| {label} | not recorded |"
    return f"| {label} | `{_relative_to_report(value, report_path)}` |"


def runtime_versions() -> dict[str, str]:
    """The versions that produced a run: minigraf, and the interpreter.

    Recorded because a benchmark number is meaningless without the minigraf
    version behind it (#284 item 4). minigraf's version materially changes
    ingestion cost -- the #260 per-commit handle drop, 2.0.0's ~376ms
    contended-open retry, SyncMode -- so two runs appended to benchmark.md
    without it are not comparable, and every section written before this
    existed is an unattributed number.
    """
    import importlib.metadata as md

    try:
        minigraf_version = md.version("minigraf")
    except Exception:  # noqa: BLE001 -- attribution must never fail a run
        minigraf_version = "unrecorded"
    return {
        "minigraf": minigraf_version,
        "python": ".".join(str(n) for n in sys.version_info[:3]),
    }


def append_ingestion_report(
    metrics: dict[str, Any],
    report_path: Path,
    json_path: Path | None = None,
) -> None:
    """Append a dated ingestion-run section to report_path, creating it with
    the shared header first if it doesn't exist yet.

    json_path is this run's results JSON, recorded so a reader can find the
    machine-readable artifact -- and through it the paired residue verdict
    (#276). It is a parameter rather than a metrics key because
    write_json_result only learns the path by writing the file, so folding it
    into the dict would need a second write.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Ingestion Run — {_utc_timestamp()}",
        "",
        f"- Repo: `{metrics['repo_path']}` @ `{metrics['branch']}`",
        # "unrecorded" rather than omitted: a section with no version line
        # would be indistinguishable from one written before this existed,
        # and silently reads as "the current version" to a later reader.
        f"- minigraf: `{metrics.get('minigraf_version', 'unrecorded')}`",
        _metrics_json_bullet(json_path, report_path),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Commits ingested | {metrics['commits_ingested']} |",
        f"| Final status | {metrics['final_status']} |",
        f"| Wall-clock | {metrics['wall_clock_seconds']:.2f}s |",
        f"| Throughput | {metrics['throughput_per_minute']:.1f} commits/min |",
        f"| Peak RSS | {metrics['peak_rss_kb']} KB |",
        f"| Graph size | {metrics['graph_size_bytes']} bytes |",
        f"| Fact-index size | {metrics['index_size_bytes']} bytes |",
        f"| Status-query latency (min/p50/p99/max) | "
        f"{metrics['status_latency']['min']*1000:.1f}ms / "
        f"{metrics['status_latency']['p50']*1000:.1f}ms / "
        f"{metrics['status_latency']['p99']*1000:.1f}ms / "
        f"{metrics['status_latency']['max']*1000:.1f}ms |",
        f"| Graph-query latency (min/p50/p99/max) | "
        f"{metrics['query_latency']['min']*1000:.1f}ms / "
        f"{metrics['query_latency']['p50']*1000:.1f}ms / "
        f"{metrics['query_latency']['p99']*1000:.1f}ms / "
        f"{metrics['query_latency']['max']*1000:.1f}ms |",
        _poll_duty_row(metrics),
        _checkpoint_duty_row(metrics),
        _stderr_tee_row(metrics),
        _stderr_capture_row(metrics),
        _skipped_commits_row(metrics),
        _error_signals_row(metrics),
        _fact_audit_row(metrics),
        _introduced_by_duplicates_row(metrics),
        _orphan_introduced_by_row(metrics),
        _commit_census_row(metrics),
        _orphaned_commits_row(metrics),
    ]
    if "ignore_comparison" in metrics:
        comp = metrics["ignore_comparison"]
        lines += [
            f"| Graph size with path-ignore | {comp['with_ignore_graph_size_bytes']} bytes |",
            f"| Graph size without path-ignore | {comp['without_ignore_graph_size_bytes']} bytes |",
            f"| Path-ignore bloat reduction | {comp['delta_bytes']} bytes |",
        ]
    lines.append("")

    with report_path.open("a") as f:
        f.write("\n".join(lines))


def append_residue_report(
    result: dict[str, Any],
    report_path: Path,
    json_out_path: Path | None = None,
) -> None:
    """Append a dated provisional-residue section to report_path (#276).

    Called by probe_provisional_residue.main(), which runs as a SEPARATE
    PROCESS by design -- it opens the graph with no other handle live, the
    hazard class #251/#253 came from -- so append_ingestion_report cannot
    render these numbers itself: they do not exist yet when it runs. This
    appender keeps that separation intact by consuming a plain dict, exactly
    as append_ingestion_report consumes a metrics dict; report.py learns
    nothing about the probe.

    json_out_path is the probe's own verdict JSON. It is a parameter rather
    than a `result` key because `result` is written to disk BEFORE the report
    is appended, so folding the path in would either need a second write or
    leave the on-disk artifact disagreeing with the rendered section.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Provisional Residue — {_utc_timestamp()}",
        "",
        "| Metric | Value |",
        "|---|---|",
        _residue_verdict_row(result),
        _residue_count_row("Provisional entities (M)", result, "provisional_entities"),
        _residue_count_row("Sweep skipped (N)", result, "sweep_skipped"),
        _residue_count_row("Commits in graph", result, "commits_in_graph"),
        _residue_breakdown_row(result),
        _residue_path_row("Graph", result.get("graph_path"), report_path),
        _residue_path_row("Metrics JSON", result.get("metrics_json"), report_path),
        _residue_path_row("Residue JSON", json_out_path, report_path),
        "",
    ]

    with report_path.open("a") as f:
        f.write("\n".join(lines))


def _ratio_row(label: str, value: Any) -> str:
    """A growth ratio row. None renders `not measured`, NEVER 0.00.

    An undefined ratio means the fit could not be read. Rendering it as a
    number -- especially a small one -- would make a failed measurement read
    as a flat result, which is the exact defect #276 was filed about.

    No trailing newline: every other row helper in this file returns a plain
    line and relies on the caller's "\n".join(lines), and a baked-in \n here
    would double it.
    """
    if value is None:
        return f"- {label}: not measured"
    return f"- {label}: {float(value):.2f}x"


def _trace_fit_control_gate_row(control_gate: Any) -> str:
    """The control gate's own pass/fail reading, spelled out the same way
    _residue_verdict_row spells out M <= N: the raw growth number alone does
    not carry whether it cleared CONTROL_MIN_GROWTH.

    An absent or empty control_gate renders "not measured", never
    **FAILED** -- {}.get("passed") is falsy the same way an actual failure
    is, and treating the two alike would conflate "never measured" with
    "measured and failed", the same absence-as-a-result hole _ratio_row
    exists to close on the ratio axis. Not reachable through today's only
    producer (trace_fit.analyse always populates control_gate), but a result
    dict handed to this function by any other caller -- or an older/partial
    one -- must not be able to manufacture a FAILED reading out of a missing
    key.
    """
    if not control_gate:
        return "- Control gate: not measured (absent from the result)"
    growth = control_gate.get("growth")
    growth_str = "not measured" if growth is None else f"{float(growth):.2f}x"
    if control_gate.get("passed"):
        return f"- Control gate: passed (mean per-checkpoint duration grew {growth_str})"
    return (
        f"- Control gate: **FAILED** ({growth_str} growth) -- "
        f"{control_gate.get('reason', 'reason not recorded')}"
    )


def _fit_quality_row(label: str, fit: Optional[dict[str, Any]]) -> str:
    """One of the three per-group fit-quality rows.

    docs/superpowers/specs/2026-08-17-per-commit-cost-attribution-design.md
    (line 250) requires the INCONCLUSIVE verdict to "report both parameters
    and the fit quality" -- a ratio alone cannot distinguish a clean fit from
    one whose r^2 is near zero and cleared the growth thresholds by chance.

    fit is None when trace_fit.fit_line found the group unidentifiable (too
    few points, or zero variance in W -- see its docstring) -- rendered
    "not identifiable", never a numeric r^2, for the same reason _ratio_row
    never renders None as 0.00: a fit that could not be computed is not the
    same finding as a fit that came out flat.
    """
    if fit is None:
        return f"- {label} fit: not identifiable"
    return f"- {label} fit: n={fit['n']}, r²={fit['r2']:.3f}"


def append_trace_fit_report(
    result: dict[str, Any],
    report_path: Path,
    json_out_path: Path | None = None,
) -> None:
    """Append a dated per-commit cost-fit section to report_path (#260).

    Mirrors append_residue_report's shape and separation: probe_per_commit_cost.py
    runs the traced ingestion and calls trace_fit.analyse in a SEPARATE PROCESS,
    long after this module could have any opinion about the result, so this
    appender consumes a plain dict exactly as append_residue_report does.

    The three-state discipline (#275/#276) governs every field here: an absent
    ratio renders "not measured" via _ratio_row, never 0.00, so a fit that could
    not be read cannot be mistaken for a flat one; and a VOID verdict (the
    control gate failed open, see trace_fit.control_gate) is rendered as VOID
    and never re-derived as CONFOUNDED -- the verdict string is the analysis's
    own final word, not recomputed here from a_ratio/b_ratio.

    Per-group fit quality (n, r^2) is rendered too, per the design spec's
    "report both parameters and the fit quality" requirement for INCONCLUSIVE
    -- a ratio alone cannot show whether it came from a clean fit or a
    near-zero r^2 that happened to clear the growth thresholds.

    json_out_path is the probe's own artifact JSON, a parameter for the same
    reason it is on append_residue_report: the result dict is written to disk
    before this is called, so folding the path in would need a second write.
    Labelled "Probe artifact" rather than "Metrics JSON" (#260 M4): unlike
    append_ingestion_report's json_path, this file is not a benchmark metrics
    JSON -- it is trace_fit's analysis plus provenance -- and reusing that
    label would misdescribe it.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    control_gate = result.get("control_gate")
    group_sizes = result.get("group_sizes", {})
    fits = result.get("fits") or {}
    commits_ingested = result.get("commits_ingested")

    lines = [
        "",
        f"## Per-Commit Cost Fit — {_utc_timestamp()}",
        "",
        f"**Verdict: {result.get('verdict', 'not measured')}** -- "
        f"{result.get('verdict_reason', 'not measured')}",
        "",
        _ratio_row("a_ratio (fixed-cost growth)", result.get("a_ratio")),
        _ratio_row("b_ratio (per-unit-work-cost growth)", result.get("b_ratio")),
        _fit_quality_row("First-group", fits.get("first")),
        _fit_quality_row("Middle-group", fits.get("middle")),
        _fit_quality_row("Last-group", fits.get("last")),
        _trace_fit_control_gate_row(control_gate),
        f"- Records: {result.get('records', 'not measured')}",
        f"- Group sizes: first={group_sizes.get('first', 'not measured')}, "
        f"middle={group_sizes.get('middle', 'not measured')}, "
        f"last={group_sizes.get('last', 'not measured')}",
        # .get(key) alone, not .get(key, default): build_result sets this key
        # to an explicit None whenever metrics lacks it (the --metrics
        # re-analyse path with an older/partial file), so a .get(..., default)
        # would never fire and "None" -- outside the three-state vocabulary
        # entirely -- would print on the page.
        "- Commits ingested: "
        + ("not measured" if commits_ingested is None else str(commits_ingested)),
        _artifact_bullet("Probe artifact", json_out_path, report_path),
        "",
    ]

    with report_path.open("a") as f:
        f.write("\n".join(lines))


def _query_ingestion_block(report: dict[str, Any]) -> list[str]:
    """The ingestion-health block of a query-benchmark section (#275).

    The query benchmark ingests its OWN graph, and used to discard the
    resulting metrics -- so its section could show a clean sweep of query
    latencies measured over a graph that had silently dropped commits.

    _stderr_capture_row / _skipped_commits_row / _error_signals_row /
    _fact_audit_row / _introduced_by_duplicates_row / _orphan_introduced_by_row
    / _commit_census_row / _orphaned_commits_row are reused verbatim rather than
    re-rendered from the same keys. That is the point: an Ingestion Run section
    and a Query Correctness Run section must not be able to disagree about how
    a dirty run reads.
    """
    metrics = report.get("ingestion")
    if metrics is None:
        return [
            "",
            "Ingestion phase (#275): **not measured** -- this report carries no "
            "ingestion metrics, so nothing is known about the graph these "
            "latencies were measured over.",
        ]
    return [
        "",
        "Ingestion phase (#275) -- the graph these latencies were measured over:",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Commits ingested | {metrics.get('commits_ingested', 'not measured')} |",
        f"| Final status | {metrics.get('final_status', 'not measured')} |",
        _stderr_capture_row(metrics),
        _skipped_commits_row(metrics),
        _error_signals_row(metrics),
        _fact_audit_row(metrics),
        _introduced_by_duplicates_row(metrics),
        _orphan_introduced_by_row(metrics),
        _commit_census_row(metrics),
        _orphaned_commits_row(metrics),
    ]


def append_query_report(report: dict[str, Any], report_path: Path) -> None:
    """Append a dated query-correctness section to report_path, creating it
    with the shared header first if it doesn't exist yet.

    Takes the {"entries": [...], "ingestion": {...}} report run_query_benchmark
    returns. No back-compatibility with the bare list it used to take: query
    results have never been persisted as JSON artifacts, so unlike the
    ingestion metrics files there is no historical input to re-render -- which
    is why this function is defensive about the ingestion keys and not about
    its own argument.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Query Correctness Run — {_utc_timestamp()}",
        "",
        f"- minigraf: `{report.get('minigraf_version', 'unrecorded')}`",
        "",
        "| ID | Category | Result | minigraf latency | baseline latency |",
        "|---|---|---|---|---|",
    ]
    for r in report["entries"]:
        if r["passed"] is None:
            status = "SKIPPED (manual diff)"
        elif r["passed"]:
            status = "PASS"
        else:
            status = f"FAIL (expected `{r['expected']}`, got `{r['actual']}`)"
        lines.append(
            f"| {r['id']} | {r['category']} | {status} | "
            f"{r['minigraf_latency_seconds']*1000:.1f}ms | "
            f"{r['baseline_latency_seconds']*1000:.1f}ms |"
        )
    lines += _query_ingestion_block(report)
    lines.append("")

    with report_path.open("a") as f:
        f.write("\n".join(lines))
