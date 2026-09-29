"""#260: drive a traced full-history ingestion and fit its per-commit cost.

The question: does per-commit cost grow with history length once per-commit
WORK SIZE is controlled for? #260 observed 3.09x growth across ~520 commits and
ruled out #239's point queries. The confound it names -- entities touched per
commit -- rises ~85x over this repository's history, because extraction is
whole-file, and #222's 1:1 converging streams turn that into a ~3.32x rise in
mean work per PROCESSED commit. That is the same magnitude as the observed cost
growth, which is why this needs a per-commit fit rather than another argument.

Two modes, because a 30-minute run must not have to be repeated to re-analyse:

    # measure (fresh graph, full history)
    .venv/bin/python evals/at_scale/probe_per_commit_cost.py --run \\
        --graph-path /tmp/260/g.graph --trace /tmp/260/trace.jsonl

    # re-analyse an existing trace
    .venv/bin/python evals/at_scale/probe_per_commit_cost.py \\
        --trace /tmp/260/trace.jsonl --metrics evals/at_scale/results/ingestion-*.json

USE .venv/bin/python. Bare python on the development machine has carried
minigraf 1.1.1 against this project's >=1.2.3 floor, where these queries run
~7x slower; that produced a retracted diagnosis on #239 and cost a day. The
artifact records the interpreter and the minigraf version so a reader can
check rather than trust.

The verdict logic lives in trace_fit.py and its constants are PRE-REGISTERED.
This module does I/O and provenance only -- it must not reinterpret a verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale import trace_fit  # noqa: E402

#: Pinned so the artifact describes a known interleave. The fit's
#: identifiability depends on fwd and rev commits sharing each window (see
#: trace_fit's docstring), so the ratio is part of the experiment, not an
#: incidental setting.
STREAM_RATIO = "1:1"


def read_trace(path: Any) -> list[dict]:
    """Parse a JSONL trace. A truncated FINAL line costs one record.

    An at-scale run takes ~30 minutes and the interesting ones sometimes die
    mid-write, so a half-written last record must not cost the whole trace. A
    malformed line anywhere else IS fatal -- that is corruption, not truncation,
    and silently dropping interior records would bias the fit invisibly.

    An empty trace raises. Zero records is not a verdict: "the run wrote
    nothing" and "cost was flat" are different findings and must not render
    the same.
    """
    lines = [l for l in Path(path).read_text().splitlines() if l.strip()]
    records: list[dict] = []
    for i, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                print(
                    f"[probe] dropping truncated final trace line "
                    f"({len(line)} bytes) -- run likely killed mid-write",
                    file=sys.stderr,
                )
                continue
            raise SystemExit(
                f"trace {path} has a malformed line at {i + 1} of {len(lines)}, "
                f"which is corruption rather than truncation. Refusing to fit a "
                f"trace with an interior hole."
            )
    if not records:
        raise SystemExit(
            f"trace {path} has no records. That is not a flat result -- it means "
            f"nothing was traced. Check MINIGRAF_INGEST_TRACE_PATH reached "
            f"_run_ingestion."
        )
    return records


def _minigraf_version() -> str:
    try:
        from importlib.metadata import version
        return version("minigraf")
    except Exception as e:
        return f"unknown ({e})"


#: Metrics-JSON keys carried through verbatim when present (I2). Absent stays
#: absent -- a metrics dict from an older/partial run genuinely lacks these,
#: and coercing a missing key to 0 or False would read as "measured clean",
#: the exact #275/#276 defect class this mirrors. error_signals matters most:
#: it is where a `Page N out of bounds` signature (#251) would show up, and a
#: run that built a multi-hundred-MB graph with no such record on disk is
#: silently unauditable without it.
_CARRIED_METRICS_KEYS = (
    "skipped_commits",
    "error_signals",
    "stderr_capture_complete",
    "poll_duty_fraction",
    "checkpoint_summary",
    # #270. For a run that failed before Stage A, checkpoint_summary is an
    # all-zeros dict, which is exactly what a legitimate run that checkpointed
    # nothing reports -- so it can never name the failure. error_signals does
    # carry an `ingestion_failed` entry (that print and its pattern were the
    # other half of #270), but this key is where the exception text lives in
    # the metrics JSON itself, independent of whether the stderr tee was
    # running.
    "ingest_error",
)


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation, or None when it is not identifiable.

    Same convention as trace_fit.fit_line: zero variance in either axis (or
    fewer than 2 points) cannot support a correlation, and inventing a number
    -- especially 0.0, which reads as "measured and uncorrelated" rather than
    "could not be measured" -- would be the same failure _ratio_row and
    growth_ratio both guard against elsewhere in this experiment.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return sxy / (sxx * syy) ** 0.5


def _await_vs_w_correlation(records: list[dict]) -> Optional[float]:
    xs = [float(r[trace_fit.W_KEY]) for r in records]
    ys = [float(r.get("await_s", 0.0)) for r in records]
    return _pearson(xs, ys)


def build_result(
    records: list[dict], metrics: dict, env: dict, trace_path: str,
) -> dict:
    """The artifact: trace_fit's analysis plus enough provenance to audit it.

    trace_path is the trace ACTUALLY READ (I3) -- always str(Path(args.trace)
    .resolve()) from the caller, never metrics.get("trace_path"). On the
    --trace/--metrics re-analyse path those can name different files (a
    stale metrics JSON re-pointed at a fresh trace, or vice versa), and
    recording the wrong one is otherwise indistinguishable from a correct
    run until read again -- the same class of guard
    probe_provisional_residue.py:377 applies to graph_path/metrics_json.
    """
    result = dict(trace_fit.analyse(records))
    result["commits_ingested"] = metrics.get("commits_ingested")
    result["wall_clock_seconds"] = metrics.get("wall_clock_seconds")
    result["final_status"] = metrics.get("final_status")
    result["graph_path"] = metrics.get("graph_path")
    result["trace_path"] = trace_path
    metrics_trace_path = metrics.get("trace_path")
    if metrics_trace_path is not None and metrics_trace_path != trace_path:
        print(
            f"[probe] trace_path mismatch: this fit was computed from "
            f"{trace_path!r}, but the metrics JSON recorded {metrics_trace_path!r} "
            f"-- these are different files. The artifact records the trace "
            f"actually read.",
            file=sys.stderr,
        )

    # I2: carry the run's ingestion-health fields when the metrics dict has
    # them. See _CARRIED_METRICS_KEYS for why absence must stay absence.
    for key in _CARRIED_METRICS_KEYS:
        if key in metrics:
            result[key] = metrics[key]

    result["provenance"] = {
        "executable": sys.executable,
        "minigraf_version": _minigraf_version(),
        "stream_ratio": env.get("MINIGRAF_INGEST_STREAM_RATIO", STREAM_RATIO),
        "checkpoint_duty": env.get("MINIGRAF_INGEST_CHECKPOINT_DUTY", "default"),
    }

    # Exploratory only -- W is frozen and these may not be substituted for it.
    await_total = sum(float(r.get("await_s", 0.0)) for r in records)
    apply_total = sum(float(r.get("apply_s", 0.0)) for r in records)
    first, middle, last = trace_fit.split_thirds(records)
    result["exploratory"] = {
        "mean_await_s": await_total / len(records),
        "tag_counts": {
            tag: sum(1 for r in records if r.get("tag") == tag)
            for tag in ("fwd", "rev")
        },
        # I5: the design spec's pre-registered sanity check -- await_s should
        # correlate with W, since extraction stall is the one cost that is
        # unambiguously work-driven. Reported, not gated: a weak correlation
        # here does not overturn the verdict, but it says the check could not
        # see much (usually because await_s is small relative to apply_s, so
        # the serial loop rarely stalls waiting on extraction) rather than
        # that W fails to measure work.
        "await_s_total_seconds": await_total,
        "apply_s_total_seconds": apply_total,
        # #280: Stage A lease-window boundary time (handle drop + pause).
        # Absent from traces written before #280, hence .get.
        "yield_s_total_seconds": sum(float(r.get("yield_s", 0.0)) for r in records),
        "await_s_to_apply_s_ratio": (
            (await_total / apply_total) if apply_total > 0.0 else None
        ),
        "pearson_await_s_vs_W": {
            "overall": _await_vs_w_correlation(records),
            "first_third": _await_vs_w_correlation(first),
            "middle_third": _await_vs_w_correlation(middle),
            "last_third": _await_vs_w_correlation(last),
        },
    }
    return result


def _refuse_existing_trace_for_run(trace_path: Path) -> None:
    """--run needs a FRESH trace file (I4).

    _IngestTrace opens its path in append mode ("a") by design -- a killed
    30-minute run must leave a readable partial trace, not lose it. But that
    means a second `--run` pointed at a reused --trace path silently
    concatenates two ingestions' records into one fit instead of failing:
    the exact corruption class resolve_graph_path's fresh-path refusal exists
    to prevent on the graph axis, left open here on the trace axis.

    Only called from the --run branch of main() -- the --trace/--metrics
    re-analyse path must still be able to read an existing trace; that is
    its whole purpose.
    """
    if trace_path.exists():
        raise SystemExit(
            f"--trace {trace_path} already exists. Each --run needs a fresh "
            f"trace file -- the trace writer opens its path in append mode, "
            f"so reusing an existing one would silently concatenate two "
            f"ingestions' records into one fit. Remove {trace_path}, or pick "
            f"a new --trace path."
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="Drive a fresh traced full-history ingestion first.")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--graph-path", default=None,
                        help="Required with --run. Must not already exist.")
    parser.add_argument("--trace", required=True, help="Trace JSONL path.")
    parser.add_argument("--metrics", default=None,
                        help="Benchmark metrics JSON. Required without --run.")
    parser.add_argument("--out", default=None,
                        help="Artifact path (default: "
                             "results/260-per-commit-cost-attribution.json).")
    parser.add_argument(
        "--report-path", default=str(REPO_ROOT / "evals" / "at_scale" / "benchmark.md"),
        help="Markdown report to append the fit section to (#260, mirrors "
             "probe_provisional_residue.py's --report-path).",
    )
    args = parser.parse_args(argv)

    os.environ.setdefault("MINIGRAF_INGEST_STREAM_RATIO", STREAM_RATIO)

    if args.run:
        if not args.graph_path:
            raise SystemExit("--run needs --graph-path (a fresh path)")
        _refuse_existing_trace_for_run(Path(args.trace))
        import evals.at_scale.run_ingestion_benchmark as bench
        # resolve_graph_path enforces the fresh-path rule -- it refuses the
        # graph, its .wal AND its fact index, because minigraf replays a
        # leftover .wal and re-ingesting into an existing graph repairs
        # nothing (see CLAUDE.md and #235).
        with bench.resolve_graph_path(args.graph_path) as graph_path:
            metrics = asyncio.run(bench.run_ingestion_benchmark(
                args.repo_path, args.branch, graph_path,
                trace_path=Path(args.trace),
            ))
    else:
        if not args.metrics:
            raise SystemExit("without --run, --metrics is required")
        metrics = json.loads(Path(args.metrics).read_text())

    records = read_trace(args.trace)
    # I3: the trace actually read, resolved the same way resolve_graph_path
    # and probe_provisional_residue resolve their own paths -- never
    # metrics.get("trace_path"), which can name a different file on the
    # --trace/--metrics re-analyse path.
    trace_path = str(Path(args.trace).resolve())
    result = build_result(records, metrics, dict(os.environ), trace_path)

    out = Path(args.out) if args.out else (
        REPO_ROOT / "evals" / "at_scale" / "results"
        / "260-per-commit-cost-attribution.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")

    print(json.dumps(result, indent=2))
    print(f"\nWrote {out}")

    # I6: probe_provisional_residue.py's pattern -- the JSON artifact alone
    # left benchmark.md to be updated by hand (an ad-hoc heredoc, for the
    # entry this module originally shipped), which goes stale the next time
    # the probe runs. Appended after the JSON write, for the same reason
    # probe_provisional_residue orders it last: a section rendered before the
    # artifact exists would be describing a result that was not yet on disk.
    from evals.at_scale.report import append_trace_fit_report

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    append_trace_fit_report(result, report_path, out)
    print(f"Appended to {report_path}")

    print(f"VERDICT: {result['verdict']} -- {result['verdict_reason']}")

    # A VOID run is a failure: the measurement did not work, and exiting 0
    # would let CI or a reader record it as a clean flat result.
    return 0 if result["verdict"] != "VOID" else 1


if __name__ == "__main__":
    sys.exit(main())
