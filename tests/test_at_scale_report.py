import json
import sys
from pathlib import Path

import pytest

from evals.at_scale.report import (
    append_ingestion_report,
    append_query_report,
    append_residue_report,
    append_trace_fit_report,
    write_json_result,
)

SAMPLE_METRICS = {
    "repo_path": "/tmp/repo", "branch": "HEAD", "commits_ingested": 2,
    "wall_clock_seconds": 1.234, "throughput_per_minute": 97.2,
    "peak_rss_kb": 45000, "graph_size_bytes": 8192, "index_size_bytes": 4096,
    "status_latency": {"min": 0.001, "p50": 0.002, "p99": 0.004, "max": 0.005},
    "query_latency": {"min": 0.002, "p50": 0.003, "p99": 0.006, "max": 0.008},
    "final_status": "complete",
    "poll_count": 5, "poll_duty_fraction": 0.042,
    "checkpoint_summary": {
        "checkpoints": 52, "suppressed": 480, "total_seconds": 6.8,
        "elapsed_seconds": 141.5, "realised_duty": 0.0481,
    },
}


class TestWriteJsonResult:
    def test_writes_valid_json_file(self, tmp_path):
        path = write_json_result(SAMPLE_METRICS, tmp_path)
        assert path.exists()
        assert json.loads(path.read_text()) == SAMPLE_METRICS

    def test_filename_has_prefix_and_timestamp(self, tmp_path):
        path = write_json_result(SAMPLE_METRICS, tmp_path, prefix="ingestion")
        assert path.name.startswith("ingestion-")
        assert path.suffix == ".json"


class TestAppendIngestionReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert text.startswith("# At-Scale Code-Graph Benchmark")

    def test_appends_metrics_table_with_real_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "## Ingestion Run" in text
        assert "2" in text  # commits_ingested
        assert "complete" in text

    def test_second_call_appends_not_overwrites(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        first_len = len(report_path.read_text())
        append_ingestion_report(SAMPLE_METRICS, report_path)
        assert len(report_path.read_text()) > first_len
        assert report_path.read_text().count("## Ingestion Run") == 2

    def test_poll_duty_cycle_row_renders_with_formatted_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "Poll duty cycle (#242)" in text
        assert "4.20%" in text  # poll_duty_fraction=0.042 -> 4.20%
        assert "over 5 polls" in text  # poll_count=5

    def test_poll_duty_cycle_row_still_renders_for_a_pre_fix_result(self, tmp_path):
        # Every result JSON written before 2026-08-07 lacks the #242 poll
        # keys. Re-rendering one used to raise KeyError; the row must instead
        # render and say so, matching benchmark.md's own "treat its absence as
        # unmeasured, assume inflated" framing.
        pre_fix = {
            k: v for k, v in SAMPLE_METRICS.items()
            if k not in ("poll_count", "poll_duty_fraction")
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(pre_fix, report_path)
        text = report_path.read_text()
        assert "Poll duty cycle (#242)" in text
        assert "not measured" in text

    def test_checkpoint_duty_cycle_row_renders_with_formatted_values(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        text = report_path.read_text()
        assert "Checkpoint duty cycle (#241)" in text
        assert "4.81%" in text  # realised_duty=0.0481 -> 4.81%
        assert "52 checkpoints" in text
        assert "6.80s" in text  # total_seconds=6.8

    def test_checkpoint_duty_cycle_row_still_renders_for_a_pre_fix_result(self, tmp_path):
        # Mirrors the poll-duty precedent immediately above: a result JSON
        # written before #241 landed has no "checkpoint_summary" key at all.
        # Re-rendering one must not raise, and must say the row is
        # unmeasured rather than silently omitting it (#241 Task 6).
        pre_fix = {
            k: v for k, v in SAMPLE_METRICS.items() if k != "checkpoint_summary"
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(pre_fix, report_path)
        text = report_path.read_text()
        assert "Checkpoint duty cycle (#241)" in text
        assert "not measured" in text

    def test_checkpoint_duty_cycle_row_names_an_errored_run_instead_of_blaming_the_harness(
        self, tmp_path,
    ):
        """#270. checkpoint_summary is absent for TWO different reasons: a
        pre-#241 harness, and a run that died before _run_ingestion built
        its _CheckpointPolicy. Both used to render the same
        "pre-2026-08-08 harness; assume once-per-commit cadence" line,
        which for the second case is a false claim about the code that
        produced the run, written into benchmark.md -- the durable human
        record. ingest_error is what tells them apart."""
        errored = {
            k: v for k, v in SAMPLE_METRICS.items() if k != "checkpoint_summary"
        }
        errored["final_status"] = "error"
        errored["ingest_error"] = "RuntimeError: injected pre-policy failure"
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(errored, report_path)
        text = report_path.read_text()
        assert "Checkpoint duty cycle (#241)" in text
        assert "injected pre-policy failure" in text
        assert "once-per-commit cadence" not in text

    def test_checkpoint_duty_cycle_row_does_not_report_an_all_zeros_summary_as_measured(
        self, tmp_path,
    ):
        """#270. Closing the pre-policy window changed what an errored run
        LOOKS like here, and the row had to change with it.

        _CheckpointPolicy is now the first statement in _run_ingestion's try,
        so a run that dies in the preload no longer leaves checkpoint_summary
        absent -- it leaves an all-zeros one. Rendered by the summary-present
        branch that would produce "0.00% over 0 checkpoints (0.00s total, 0
        suppressed)": a formatted measurement, in benchmark.md, for a run that
        measured nothing. That is the same false-claim-in-the-durable-record
        shape the test above exists to prevent, arriving by a new route, so
        ingest_error is consulted BEFORE the summary rather than only when it
        is missing.

        Deliberately keyed on `checkpoints == 0`, not on ingest_error alone: a
        run that failed in Stage B after checkpointing 40 times has a real
        duty figure, and that figure is a measurement worth keeping in the
        record. Only a zeros summary is uninformative enough to suppress.
        """
        errored = dict(SAMPLE_METRICS)
        errored["checkpoint_summary"] = {
            "checkpoints": 0,
            "suppressed": 0,
            "total_seconds": 0.0,
            "elapsed_seconds": 0.0031,
            "realised_duty": 0.0,
        }
        errored["final_status"] = "error"
        errored["ingest_error"] = "RuntimeError: injected pre-policy failure"
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(errored, report_path)
        text = report_path.read_text()
        assert "Checkpoint duty cycle (#241)" in text
        assert "injected pre-policy failure" in text
        assert "0.00%" not in text, (
            "an all-zeros summary from a failed run must not render as a "
            "measured duty figure"
        )
        assert "once-per-commit cadence" not in text

    def test_checkpoint_duty_cycle_row_keeps_a_partial_runs_real_measurement(
        self, tmp_path,
    ):
        """The other side of the rule above: an errored run that DID
        checkpoint still reports its duty. Without this, "the run failed" and
        "the run measured nothing" collapse into one branch and a Stage B
        failure's 52 real checkpoints vanish from the record."""
        errored = dict(SAMPLE_METRICS)
        errored["final_status"] = "error"
        errored["ingest_error"] = "RuntimeError: injected Stage B failure"
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(errored, report_path)
        text = report_path.read_text()
        assert "4.81%" in text
        assert "52 checkpoints" in text

    # --- #256 stderr-capture rows -------------------------------------
    #
    # benchmark.md is the DURABLE HUMAN RECORD, and it is the artifact the
    # prior #255 acceptance claim ("no errors") actually rested on. Before
    # these rows, a run that dropped commits, or one whose capture truncated,
    # was rendered identically to a clean one: "Final status | complete" and
    # nothing else. The JSON and the exit code were honest; this file was not.

    def _clean_256_metrics(self):
        return {
            **SAMPLE_METRICS,
            "stderr_capture_complete": True,
            "skipped_commits": [],
            "error_signals": [],
            "correction_sweep_summaries": [],
            "correction_sweep_skipped": 0,
        }

    def test_a_clean_run_reports_the_tee_active_and_the_capture_complete(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(self._clean_256_metrics(), report_path)
        text = report_path.read_text()
        assert "| Stderr tee (#256) | active" in text
        assert "| Stderr capture (#256) | complete |" in text
        assert "| Commits dropped (#256) | 0 |" in text
        assert "| Error signatures (#251/#256) | 0 |" in text

    def test_dropped_commits_are_visible_in_the_human_record(self, tmp_path):
        """The whole point: "Final status | complete" and "Commits ingested |
        2" are both blind to a dropped commit, so without this row the
        markdown cannot distinguish this run from a clean one."""
        metrics = {**self._clean_256_metrics(), "skipped_commits": ["deadbee1", "cafe002"]}
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Commits dropped (#256) | **2**" in text
        assert "deadbee1" in text and "cafe002" in text

    def test_error_signatures_are_visible_in_the_human_record(self, tmp_path):
        metrics = {
            **self._clean_256_metrics(),
            "error_signals": [
                {"pattern": "page_out_of_bounds", "line": "Page 130 out of bounds"}
            ],
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Error signatures (#251/#256) | **1**: page_out_of_bounds |" in text

    def test_a_truncated_capture_is_flagged_and_its_counts_called_lower_bounds(self, tmp_path):
        """The dangerous case. skipped_commits/error_signals are EMPTY here --
        byte-identical to a clean run -- because the tee stopped collecting.
        The markdown must say so, or an empty count reads as proof of health."""
        metrics = {
            **self._clean_256_metrics(),
            "stderr_capture_complete": False,
            "tee_failure": "TeeStderrFailure('pump did not complete cleanly')",
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "INCOMPLETE" in text
        assert "LOWER BOUNDS" in text
        assert "pump did not complete cleanly" in text

    def test_a_clean_run_reports_zero_fact_index_divergence(self, tmp_path):
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {"divergence": 0, "graph_facts": 1894, "audit_error": None},
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Fact-index divergence (#302) | 0 (1894 facts cross-checked) |" in text

    def test_a_silent_loss_is_visible_beside_an_error_signature_count_of_zero(
        self, tmp_path
    ):
        """#302's exact shape: nothing was printed, so "Error signatures | 0"
        renders clean, and the only row that can contradict it is this one."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 44, "missing_from_graph": 44, "missing_from_index": 0,
                "graph_facts": 356, "audit_error": None,
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Error signatures (#251/#256) | 0 |" in text
        assert "| Fact-index divergence (#302) | **44**" in text
        assert "44 in the index but not the graph" in text

    def test_a_zero_sets_nothing_aside(self, tmp_path):
        """#303 taught the index to hold boolean-valued facts, so the row no
        longer carries an "excluded" caveat -- the zero now means every fact
        the graph produced was cross-checked. A stale key left over in a
        metrics dict must not resurrect the note: a reader who sees facts set
        aside would read the gate as narrower than it is."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 29465, "audit_error": None,
                "unindexed_boolean_facts": 83,
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Fact-index divergence (#302) | 0 (29465 facts cross-checked) |" in text
        assert "excluded" not in text
        assert "boolean" not in text

    def test_an_audit_that_could_not_run_renders_unverified_not_zero(self, tmp_path):
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {"divergence": 0, "audit_error": "OperationalError: no such table"},
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Fact-index divergence (#302) | **UNVERIFIED**" in text
        assert "no such table" in text

    def test_a_clean_run_reports_zero_duplicate_introduced_by(self, tmp_path):
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 1894, "audit_error": None,
                "introduced_by_duplicates": {"entities": 0, "sample": []},
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Duplicate :introduced-by (#287) | 0 |" in text

    def test_an_affected_graph_is_told_to_rebuild_not_repair(self, tmp_path):
        """The whole point of the detection: the answer is never "repair". A
        row that printed a count without saying what to do would leave the
        reader to discover the standing decision somewhere else."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 29465, "audit_error": None,
                "introduced_by_duplicates": {
                    "entities": 12,
                    "sample": [[":function/f", [":commit/aaa", ":commit/bbb"]]],
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Duplicate :introduced-by (#287) | **12**" in text
        assert "rebuilt into a fresh graph path" in text
        assert ":function/f" in text

    def test_an_affected_graph_reads_wrong_beside_a_zero_divergence(self, tmp_path):
        """#287's exact shape, and why it is not folded into `divergence`:
        the index holds both values faithfully, so the two witnesses agree
        perfectly about a graph that is wrong. Only this row contradicts the
        clean sweep above it."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 29465, "audit_error": None,
                "introduced_by_duplicates": {"entities": 12, "sample": []},
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Fact-index divergence (#302) | 0 (29465 facts cross-checked) |" in text
        assert "| Duplicate :introduced-by (#287) | **12**" in text

    def test_an_audit_that_could_not_run_renders_unverified_not_zero_here_too(
        self, tmp_path
    ):
        """A failed scan yields an empty fact set, and an empty fact set has
        no duplicates. Rendering 0 would say "clean" about a graph nobody
        managed to read."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "audit_error": "OperationalError: no such table",
                "introduced_by_duplicates": None,
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Duplicate :introduced-by (#287) | **UNVERIFIED**" in text

    def test_a_pre_287_fact_audit_renders_not_measured(self, tmp_path):
        """A metrics file from a harness that HAD the fact audit but not this
        check carries the outer key and not the inner one. Absent is not zero:
        that graph was never asked."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {"divergence": 0, "graph_facts": 1894, "audit_error": None},
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Duplicate :introduced-by (#287) | not measured" in text

    def test_a_clean_run_reports_zero_orphans_with_its_denominator(self, tmp_path):
        """0 is never rendered bare. A check that matched no code entities
        would also report 0 entities, so the denominator is the half that
        makes the zero mean anything -- CLAUDE.md's standing requirement for
        a zero-tolerance gate, restated by every run instead of once."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 1894, "audit_error": None,
                "entities_without_introduced_by": {
                    "entities": 0, "code_entities_scanned": 3150, "sample": [],
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert (
            "| Code entities with no :introduced-by (#316) | 0 of 3150 "
            "code entities |"
        ) in text

    def test_a_zero_denominator_is_not_rendered_as_clean(self, tmp_path):
        """The state the denominator exists to distinguish. Both halves are
        zero, which is also exactly what a broken narrowing looks like, so the
        row says the check proved nothing rather than calling it a pass."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 1894, "audit_error": None,
                "entities_without_introduced_by": {
                    "entities": 0, "code_entities_scanned": 0, "sample": [],
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "**0 code entities were scanned**" in text
        assert "proved nothing about it" in text

    def test_an_orphaned_graph_is_told_to_rebuild_not_repair(self, tmp_path):
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 29465, "audit_error": None,
                "entities_without_introduced_by": {
                    "entities": 3, "code_entities_scanned": 3150,
                    "sample": [":module/mcp_server-py"],
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Code entities with no :introduced-by (#316) | **3** of 3150" in text
        assert "rebuilt into a fresh graph path" in text
        assert ":module/mcp_server-py" in text

    def test_an_orphaned_graph_reads_wrong_beside_two_clean_rows(self, tmp_path):
        """#316's exact shape. The index is missing precisely the fact the
        graph is missing, so `divergence` is 0; the duplicate check skips
        anything holding fewer than two values, so that row is 0 too. Only
        this row contradicts the clean sweep above it."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "graph_facts": 29465, "audit_error": None,
                "introduced_by_duplicates": {"entities": 0, "sample": []},
                "entities_without_introduced_by": {
                    "entities": 3, "code_entities_scanned": 3150, "sample": [],
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Fact-index divergence (#302) | 0 (29465 facts cross-checked) |" in text
        assert "| Duplicate :introduced-by (#287) | 0 |" in text
        assert "| Code entities with no :introduced-by (#316) | **3** of 3150" in text

    def test_an_audit_that_could_not_run_renders_unverified_for_orphans_too(
        self, tmp_path
    ):
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {
                "divergence": 0, "audit_error": "OperationalError: no such table",
                "entities_without_introduced_by": None,
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Code entities with no :introduced-by (#316) | **UNVERIFIED**" in text

    def test_a_pre_316_fact_audit_renders_not_measured(self, tmp_path):
        """Outer key present, this one absent. Absence is not zero: that graph
        was never asked."""
        metrics = {
            **self._clean_256_metrics(),
            "fact_audit": {"divergence": 0, "graph_facts": 1894, "audit_error": None},
        }
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        text = report_path.read_text()
        assert "| Code entities with no :introduced-by (#316) | not measured" in text

    def test_the_256_rows_still_render_for_a_pre_fix_result(self, tmp_path):
        """Same defensive precedent as the poll and checkpoint rows: a result
        JSON written before 2026-08-16 carries none of these keys. Absence
        must render as "not measured", never as zero -- nothing was captured,
        so nothing is known."""
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)  # no #256 keys
        text = report_path.read_text()
        assert "| Stderr tee (#256) | none" in text
        assert "| Stderr capture (#256) | not measured" in text
        assert "| Commits dropped (#256) | not measured" in text
        assert "| Error signatures (#251/#256) | not measured" in text
        assert "| Fact-index divergence (#302) | not measured" in text
        assert "| Duplicate :introduced-by (#287) | not measured" in text
        assert "| Code entities with no :introduced-by (#316) | not measured" in text
        assert "NOT zero" in text


class TestCommitCensusRow:
    """#317. The one content row whose reference is not the graph itself."""

    def _clean(self):
        # Mirrors TestAppendIngestionReport's own clean-metrics shape closely
        # enough for the row under test; the other rows' rendering is covered
        # by their own tests.
        return {
            "repo_path": ".", "branch": "master", "commits_ingested": 847,
            "wall_clock_seconds": 10.0, "throughput_per_minute": 5082.0,
            "peak_rss_kb": 1000, "graph_size_bytes": 1, "index_size_bytes": 1,
            "status_latency": {"min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0},
            "query_latency": {"min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0},
            "final_status": "complete", "poll_count": 1,
            "poll_duty_fraction": 0.0, "poll_offsets": [],
            "checkpoint_summary": None, "ingest_error": None,
            "skipped_commits": [], "error_signals": [],
            "stderr_capture_complete": True,
        }

    def _render(self, tmp_path, census):
        metrics = {**self._clean()}
        if census is not None:
            metrics["commit_census"] = census
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        return report_path.read_text()

    def test_a_clean_run_renders_all_three_counts(self, tmp_path):
        """Never a bare tick. `847 / 847 / 847` says which of the three numbers
        a future run moved; a single "0 divergence" would not, and three counts
        that are all zero also agree perfectly."""
        text = self._render(tmp_path, {
            "ref": "master", "repo_commits": 847, "walk_claimed": 847,
            "graph_commit_entities": 847, "ok": True, "proved_nothing": False,
            "census_error": None,
        })
        assert "| Commits: repo / walk / graph (#317) | 847 / 847 / 847" in text

    def test_the_clean_row_names_the_ref_it_counted(self, tmp_path):
        text = self._render(tmp_path, {
            "ref": "master", "repo_commits": 847, "walk_claimed": 847,
            "graph_commit_entities": 847, "ok": True, "proved_nothing": False,
            "census_error": None,
        })
        assert "(ref `master`)" in text

    def test_a_lost_commit_renders_bold_with_its_interpretation(self, tmp_path):
        text = self._render(tmp_path, {
            "ref": "master", "repo_commits": 847, "walk_claimed": 847,
            "graph_commit_entities": 846, "ok": False, "proved_nothing": False,
            "census_error": None,
            "interpretation": "the walk claimed 847 commits but the graph holds 846",
        })
        assert "**847 / 847 / 846**" in text
        assert "the graph holds 846" in text

    def test_an_absent_census_reads_as_not_measured(self, tmp_path):
        """Absence is not zero: such a run was never asked."""
        text = self._render(tmp_path, None)
        assert "| Commits: repo / walk / graph (#317) | not measured" in text

    def test_a_failed_census_reads_as_unverified_not_clean(self, tmp_path):
        text = self._render(tmp_path, {
            "ref": "master", "repo_commits": 0, "walk_claimed": 847,
            "graph_commit_entities": 847, "ok": False, "proved_nothing": False,
            "census_error": "FileNotFoundError: git",
        })
        assert "**UNVERIFIED**" in text
        assert "FileNotFoundError: git" in text

    def test_an_empty_repo_is_not_rendered_as_clean(self, tmp_path):
        """The state the denominator exists to distinguish. All three counts
        are zero, which is also exactly what a wrong ref looks like, so the row
        says the census proved nothing rather than calling it a pass."""
        text = self._render(tmp_path, {
            "ref": "master", "repo_commits": 0, "walk_claimed": 0,
            "graph_commit_entities": 0, "ok": True, "proved_nothing": True,
            "census_error": None,
        })
        assert "proved nothing about the" in text


class TestOrphanedCommitsRow:
    """#222 phase 5, gate clause 9. `entities` is never rendered without
    `proved_nothing`, and a clean row carries its denominator."""

    def _render(self, tmp_path, census):
        metrics = {**TestCommitCensusRow()._clean()}
        if census is not None:
            metrics["commit_census"] = census
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(metrics, report_path)
        return report_path.read_text()

    @staticmethod
    def _census(orphans, census_error=None):
        return {
            "ref": "master", "repo_commits": 957, "walk_claimed": 957,
            "graph_commit_entities": 957, "ok": True, "proved_nothing": False,
            "census_error": census_error, "orphaned_commits": orphans,
        }

    def test_a_clean_run_renders_zero_with_its_denominator(self, tmp_path):
        text = self._render(tmp_path, self._census({
            "entities": 0, "commit_entities_scanned": 957, "sample": [],
            "recorded_branch": "master", "audited_ref": "master",
            "proved_nothing": False,
        }))
        assert "| Orphaned commit entities (#222 phase 5) | 0 of 957 (ref `master`) |" in text

    def test_an_orphaned_graph_is_told_to_rebuild_not_repair(self, tmp_path):
        text = self._render(tmp_path, self._census({
            "entities": 2, "commit_entities_scanned": 959, "sample": ["dead1", "dead2"],
            "recorded_branch": "master", "audited_ref": "master",
            "proved_nothing": False,
        }))
        assert "**2** of 959" in text
        assert "rebuilt into a fresh graph path" in text
        assert "`dead1`" in text

    def test_a_branch_mismatch_shows_its_count_as_uninterpretable_not_findings(
        self, tmp_path
    ):
        """The count ships, but it may be another branch's real history: the
        row must neither call it clean nor tell anyone to rebuild."""
        text = self._render(tmp_path, self._census({
            "entities": 5, "commit_entities_scanned": 962, "sample": ["x"],
            "recorded_branch": "develop", "audited_ref": "master",
            "proved_nothing": True,
        }))
        row = next(l for l in text.splitlines() if "Orphaned commit entities" in l)
        assert "5 of 962" in row
        assert "proved nothing" in row
        assert "`develop`" in row
        assert "rebuilt" not in row
        assert "**5**" not in row

    def test_an_absent_branch_is_named_as_the_reason(self, tmp_path):
        text = self._render(tmp_path, self._census({
            "entities": 3, "commit_entities_scanned": 10, "sample": [],
            "recorded_branch": None, "audited_ref": "master",
            "proved_nothing": True,
        }))
        assert "records no `:ingestion/branch`" in text

    def test_a_zero_denominator_is_not_rendered_as_clean(self, tmp_path):
        text = self._render(tmp_path, self._census({
            "entities": 0, "commit_entities_scanned": 0, "sample": [],
            "recorded_branch": "master", "audited_ref": "master",
            "proved_nothing": True,
        }))
        assert "0 commit entities were scanned" in text
        assert "0 of 0 (ref" not in text

    def test_a_failed_census_reads_as_unverified(self, tmp_path):
        text = self._render(tmp_path, self._census({
            "entities": 0, "commit_entities_scanned": 0, "sample": [],
            "recorded_branch": None, "audited_ref": "master",
            "proved_nothing": True,
        }, census_error="FileNotFoundError: git"))
        row = next(l for l in text.splitlines() if "Orphaned commit entities" in l)
        assert "**UNVERIFIED**" in row

    def test_a_pre_phase5_census_renders_not_measured(self, tmp_path):
        census = self._census(None)
        del census["orphaned_commits"]
        text = self._render(tmp_path, census)
        assert "| Orphaned commit entities (#222 phase 5) | not measured" in text


class TestMetricsJsonBullet:
    """#276: a reader of an Ingestion Run section had no way to find the
    results JSON it was rendered from, and therefore no way to find the
    residue JSON that pairs with it. The bullet sits beside `- Repo:` rather
    than in the metrics table because it is provenance, not a measurement.
    """

    def test_records_the_metrics_json_relative_to_the_report(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        json_path = tmp_path / "results" / "ingestion-20260817T041942Z.json"
        append_ingestion_report(SAMPLE_METRICS, report_path, json_path)
        assert (
            "- Metrics JSON: `results/ingestion-20260817T041942Z.json`"
            in report_path.read_text()
        )

    def test_falls_back_to_the_absolute_path_when_not_under_the_report_dir(self, tmp_path):
        # An artifact copied off a run host, or a tmp_path in a test. An
        # absolute path is more useful to a human than a ../../.. chain.
        report_path = tmp_path / "report" / "benchmark.md"
        report_path.parent.mkdir()
        json_path = tmp_path / "elsewhere" / "ingestion-x.json"
        append_ingestion_report(SAMPLE_METRICS, report_path, json_path)
        assert f"- Metrics JSON: `{json_path.resolve()}`" in report_path.read_text()

    def test_absence_is_visible_rather_than_a_missing_line(self, tmp_path):
        # Same reason _poll_duty_row always emits: an omitted line is
        # invisible, and a reader cannot distinguish "not recorded" from
        # "nobody looked".
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        assert "- Metrics JSON: not recorded" in report_path.read_text()


SAMPLE_QUERY_REPORT = {
    "entries": [
        {
            "id": 1, "category": "point-in-time", "passed": True,
            "actual": [[8]], "expected": [[8]],
            "minigraf_latency_seconds": 0.003, "baseline_latency_seconds": 0.015,
        },
        {
            "id": 2, "category": "delta", "passed": None,
            "actual": None, "expected": None,
            "minigraf_latency_seconds": 0.0, "baseline_latency_seconds": 0.0,
        },
    ],
    "ingestion": {
        "commits_ingested": 12, "final_status": "complete",
        "stderr_capture_complete": True, "skipped_commits": [], "error_signals": [],
    },
}


class TestAppendQueryReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_REPORT, report_path)
        assert report_path.read_text().startswith("# At-Scale Code-Graph Benchmark")

    def test_reports_pass_fail_and_skipped(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_REPORT, report_path)
        text = report_path.read_text()
        assert "## Query Correctness Run" in text
        assert "PASS" in text
        assert "SKIPPED (manual diff)" in text

    def test_a_clean_ingestion_phase_is_reported(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_REPORT, report_path)
        text = report_path.read_text()
        assert "Ingestion phase (#275)" in text
        assert "| Commits ingested | 12 |" in text
        assert "| Stderr capture (#256) | complete |" in text
        assert "| Commits dropped (#256) | 0 |" in text
        assert "| Error signatures (#251/#256) | 0 |" in text

    def test_a_dirty_ingestion_phase_is_visible_beside_the_latencies(self, tmp_path):
        """#275: the latencies are the reason this matters. Query numbers
        measured over a graph that silently lost commits are not comparable to
        ones that were not, and the section used to render them identically.
        """
        report = {
            **SAMPLE_QUERY_REPORT,
            "ingestion": {
                **SAMPLE_QUERY_REPORT["ingestion"],
                "skipped_commits": ["deadbee1", "cafe002"],
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_query_report(report, report_path)
        text = report_path.read_text()
        assert "| Commits dropped (#256) | **2**" in text
        assert "deadbee1" in text

    def test_a_truncated_capture_is_flagged_in_the_query_section_too(self, tmp_path):
        # Rendered by the SAME helper the ingestion section uses, so the two
        # cannot disagree about how a dirty run reads.
        report = {
            **SAMPLE_QUERY_REPORT,
            "ingestion": {
                **SAMPLE_QUERY_REPORT["ingestion"],
                "stderr_capture_complete": False,
                "tee_failure": "TeeStderrFailure('pump did not complete cleanly')",
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_query_report(report, report_path)
        text = report_path.read_text()
        assert "INCOMPLETE" in text
        assert "LOWER BOUNDS" in text

    def test_a_condemned_graph_is_flagged_in_the_query_section_too(self, tmp_path):
        """#287, and the reason _query_ingestion_rows reuses the row rather
        than re-rendering it: query latencies measured over a graph that must
        be thrown away are not comparable to ones measured over a healthy
        graph, and the fact-index row above cannot say so -- an affected graph
        diverges by zero. Wiring the row into the ingestion section alone
        broke no test until this one existed."""
        report = {
            **SAMPLE_QUERY_REPORT,
            "ingestion": {
                **SAMPLE_QUERY_REPORT["ingestion"],
                "fact_audit": {
                    "divergence": 0, "graph_facts": 29465, "audit_error": None,
                    "introduced_by_duplicates": {"entities": 12, "sample": []},
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_query_report(report, report_path)
        text = report_path.read_text()
        assert "| Duplicate :introduced-by (#287) | **12**" in text
        assert "rebuilt into a fresh graph path" in text

    def test_an_orphaned_graph_is_flagged_in_the_query_section_too(self, tmp_path):
        """#316, same reason as the #287 case above: query latencies measured
        over a graph that must be thrown away are not comparable to ones
        measured over a healthy graph, and NEITHER row above this one can say
        so -- an orphaned graph diverges by zero and holds no duplicates.
        Wiring the row into the ingestion section alone broke no test until
        this one existed."""
        report = {
            **SAMPLE_QUERY_REPORT,
            "ingestion": {
                **SAMPLE_QUERY_REPORT["ingestion"],
                "fact_audit": {
                    "divergence": 0, "graph_facts": 29465, "audit_error": None,
                    "introduced_by_duplicates": {"entities": 0, "sample": []},
                    "entities_without_introduced_by": {
                        "entities": 3, "code_entities_scanned": 3150, "sample": [],
                    },
                },
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_query_report(report, report_path)
        text = report_path.read_text()
        assert "| Code entities with no :introduced-by (#316) | **3** of 3150" in text
        assert "rebuilt into a fresh graph path" in text

    def test_an_absent_ingestion_key_renders_not_measured(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report({"entries": SAMPLE_QUERY_REPORT["entries"]}, report_path)
        text = report_path.read_text()
        assert "Ingestion phase (#275)" in text
        assert "not measured" in text


# A verdict as probe_provisional_residue.main() builds it -- keys copied
# from the committed artifact at
# evals/at_scale/results/256-provisional-residue.json.
SAMPLE_RESIDUE = {
    "provisional_entities": 0,
    "sweep_skipped": 3,
    "ok": True,
    "interpretation": "M <= N: provisional residue is within the correction "
                      "sweep's own accounting.",
    "commits_in_graph": 732,
    "graph_path": "/tmp/bench/at-scale.graph",
    "metrics_json": "/repo/evals/at_scale/results/ingestion-20260816T194720Z.json",
    "breakdown_by_entity_type": {},
    "correction_sweep_summaries": [3],
}


class TestAppendResidueReport:
    """#276: benchmark.md carried the #256 capture-health rows but not the
    M <= N verdict those rows exist to qualify, so the durable human record
    said a run was clean in the capture sense while saying nothing about
    whether its provisional residue was accounted for.
    """

    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert report_path.read_text().startswith("# At-Scale Code-Graph Benchmark")

    def test_a_clean_verdict_renders_the_numbers_and_the_reading(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        text = report_path.read_text()
        assert "## Provisional Residue" in text
        assert "| Verdict (#256) | OK -- M <= N" in text
        assert "| Provisional entities (M) | 0 |" in text
        assert "| Sweep skipped (N) | 3 |" in text
        assert "| Commits in graph | 732 |" in text

    def test_m_above_n_renders_as_a_failure_not_a_number(self, tmp_path):
        # The signature this probe exists to detect. It must not be possible
        # to skim the section and read it as ordinary.
        result = {**SAMPLE_RESIDUE, "provisional_entities": 7,
                  "sweep_skipped": 3, "ok": False}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        text = report_path.read_text()
        assert "**FAILED**" in text
        assert "M > N" in text
        assert "#251" in text

    def test_second_call_appends_not_overwrites(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert report_path.read_text().count("## Provisional Residue") == 2

    def test_a_populated_breakdown_is_rendered(self, tmp_path):
        result = {**SAMPLE_RESIDUE, "provisional_entities": 3,
                  "breakdown_by_entity_type": {"function": 2, "module": 1}}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        assert "| Provisional by entity type | function: 2, module: 1 |" in \
            report_path.read_text()

    def test_an_empty_breakdown_renders_none_not_not_measured(self, tmp_path):
        # An empty dict is a MEASURED zero -- the healthy case. Only an
        # absent key is unmeasured, and conflating the two would make every
        # clean run look uninspected.
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert "| Provisional by entity type | none |" in report_path.read_text()

    @pytest.mark.parametrize(
        "key,label",
        [
            ("provisional_entities", "Provisional entities (M)"),
            ("sweep_skipped", "Sweep skipped (N)"),
            ("commits_in_graph", "Commits in graph"),
            ("breakdown_by_entity_type", "Provisional by entity type"),
        ],
    )
    def test_an_absent_key_renders_not_measured_and_never_zero(self, tmp_path, key, label):
        """report.py's standing convention (_poll_duty_row). It matters more
        here than anywhere else in the file: the nightly workflow does not run
        this probe, so most benchmark.md entries carry no residue section at
        all, and a re-rendered older artifact must not be able to manufacture
        a clean verdict out of missing keys.
        """
        result = {k: v for k, v in SAMPLE_RESIDUE.items() if k != key}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        row = next(
            line for line in report_path.read_text().splitlines()
            if line.startswith(f"| {label} |")
        )
        assert "not measured" in row
        assert "| 0 |" not in row
        assert "| none |" not in row

    def test_an_absent_verdict_renders_not_measured(self, tmp_path):
        result = {k: v for k, v in SAMPLE_RESIDUE.items() if k != "ok"}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        row = next(
            line for line in report_path.read_text().splitlines()
            if line.startswith("| Verdict (#256) |")
        )
        assert "not measured" in row
        assert "OK" not in row

    def test_the_three_artifact_paths_are_recorded(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        out_path = tmp_path / "results" / "256-provisional-residue.json"
        append_residue_report(SAMPLE_RESIDUE, report_path, out_path)
        text = report_path.read_text()
        # Resolved on both sides: _relative_to_report resolves its input, and
        # /tmp is a symlink on some platforms.
        assert f"| Graph | `{Path('/tmp/bench/at-scale.graph').resolve()}` |" in text
        assert "ingestion-20260816T194720Z.json" in text
        assert "| Residue JSON | `results/256-provisional-residue.json` |" in text

    def test_an_absent_residue_json_path_renders_not_recorded(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert "| Residue JSON | not recorded |" in report_path.read_text()

    @pytest.mark.parametrize(
        "key,label",
        [
            ("graph_path", "Graph"),
            ("metrics_json", "Metrics JSON"),
        ],
    )
    def test_an_absent_result_path_renders_not_recorded(self, tmp_path, key, label):
        """Same _residue_path_row helper as the Residue JSON row above, but
        for the two paths that come from `result` rather than the json_out
        argument -- untested until now, so a break in either would only show
        up as a missing bullet in a real report, not a red test."""
        result = {k: v for k, v in SAMPLE_RESIDUE.items() if k != key}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        row = next(
            line for line in report_path.read_text().splitlines()
            if line.startswith(f"| {label} |")
        )
        assert "not recorded" in row


class TestAppendTraceFitReport:
    """#260: the per-commit cost fit's verdict, rendered the same defensive
    way append_residue_report renders the M <= N verdict -- an absent ratio
    must not be able to read as a flat result, and a VOID verdict (the
    control gate failed open) must not be able to read as CONFOUNDED.
    """

    def test_renders_verdict_ratios_and_control_gate(self, tmp_path):
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "CONFOUNDED",
            "verdict_reason": "both parameters below 1.5x (a=1.10x, b=1.05x)",
            "a_ratio": 1.10, "b_ratio": 1.05,
            "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {"passed": True, "growth": 4.2, "reason": "passed: ..."},
            "commits_ingested": 767,
        }, report)
        text = report.read_text()
        assert "CONFOUNDED" in text
        # Exact rows, not bare substrings: verdict_reason is rendered
        # verbatim just above ("...a=1.10x, b=1.05x)"), so a substring check
        # against "1.10"/"1.05" passes even if _ratio_row's value branch is
        # broken -- the reason line alone already contains both. See the
        # sibling test below, which was tightened for the same defect.
        assert "- a_ratio (fixed-cost growth): 1.10x" in text
        assert "- b_ratio (per-unit-work-cost growth): 1.05x" in text
        assert "4.2" in text or "4.20" in text

    def test_void_verdict_is_rendered_as_void_not_as_flat(self, tmp_path):
        """A void run must be unmistakable in the record. #276's whole lesson
        was that a self-misdescribing record is the defect."""
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "VOID",
            "verdict_reason": "control gate did not pass: FAILED OPEN ...",
            "a_ratio": 1.01, "b_ratio": 1.02, "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {"passed": False, "growth": 1.1, "reason": "FAILED OPEN ..."},
        }, report)
        text = report.read_text()
        assert "VOID" in text
        assert "CONFOUNDED" not in text

    def test_measured_and_failed_control_gate_renders_the_failed_row(self, tmp_path):
        """M5: no prior test positively asserted the rendered control-gate row
        for a MEASURED-and-FAILED gate -- test_void_verdict_is_rendered... above
        exercises this same input shape but never reads the row itself, only
        the verdict string. This pins _trace_fit_control_gate_row's FAILED
        branch (report.py) directly, distinguishing it from both the passing
        row and the "not measured (absent from the result)" row."""
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "VOID",
            "verdict_reason": "control gate did not pass: FAILED OPEN ...",
            "a_ratio": 1.01, "b_ratio": 1.02, "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {
                "passed": False, "growth": 1.1,
                "reason": "FAILED OPEN: mean per-checkpoint duration grew 1.10x",
            },
        }, report)
        text = report.read_text()
        assert (
            "- Control gate: **FAILED** (1.10x growth) -- "
            "FAILED OPEN: mean per-checkpoint duration grew 1.10x"
        ) in text
        assert "- Control gate: passed" not in text
        assert "- Control gate: not measured" not in text

    def test_absent_ratio_renders_not_measured_never_zero(self, tmp_path):
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "INCONCLUSIVE",
            "verdict_reason": "a_ratio is undefined",
            "a_ratio": None, "b_ratio": 1.2, "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {"passed": True, "growth": 3.0, "reason": "passed"},
        }, report)
        text = report.read_text()
        # The exact a_ratio row, not a bare substring: "not measured" is also
        # the correct rendering for other absent fields in this fixture (e.g.
        # commits_ingested, which is omitted here entirely), so a substring
        # check alone would pass even if _ratio_row's None branch were broken,
        # as long as some other field happened to say "not measured" too.
        assert "a_ratio (fixed-cost growth): not measured" in text
        assert "0.00" not in text

    def test_appends_rather_than_truncating(self, tmp_path):
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n\n## Existing entry\n")
        append_trace_fit_report({
            "verdict": "CONFOUNDED", "verdict_reason": "x",
            "a_ratio": 1.0, "b_ratio": 1.0, "records": 100,
            "group_sizes": {"first": 33, "middle": 34, "last": 33},
            "control_gate": {"passed": True, "growth": 3.0, "reason": "passed"},
        }, report)
        assert "## Existing entry" in report.read_text()

    def test_commits_ingested_present_but_none_renders_not_measured(self, tmp_path):
        """build_result (Task 6) sets commits_ingested to an explicit None
        whenever the metrics JSON lacks the field -- reachable through the
        --metrics re-analyse path with an older or partial file. .get(key,
        default) only substitutes the default when the KEY is absent, so a
        present-but-None value must be checked explicitly or the page prints
        the raw Python string "None", which is outside the three-state
        vocabulary entirely.
        """
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "CONFOUNDED", "verdict_reason": "x",
            "a_ratio": 1.0, "b_ratio": 1.0, "records": 100,
            "group_sizes": {"first": 33, "middle": 34, "last": 33},
            "control_gate": {"passed": True, "growth": 3.0, "reason": "passed"},
            "commits_ingested": None,
        }, report)
        text = report.read_text()
        assert "- Commits ingested: not measured" in text
        assert "None" not in text

    def test_absent_control_gate_renders_not_measured_not_failed(self, tmp_path):
        """{}.get("passed") and a real failure's control_gate.get("passed")
        are both falsy, so a control_gate that was never measured must be
        distinguished BEFORE that lookup, not by it -- otherwise an absent
        key manufactures a **FAILED** reading out of nothing, the same
        absence-as-a-result hole _ratio_row exists to close for the ratios.
        """
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        result = {
            "verdict": "CONFOUNDED", "verdict_reason": "x",
            "a_ratio": 1.0, "b_ratio": 1.0, "records": 100,
            "group_sizes": {"first": 33, "middle": 34, "last": 33},
        }
        assert "control_gate" not in result
        append_trace_fit_report(result, report)
        text = report.read_text()
        assert "- Control gate: not measured" in text
        assert "FAILED" not in text

    def test_populated_and_unidentifiable_fits_are_both_rendered(self, tmp_path):
        """Design spec 2026-08-17-per-commit-cost-attribution-design.md line
        250: the INCONCLUSIVE verdict must report both parameters and the fit
        quality, since a ratio alone can't distinguish a clean fit from a
        near-zero r^2 that happened to clear the growth thresholds. A None
        fit (trace_fit.fit_line's unidentifiable case) must render "not
        identifiable", never a numeric r^2 like 0.00.
        """
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "INCONCLUSIVE", "verdict_reason": "x",
            "a_ratio": 1.6, "b_ratio": 1.3, "records": 300,
            "group_sizes": {"first": 100, "middle": 100, "last": 100},
            "control_gate": {"passed": True, "growth": 3.0, "reason": "passed"},
            "fits": {
                "first": {"a": 0.1, "b": 0.02, "r2": 0.91, "n": 100},
                "middle": {"a": 0.12, "b": 0.021, "r2": 0.88, "n": 100},
                "last": None,
            },
        }, report)
        text = report.read_text()
        assert "First-group fit: n=100, r²=0.910" in text
        assert "Middle-group fit: n=100, r²=0.880" in text
        assert "Last-group fit: not identifiable" in text
        assert "r²=0.00" not in text


class TestRuntimeVersionAttribution:
    """#284 item 4: every recorded run must name the minigraf version that
    produced it.

    Comparing 1.2.3 against 2.0.0 is the entire point of the item, and nothing
    in the metrics or the rendered report captured the version -- two appended
    runs were indistinguishable. It matters beyond the comparison: minigraf's
    version materially changes ingestion cost (the #260 per-commit handle drop,
    2.0.0's ~376ms contended-open retry, SyncMode), so every historical section
    in benchmark.md is an unattributed number.
    """

    def test_runtime_versions_reports_the_installed_minigraf(self):
        import importlib.metadata as md

        from evals.at_scale.report import runtime_versions

        versions = runtime_versions()
        assert versions["minigraf"] == md.version("minigraf")
        assert versions["python"].startswith(
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )

    def test_ingestion_report_renders_the_minigraf_version(self, tmp_path):
        from evals.at_scale.report import append_ingestion_report

        report_path = tmp_path / "benchmark.md"
        metrics = dict(SAMPLE_METRICS, minigraf_version="9.9.9")
        append_ingestion_report(metrics, report_path)
        assert "9.9.9" in report_path.read_text()

    def test_ingestion_report_survives_a_run_recorded_before_this_existed(self, tmp_path):
        """Historical results JSONs have no minigraf_version key. Re-rendering
        one must not crash -- it must say so instead."""
        from evals.at_scale.report import append_ingestion_report

        report_path = tmp_path / "benchmark.md"
        metrics = dict(SAMPLE_METRICS)
        metrics.pop("minigraf_version", None)
        append_ingestion_report(metrics, report_path)
        assert "unrecorded" in report_path.read_text()

    def test_query_report_renders_the_minigraf_version(self, tmp_path):
        from evals.at_scale.report import append_query_report

        report_path = tmp_path / "benchmark.md"
        append_query_report(
            {"entries": [], "minigraf_version": "9.9.9"}, report_path
        )
        assert "9.9.9" in report_path.read_text()
