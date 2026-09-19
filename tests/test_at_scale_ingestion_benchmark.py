# tests/test_at_scale_ingestion_benchmark.py
import contextlib
import io
import os
import subprocess as _subprocess
import sys
from pathlib import Path

import pytest

import evals.at_scale.run_ingestion_benchmark as rib
from evals.at_scale.run_ingestion_benchmark import (
    _exit_code,
    resolve_graph_path,
    run_ingestion_benchmark,
)
from evals.at_scale.stderr_capture import TeeStderrFailure

# Every key run_ingestion_benchmark returns on a clean run. Shared with the
# tee-failure tests below, whose whole point is that the failure path produces
# the SAME dict plus its two diagnostic keys, not a truncated one.
_EXPECTED_METRIC_KEYS = {
    "repo_path", "branch", "graph_path", "commits_ingested", "wall_clock_seconds",
    "throughput_per_minute", "peak_rss_kb", "graph_size_bytes",
    "index_size_bytes", "status_latency", "query_latency", "final_status",
    "poll_count", "poll_duty_fraction", "poll_offsets", "checkpoint_summary",
    "skipped_commits", "error_signals", "correction_sweep_summaries",
    "correction_sweep_skipped", "stderr_capture_complete", "ingest_error",
    # #302: the one key derived from the graph's CONTENT rather than its logs.
    "fact_audit",
    # #317: the only content key whose reference is not the graph itself. A
    # commit that never reached the graph is absent from the fact index in
    # exactly the same way, so fact_audit's two witnesses agree perfectly
    # about it and only the repo can report it.
    "commit_census",
    # #284 item 4: attribution. A wall-clock number is not comparable across
    # runs without the minigraf version that produced it.
    "minigraf_version", "python_version",
}


class TestExitCode:
    def test_zero_when_status_complete(self):
        assert _exit_code({"final_status": "complete"}) == 0

    def test_nonzero_when_status_error(self):
        assert _exit_code({"final_status": "error"}) == 1

    def test_zero_when_status_missing(self):
        assert _exit_code({}) == 0


class TestExitCodeGate:
    def test_clean_run_exits_zero(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 0

    def test_a_skipped_commit_fails_the_run(self):
        """processed and final_status are both blind to this -- the gate is
        the only thing that turns a dropped commit into a failure."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": ["abc123"],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 1

    def test_a_251_signature_fails_the_run(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [{"pattern": "page_out_of_bounds", "line": "..."}],
            "stderr_capture_complete": True,
        }) == 1

    def test_error_status_still_fails(self):
        assert _exit_code({
            "final_status": "error",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 1

    def test_missing_keys_are_treated_as_clean_for_old_metrics(self):
        """Pre-#256 metrics files carry none of these keys; _exit_code must
        not crash reading them, and must not fail them either."""
        assert _exit_code({"final_status": "complete"}) == 0

    def test_a_tee_failure_fails_the_run(self):
        """run_ingestion_benchmark CATCHES TeeStderrFailure so a broken tee
        does not destroy a 25-minute run's metrics. Without this clause that
        catch silently converts the failure back into exit 0."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": False,
            "tee_failure": "TeeStderrFailure('pump did not complete cleanly')",
        }) == 1

    def test_an_incomplete_capture_fails_even_with_empty_signal_lists(self):
        """The discriminating case: a truncated capture yields EMPTY
        skipped_commits/error_signals, which are byte-identical to a clean
        run's. They are lower bounds, so the emptiness proves nothing and the
        flag alone must fail the run -- even with no tee_failure string."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": False,
        }) == 1

    def test_a_fact_index_divergence_fails_the_run(self):
        """#302. Every other key here reads clean -- nothing was printed, no
        commit was dropped, the capture completed -- which is exactly the
        state a silently corrupted graph produces."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {"divergence": 44, "audit_error": None},
        }) == 1

    def test_an_audit_that_could_not_run_fails_the_run(self):
        """Unverified is not verified-clean: the same reasoning as an
        incomplete stderr capture."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {"divergence": 0, "audit_error": "RuntimeError: boom"},
        }) == 1

    def test_a_zero_divergence_audit_is_clean(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {"divergence": 0, "audit_error": None},
        }) == 0

    def test_a_duplicate_introduced_by_fails_the_run(self):
        """#287, gated with no tolerance on the strength of a measurement:
        the 831-commit at-scale graph carries 3150 :introduced-by facts across
        3150 entities, every one of them holding exactly one. A clean graph
        has zero, so any nonzero is a real defect rather than a threshold to
        be tuned.

        Note every other key here, INCLUDING divergence, reads clean. That is
        not a contrived case -- it is the only case: both values reach the
        index too, so the two witnesses agree perfectly about a graph that
        must be thrown away."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {
                "divergence": 0, "audit_error": None,
                "introduced_by_duplicates": {"entities": 12, "sample": []},
            },
        }) == 1

    def test_a_zero_duplicate_count_is_clean(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {
                "divergence": 0, "audit_error": None,
                "introduced_by_duplicates": {"entities": 0, "sample": []},
            },
        }) == 0

    def test_a_pre_287_audit_stays_clean_for_old_metrics(self):
        """A metrics file from a harness that HAD the fact audit but not this
        check carries the outer key and not the inner one. It cannot be
        retro-audited, so it must not be retro-failed -- the same precedent as
        an absent fact_audit."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {"divergence": 0, "audit_error": None},
        }) == 0

    def test_an_absent_audit_stays_clean_for_old_metrics(self):
        """Same precedent as stderr_capture_complete: a metrics file written
        before this harness cannot be retro-audited."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 0

    def test_a_code_entity_with_no_introduced_by_fails_the_gate(self):
        """#316, clause 7. Every other key here reads clean, and that is not
        contrived -- it is the only case. The index is missing exactly the
        fact the graph is missing (neither was ever written), so divergence is
        0; `introduced_by_duplicates` skips anything with fewer than two
        values, so clause 6 is 0; and #313's runs printed nothing, so clauses
        1-4 pass. Without this clause the graph is green."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {
                "divergence": 0, "audit_error": None,
                "introduced_by_duplicates": {"entities": 0, "sample": []},
                "entities_without_introduced_by": {
                    "entities": 3, "code_entities_scanned": 3150, "sample": [],
                },
            },
        }) == 1

    def test_a_zero_orphan_count_is_clean(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {
                "divergence": 0, "audit_error": None,
                "introduced_by_duplicates": {"entities": 0, "sample": []},
                "entities_without_introduced_by": {
                    "entities": 0, "code_entities_scanned": 3150, "sample": [],
                },
            },
        }) == 0

    def test_a_zero_denominator_does_not_fail_the_gate(self):
        """A graph holding no code entities at all is not a defect -- the
        query benchmark's own graph can be one. The report says the check
        proved nothing about it; the gate does not turn that into a failure,
        because there is no condemned graph here to warn anyone off."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {
                "divergence": 0, "audit_error": None,
                "entities_without_introduced_by": {
                    "entities": 0, "code_entities_scanned": 0, "sample": [],
                },
            },
        }) == 0

    def test_a_pre_316_audit_stays_clean_for_old_metrics(self):
        """Outer key present, this one absent. Same precedent as the #287
        clause above: a graph that was never asked cannot be retro-failed."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "fact_audit": {
                "divergence": 0, "audit_error": None,
                "introduced_by_duplicates": {"entities": 0, "sample": []},
            },
        }) == 0

    def test_a_graph_that_reads_back_empty_fails_even_with_a_matching_index(self):
        """The audit's blind spot: two empty witnesses agree perfectly.
        commits_ingested is the independent count that catches it."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "commits_ingested": 808,
            "fact_audit": {"divergence": 0, "audit_error": None, "graph_facts": 0},
        }) == 1

    def test_a_zero_commit_run_is_not_failed_for_having_no_facts(self):
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
            "commits_ingested": 0,
            "fact_audit": {"divergence": 0, "audit_error": None, "graph_facts": 0},
        }) == 0

    def test_a_complete_capture_with_no_signals_is_still_clean(self):
        """Counterpart to the above: the flag must fail only when explicitly
        False, or it would fail every clean run too."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [],
            "error_signals": [],
            "stderr_capture_complete": True,
        }) == 0


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with two commits (mirrors tests/test_mcp_server.py's fixture)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)
    return repo


class TestExitCodeCommitCensusClause:
    """#317, clause 8. The generalisation of the `graph_facts == 0` clause:
    that one catches a graph reading back COMPLETELY empty, this one catches
    one commit missing out of 847."""

    def test_a_failed_census_fails_the_run(self):
        """Every other key here reads clean, and that is the whole point. A
        commit that was never written is absent from the graph AND from its
        fact index -- they are written from the same triples in the same
        transaction boundary -- so clause 5's two witnesses agree perfectly;
        clauses 6 and 7 are well-formedness checks on entities that exist, and
        a commit that produced no entities produces nothing for either; and a
        run that silently dropped one still prints nothing, so clauses 1-4
        pass. Without this clause the graph is green."""
        assert _exit_code({
            "final_status": "complete",
            "skipped_commits": [], "error_signals": [],
            "stderr_capture_complete": True,
            "commits_ingested": 846,
            "fact_audit": {
                "divergence": 0, "audit_error": None, "graph_facts": 31377,
                "introduced_by_duplicates": {"entities": 0},
                "entities_without_introduced_by": {
                    "entities": 0, "code_entities_scanned": 3323,
                },
            },
            "commit_census": {
                "repo_commits": 847, "walk_claimed": 846,
                "graph_commit_entities": 846, "ok": False,
            },
        }) == 1

    def test_a_clean_census_passes(self):
        assert _exit_code({
            "final_status": "complete",
            "commit_census": {
                "repo_commits": 847, "walk_claimed": 847,
                "graph_commit_entities": 847, "ok": True,
            },
        }) == 0

    def test_an_absent_census_key_stays_clean(self):
        """A metrics file from a harness that predates this census. Same
        precedent as an absent fact_audit: a run that was never asked cannot
        be retro-failed."""
        assert _exit_code({
            "final_status": "complete", "commits_ingested": 847,
        }) == 0

    def test_a_census_that_could_not_run_fails(self):
        """Unverified is not verified-clean -- the reasoning of clause 4 and of
        fact_audit's audit_error. commit_census sets ok False on a
        census_error, so this clause needs no separate term for it."""
        assert _exit_code({
            "final_status": "complete",
            "commit_census": {
                "repo_commits": 0, "walk_claimed": 847,
                "graph_commit_entities": 847, "ok": False,
                "census_error": "FileNotFoundError: git",
            },
        }) == 1

    def test_an_empty_repo_census_is_not_failed(self):
        """`proved_nothing` is reported, not failed: a repo holding no commits
        is not a defect. Mirrors clause 7's zero-denominator handling."""
        assert _exit_code({
            "final_status": "complete",
            "commit_census": {
                "repo_commits": 0, "walk_claimed": 0,
                "graph_commit_entities": 0, "ok": True,
                "proved_nothing": True,
            },
        }) == 0


def test_exit_code_fails_on_orphaned_commits():
    assert _exit_code({
        "commit_census": {"ok": True, "orphaned_commits": {
            "entities": 3, "proved_nothing": False}},
    }) == 1


def test_exit_code_ignores_orphans_when_nothing_was_proved():
    """A branch mismatch or an absent :ingestion/branch means the count is
    uninterpretable, not clean-with-findings."""
    assert _exit_code({
        "commit_census": {"ok": True, "orphaned_commits": {
            "entities": 7, "proved_nothing": True}},
    }) == 0


def test_exit_code_clean_when_key_absent():
    """A metrics file from a harness predating this check cannot be
    retro-failed -- same precedent as stderr_capture_complete and fact_audit."""
    assert _exit_code({"commit_census": {"ok": True}}) == 0


class TestCommitCensusReachesTheMetrics:
    """The pure comparison is tested in tests/test_at_scale_commit_census.py.
    What THIS needs to prove is that the harness hands it the right three
    numbers off a real repo and a real graph -- a pure function cannot."""

    @pytest.mark.asyncio
    async def test_a_real_run_censuses_its_own_commits(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )
        census = metrics["commit_census"]
        # The fixture repo has exactly two commits. Asserted as a literal
        # rather than against metrics["commits_ingested"], which is the very
        # number under test -- comparing the census to its own input would
        # pass on a census that echoed it.
        assert census["repo_commits"] == 2
        assert census["walk_claimed"] == 2
        assert census["graph_commit_entities"] == 2
        assert census["ok"] is True
        assert census["census_error"] is None
        assert _exit_code(metrics) == 0

    @pytest.mark.asyncio
    async def test_the_census_records_the_ref_it_used(self, git_repo, tmp_path):
        """The ref is the half that was silently wrong in _run_ingestion's own
        repo_total (#317), so the census must say which one it counted rather
        than leaving a reader to assume."""
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", tmp_path / "bench.graph", poll_interval=0.05
        )
        assert metrics["commit_census"]["ref"] == "HEAD"

    @pytest.mark.asyncio
    async def test_the_denominator_is_nonzero_on_a_real_run(self, git_repo, tmp_path):
        """The positive control, in the test as well as in the artifact. Three
        counts that are all zero also agree, so a census reporting ok True over
        a repo it never read would look identical to a clean one."""
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", tmp_path / "bench.graph", poll_interval=0.05
        )
        assert metrics["commit_census"]["repo_commits"] > 0
        assert metrics["commit_census"]["proved_nothing"] is False


class TestRunIngestionBenchmark:
    @pytest.mark.asyncio
    async def test_returns_expected_metric_keys(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert set(metrics.keys()) == _EXPECTED_METRIC_KEYS

    @pytest.mark.asyncio
    async def test_records_the_resolved_graph_path(self, git_repo, tmp_path):
        """#256 review round 5. The probe recording the path it was HANDED
        says nothing about whether that was the right graph; the pairing is
        only auditable if the metrics record their own side of it. Resolved,
        so a relative invocation still pairs with the probe's resolved path.
        """
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )
        assert metrics["graph_path"] == str(graph_path.resolve())

    @pytest.mark.asyncio
    async def test_a_clean_run_reports_a_complete_capture_and_no_signals(self, git_repo, tmp_path):
        """The tee spans the whole run, so a healthy two-commit ingestion must
        come back with a complete capture and empty signal lists -- and pass
        the gate."""
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["stderr_capture_complete"] is True
        assert metrics["skipped_commits"] == []
        assert metrics["error_signals"] == []
        assert metrics["correction_sweep_skipped"] == 0
        assert "tee_failure" not in metrics
        assert _exit_code(metrics) == 0

    @pytest.mark.asyncio
    async def test_a_clean_run_diverges_from_its_fact_index_by_exactly_zero(
        self, git_repo, tmp_path
    ):
        """#302. The gate has no tolerance, so this is the load-bearing claim:
        a real ingestion, through the real harness, must produce a graph and
        an index that agree fact for fact. Anything above zero here would be
        permanent false red on the nightly, not a caught corruption.

        `graph_facts > 0` is not decoration -- an audit of an empty graph
        against an empty index also diverges by zero. Neither is the
        independent re-count below: asserting only on the numbers the audit
        reported about itself passes just as happily when the harness stops
        auditing and hands back a constant (verified by ablation).
        """
        import mcp_server

        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )
        audit = metrics["fact_audit"]
        assert audit["audit_error"] is None
        assert audit["graph_facts"] > 0
        assert audit["divergence"] == 0, audit["missing_from_graph_sample"]
        assert _exit_code(metrics) == 0

        recount = mcp_server.handle_minigraf_query(
            "[:find (count ?e) :where [?e ?a ?v]]"
        )["results"][0][0]
        assert audit["graph_facts"] == recount
        assert audit["index_current_rows"] == recount

    @pytest.mark.asyncio
    async def test_checkpoint_summary_is_present_and_self_consistent(self, git_repo, tmp_path):
        """#241 Task 6: the benchmark's acceptance criterion is a run that
        reports realised checkpoint duty, not just poll duty. mcp_server
        publishes this into _ingest_progress["checkpoint_summary"] just
        before _run_ingestion discards the policy that held the counters;
        the harness must carry it through into its own returned metrics.

        #270: the status/error check comes FIRST, and it is not decoration.
        _run_ingestion publishes the summary from two `finally` blocks, both
        guarded on `_ingest_checkpoint_policy is not None`, and the policy
        used to be constructed ~100 lines into the try -- so a run dying in
        the git enumerations or the preload published NOTHING, and this
        test's `summary is not None` was the assertion that fired. It
        reported `assert None is not None` while _run_ingestion's swallowed
        exception sat unread in _ingest_progress["error"], which is why #270
        spent 48 sampled runs unable to name a cause.

        Both halves of that are now closed, and they are independent
        defences -- keep both. The policy is constructed as the FIRST
        statement in the try (mcp_server.py, `_CheckpointPolicy(
        _checkpoint_duty_from_env())`), so there is no longer a window in
        which a failure publishes no summary; that is covered directly by
        tests/test_mcp_server.py TestCheckpointDutySummaryPublication::
        test_summary_survives_a_pre_policy_failure. And asserting the status
        first puts the error text in the failure message, so any OTHER cause
        of an incomplete run still names itself here rather than surfacing
        as a bare assertion on a downstream key.

        The two assertions this test used to make about total_seconds and
        realised_duty were DELETED as tautologies, the same call
        test_poll_duty_fraction_is_a_bounded_fraction got below.
        total_seconds is a sum of time.monotonic deltas, so `>= 0.0` cannot
        fail; and summary() computes realised_duty as `self.total_seconds /
        elapsed` from the very same `elapsed` float it returns, so
        `realised_duty == approx(total_seconds / elapsed_seconds)` compares
        an expression against itself. #270 listed that one as a live
        candidate for the flake; it can never fail. The non-vacuous version
        of both -- a policy driven by a fake clock through known checkpoint
        costs -- is tests/test_mcp_server.py
        TestCheckpointPolicy::
        test_summary_reports_checkpoints_suppressed_total_seconds_and_duty,
        which drives a policy through known costs on a fake clock.
        """
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["final_status"] == "complete", (
            f"ingestion did not complete: {metrics['ingest_error']!r}"
        )
        summary = metrics["checkpoint_summary"]
        assert summary is not None
        assert summary["checkpoints"] >= 1

    @pytest.mark.asyncio
    async def test_reports_no_ingest_error_on_a_clean_run(self, git_repo, tmp_path):
        """#270. The key must be present-and-None on a healthy run, not
        absent: a reader that has to distinguish "no error" from "this
        harness predates the key" cannot do it from an absent key, and the
        `metrics['ingest_error']` reads in the tests above would KeyError."""
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["ingest_error"] is None

    @pytest.mark.asyncio
    async def test_surfaces_a_failure_in_the_preload(
        self, git_repo, tmp_path, monkeypatch, capfd
    ):
        """#270. A failure in _load_ingestion_preload_state must name itself
        in the metrics and on the tee.

        This test was written when this injection point sat BEFORE
        _run_ingestion constructed its _CheckpointPolicy, and it asserted
        `checkpoint_summary is None` as the precondition that made the window
        distinct. That window is CLOSED -- the policy is now the first
        statement in the try -- so the assertion is inverted here rather than
        deleted: an all-zeros summary is the positive control that the move
        actually took effect on the path that motivated it. The
        summary-publication behaviour itself is owned by
        tests/test_mcp_server.py TestCheckpointDutySummaryPublication::
        test_summary_survives_a_pre_policy_failure, which is ablation-proven
        against the construction's old position; this one keeps its original
        subject, which is the ERROR REPORTING.

        That subject is undiminished by the fix, and deliberately still
        tested: a zeros summary says a run checkpointed nothing, which is
        also what a legitimate zero-commit run says, so it can never be the
        thing that names a failure. _run_ingestion swallows the exception
        into _ingest_progress["error"], and before `ingest_error` existed the
        cause reached neither the metrics JSON, nor scan_ingestion_stderr's
        error_signals, nor the test's failure message.

        Ablation-proven: with the `ingest_error` key removed from
        run_ingestion_benchmark's result dict, this test fails on the
        KeyError at `metrics["ingest_error"]`, not on the assertion.
        """
        import mcp_server

        def boom(*a, **k):
            raise RuntimeError("injected pre-policy failure")

        monkeypatch.setattr(mcp_server, "_load_ingestion_preload_state", boom)
        graph_path = tmp_path / "bench.graph"
        # capfd.disabled(), or the error_signals half of this test is
        # untestable: pytest's default fd capture leaves sys.stderr bound to
        # its own temp file, so _run_ingestion's print() would never reach the
        # fd 2 that tee_stderr dup2s -- the same layering
        # TestTeeStderr::test_captures_parent_process_writes documents by
        # using os.write(2, ...) instead of print(). Suspending capture for
        # the run restores the real fd 2 and makes the tee see what a real
        # at-scale run's tee sees.
        with capfd.disabled():
            metrics = await run_ingestion_benchmark(
                str(git_repo), "HEAD", graph_path, poll_interval=0.05
            )

        # Positive control for #270's move: this failure lands above every
        # statement that could have checkpointed, so the summary is present
        # and all zeros. Before the move it was absent entirely.
        assert metrics["checkpoint_summary"] is not None
        assert metrics["checkpoint_summary"]["checkpoints"] == 0
        assert metrics["checkpoint_summary"]["total_seconds"] == 0.0
        # And it still cannot name the failure -- a zeros summary is
        # indistinguishable from a legitimate run that checkpointed nothing.
        # That is what the next three assertions are for.
        assert metrics["final_status"] == "error"
        assert "injected pre-policy failure" in metrics["ingest_error"]
        # And the gate still fails the run, as it did before.
        assert _exit_code(metrics) == 1
        # #270 follow-up: the same failure must also reach the tee, so it is
        # machine-visible and not only readable in ingest_error. Before
        # _run_ingestion printed it, nothing was written to fd 2 on this
        # path and error_signals was EMPTY for a run that produced nothing --
        # a clean-looking record of a dead run, the fail-open shape #256
        # exists to close.
        assert [sig["pattern"] for sig in metrics["error_signals"]] == ["ingestion_failed"]
        assert "injected pre-policy failure" in metrics["error_signals"][0]["line"]

    # test_poll_duty_fraction_is_a_bounded_fraction was DELETED here (final
    # whole-branch review). Both of its assertions were tautologies:
    # poll_count is *defined* as len(poll_offsets)
    # (run_ingestion_benchmark.py:171), and poll_duty_fraction is a serial sum
    # of latencies measured INSIDE the same wall_clock it divides by
    # (:148-149), so 0.0 <= f <= 1.0 cannot fail. It had zero discriminating
    # power while being counted as #242 coverage -- the exact pattern this
    # project has already shipped four times. Its only non-vacuous content
    # (key presence) is covered by test_returns_expected_metric_keys above,
    # and the real offsets/samples alignment by
    # TestPollerDoesNotStarveTheEventLoop::test_returns_poll_offsets_aligned_with_samples,
    # which is ablation-proven.

    @pytest.mark.asyncio
    async def test_ingests_all_commits(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["commits_ingested"] == 2
        assert metrics["final_status"] == "complete"

    @pytest.mark.asyncio
    async def test_wall_clock_and_sizes_are_positive(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert metrics["wall_clock_seconds"] > 0
        assert metrics["peak_rss_kb"] > 0
        assert metrics["graph_size_bytes"] > 0
        assert metrics["index_size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_default_branch_resolved_when_none_passed(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        # git_repo has no "main"/"master" branch name set explicitly by `git init`
        # in this sandbox's git config, so branch=None must still resolve to
        # something _run_ingestion can walk without raising.
        metrics = await run_ingestion_benchmark(str(git_repo), None, graph_path, poll_interval=0.05)
        assert metrics["commits_ingested"] == 2


class _ReachedBenchmark(Exception):
    """Raised by the sentinel below when main() gets past its argument checks.

    Ablating the #330 guard without this would drive a REAL ingestion out of
    main(), which appends to the repo's own evals/at_scale/benchmark.md and
    writes into evals/at_scale/results/. An ablation that edits tracked files
    is one nobody runs a second time, so the counterfactual has to stay cheap
    and side-effect-free to stay worth running.
    """


class TestMainRefusesALiteralHeadBranch:
    """#330. `--branch HEAD` is truthy, so it does not *also* select the
    checked-out ref -- it DEFEATS `branch or _default_git_branch(repo_path)`
    outright, and the literal reaches `_git_commits`' range spec,
    `_run_ingestion`'s `repo_total` and #317's commit census alike. Every one
    of them inherits the same wrong ref, so they stay internally consistent
    with each other while describing whatever is checked out rather than the
    branch being tracked; nothing in the tier disagrees with anything else.
    The at-scale nightly shipped exactly that.

    THE GUARD IS ON THE CLI ARGUMENT, NEVER ON `resolved_branch`, for two
    independent reasons -- neither of which the other covers:

      * `_default_git_branch` legitimately RETURNS "HEAD", as its documented
        last-resort fallback when neither `main` nor `master` resolves. A
        check downstream of the `or` would refuse that supported path, and
        this change is not entitled to make the fallback unreachable.
      * `run_ingestion_benchmark(repo, "HEAD", ...)` is the CORRECT call for a
        throwaway fixture repo whose branch name is whatever `git init` chose,
        which is how most tests in this file drive it (see
        `test_ingests_all_commits`).

    So what is guarded is a human writing an argument, which is precisely
    where the defect happened and the only place it can be caught without
    breaking a supported path.
    """

    @staticmethod
    def _argv(tmp_path, *extra):
        return [
            "run_ingestion_benchmark",
            "--repo-path", ".",
            "--graph-path", str(tmp_path / "bench.graph"),
            *extra,
        ]

    @staticmethod
    def _sentinel(monkeypatch):
        def _boom(*args, **kwargs):
            raise _ReachedBenchmark()

        monkeypatch.setattr(rib, "run_ingestion_benchmark", _boom)

    def test_a_literal_head_is_refused(self, tmp_path, monkeypatch):
        self._sentinel(monkeypatch)
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path, "--branch", "HEAD"))
        with pytest.raises(SystemExit) as excinfo:
            rib.main()
        assert "HEAD" in str(excinfo.value)

    def test_a_real_branch_name_is_not_refused(self, tmp_path, monkeypatch):
        # NEGATIVE CONTROL. A guard that refused every --branch would satisfy
        # the test above while breaking the argument outright, and a refusal
        # is indistinguishable from a correct one when nothing ever gets past
        # it. This is the assertion that says the guard discriminates.
        self._sentinel(monkeypatch)
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path, "--branch", "master"))
        with pytest.raises(_ReachedBenchmark):
            rib.main()

    def test_an_omitted_branch_is_not_refused(self, tmp_path, monkeypatch):
        # The shape the nightly now uses: no --branch at all, leaving
        # _default_git_branch to resolve it inside run_ingestion_benchmark.
        # Guarded separately from the case above because argparse's default is
        # None rather than a string, so a guard written against `.upper()` or
        # a bare `in` would fail here and nowhere else.
        self._sentinel(monkeypatch)
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path))
        with pytest.raises(_ReachedBenchmark):
            rib.main()


class TestBenchmarkTracePath:
    """#260: --trace-path arms the per-commit trace and records where it went."""

    @pytest.mark.asyncio
    async def test_metrics_record_the_resolved_trace_path(self, git_repo, tmp_path):
        """Provenance. #275's whole lesson was that a benchmark which does not
        record what it measured produces an artifact nobody can interpret."""
        graph_path = tmp_path / "g.graph"
        trace = tmp_path / "trace.jsonl"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05, trace_path=trace,
        )
        assert metrics["trace_path"] == str(trace.resolve())
        assert trace.exists()

    @pytest.mark.asyncio
    async def test_untraced_run_records_no_trace_path_key(self, git_repo, tmp_path):
        """Absent must mean 'not traced', never 'traced and empty' -- the same
        three-state discipline benchmark.md's residue rows use."""
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", tmp_path / "g.graph", poll_interval=0.05,
        )
        assert "trace_path" not in metrics

    @pytest.mark.asyncio
    async def test_env_var_does_not_leak_past_the_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        import os
        monkeypatch.delenv("MINIGRAF_INGEST_TRACE_PATH", raising=False)
        await run_ingestion_benchmark(
            str(git_repo), "HEAD", tmp_path / "g.graph", poll_interval=0.05,
            trace_path=tmp_path / "t.jsonl",
        )
        assert "MINIGRAF_INGEST_TRACE_PATH" not in os.environ

    @pytest.mark.asyncio
    async def test_compare_ignore_run_is_not_traced(self, git_repo, tmp_path):
        """The hazard this task exists to close: the trace must be armed ONLY
        around the measured run. compare_ignore drives a SECOND, complete
        ingestion into a different graph purely to size it -- if the env var
        stayed armed across that second call, its records would land in the
        same JSONL file (opened in append mode) and double the line count,
        with nothing in the data marking where one run ended and the other
        began. git_repo has exactly two commits, so a correctly-scoped trace
        has exactly two records; a leaked one would have four."""
        graph_path = tmp_path / "g.graph"
        trace = tmp_path / "trace.jsonl"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05,
            compare_ignore=True, trace_path=trace,
        )
        assert metrics["commits_ingested"] == 2
        lines = trace.read_text().splitlines()
        assert len(lines) == metrics["commits_ingested"] == 2


class TestCompareIgnore:
    @pytest.mark.asyncio
    async def test_ignore_comparison_present_when_requested(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05, compare_ignore=True
        )
        assert "ignore_comparison" in metrics
        comp = metrics["ignore_comparison"]
        assert comp["with_ignore_graph_size_bytes"] > 0
        assert comp["without_ignore_graph_size_bytes"] > 0
        assert comp["delta_bytes"] == (
            comp["without_ignore_graph_size_bytes"] - comp["with_ignore_graph_size_bytes"]
        )

    @pytest.mark.asyncio
    async def test_ignore_comparison_absent_by_default(self, git_repo, tmp_path):
        graph_path = tmp_path / "bench.graph"
        metrics = await run_ingestion_benchmark(str(git_repo), "HEAD", graph_path, poll_interval=0.05)
        assert "ignore_comparison" not in metrics


def _emit_on_fd_2_after_ingesting(monkeypatch, payload: bytes):
    """Wrap mcp_server._run_ingestion so it runs for real and then writes
    `payload` to fd 2.

    os.write(2, ...), NOT print(file=sys.stderr): under pytest's fd capture
    sys.stderr is not fd 2 at all, so a print would never enter the tee pipe
    and the test would pass on a permanently blind wiring -- the exact
    fail-open shape being tested against. mcp_server's real skip sites do
    reach fd 2 (print(file=sys.stderr) with no capture in front of it, plus
    the pool children which inherit fd 2 and have no sys.stderr of the
    parent's at all), which is why the tee is fd-level in the first place.
    """
    import mcp_server

    real_run_ingestion = mcp_server._run_ingestion

    async def run_then_emit(*args, **kwargs):
        await real_run_ingestion(*args, **kwargs)
        os.write(2, payload)

    monkeypatch.setattr(mcp_server, "_run_ingestion", run_then_emit)


class TestCapturedStderrReachesTheMetrics:
    """The POSITIVE direction of this task's claim: a line emitted on fd 2
    during a tee'd run must actually arrive in the metrics.

    Every other test here is satisfied by a permanently blind wiring --
    scan_ingestion_stderr("") also yields empty lists, and a clean run's
    empty lists are byte-identical to a blind scanner's. These two are the
    only tests that can tell the difference, so they are what stops the
    verification from failing open."""

    @pytest.mark.asyncio
    async def test_a_real_skip_line_on_fd_2_reaches_skipped_commits(
        self, git_repo, tmp_path, monkeypatch
    ):
        _emit_on_fd_2_after_ingesting(
            monkeypatch,
            b"[_run_ingestion] skipping commit deadbee1 "
            b"('some subject'): write failed: boom\n",
        )
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert metrics["skipped_commits"] == ["deadbee1"]
        assert metrics["stderr_capture_complete"] is True
        # final_status and commits_ingested are both blind to this -- the
        # scanned keys are the only thing that turns it into a failure.
        assert metrics["final_status"] == "complete"
        assert _exit_code(metrics) == 1

    @pytest.mark.asyncio
    async def test_a_real_251_signature_on_fd_2_reaches_error_signals(
        self, git_repo, tmp_path, monkeypatch
    ):
        # The live-numbers form #251 actually reproduced; a literal-string
        # scanner would match nothing here and report all-clear.
        _emit_on_fd_2_after_ingesting(
            monkeypatch, b"Page 130 out of bounds (total pages: 113)\n"
        )
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert [s["pattern"] for s in metrics["error_signals"]] == ["page_out_of_bounds"]
        assert "total pages: 113" in metrics["error_signals"][0]["line"]
        assert metrics["stderr_capture_complete"] is True
        assert _exit_code(metrics) == 1


class TestTeeFailureDoesNotDestroyTheRun:
    """tee_stderr() raises TeeStderrFailure from its own teardown, which runs
    AFTER _run_ingestion has returned. If run_ingestion_benchmark let that
    propagate, a ~25-minute run would end in a traceback with no metrics JSON
    and no report row -- the instrument's failure destroying the measurement.
    It must instead produce the full metrics dict, flagged and failing."""

    @staticmethod
    def _failing_tee(exc_factory=lambda: TeeStderrFailure("simulated pump failure")):
        """A tee_stderr() stand-in that captures for real, then raises on exit
        exactly where the real one does -- in its own finally, after the body
        has completed."""
        real_tee = rib.tee_stderr

        @contextlib.contextmanager
        def failing_tee():
            try:
                with real_tee() as capture:
                    yield capture
            finally:
                raise exc_factory()

        return failing_tee

    @pytest.mark.asyncio
    async def test_full_metrics_survive_a_tee_failure(self, git_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(rib, "tee_stderr", self._failing_tee())
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        # Nothing is actually lost on this path: the raise lands after
        # _run_ingestion returned, so every metric below was still readable.
        assert _EXPECTED_METRIC_KEYS <= set(metrics)
        assert metrics["commits_ingested"] == 2
        assert metrics["final_status"] == "complete"
        assert metrics["wall_clock_seconds"] > 0
        assert metrics["graph_size_bytes"] > 0
        assert metrics["checkpoint_summary"] is not None

    @pytest.mark.asyncio
    async def test_a_tee_failure_is_flagged_and_fails_the_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(rib, "tee_stderr", self._failing_tee())
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert metrics["stderr_capture_complete"] is False
        assert "simulated pump failure" in metrics["tee_failure"]
        # The lists are lower bounds here, so their emptiness must NOT read as
        # a clean run. This is the whole point of the third _exit_code clause.
        assert metrics["skipped_commits"] == []
        assert metrics["error_signals"] == []
        assert _exit_code(metrics) == 1

    @pytest.mark.asyncio
    async def test_a_body_exception_displaced_by_the_tee_raise_is_preserved(
        self, git_repo, tmp_path, monkeypatch
    ):
        """A raise from the context manager's finally DISPLACES whatever the
        body was raising: the real ingestion crash comes out only as
        TeeStderrFailure.__context__. A handler that recorded the tee failure
        alone would hide a genuine crash behind an instrument fault."""
        import mcp_server

        async def boom(*_args, **_kwargs):
            raise RuntimeError("ingestion exploded")

        monkeypatch.setattr(mcp_server, "_run_ingestion", boom)
        monkeypatch.setattr(rib, "tee_stderr", self._failing_tee())
        graph_path = tmp_path / "bench.graph"

        metrics = await rib.run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05
        )

        assert "ingestion exploded" in metrics["tee_failure_context"]
        assert metrics["stderr_capture_complete"] is False
        assert _exit_code(metrics) == 1

    @pytest.mark.asyncio
    async def test_the_handlers_own_logging_cannot_destroy_the_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        """The handler logs to stderr -- and on the one path that matters
        most, that stderr is broken. When guard.restore()'s dup2 is what
        failed, fd 2 still points at the tee pipe, whose read end teardown
        then closes, so every write raises BrokenPipeError. Unguarded, that
        propagates out of the except clause and destroys the metrics dict the
        catch exists to preserve."""

        class _RecordingStderr:
            """Genuinely fd 2 (so the BrokenPipeError comes from the OS, not
            from a stub), while recording that it really did raise -- without
            that positive control this test would pass vacuously in any
            environment where the write happened to succeed."""

            def __init__(self) -> None:
                self._raw = io.TextIOWrapper(
                    io.FileIO(2, "w", closefd=False), line_buffering=True
                )
                self.raised: list[BaseException] = []

            def write(self, s):
                try:
                    return self._raw.write(s)
                except BaseException as exc:
                    self.raised.append(exc)
                    raise

            def flush(self):
                try:
                    return self._raw.flush()
                except BaseException as exc:
                    self.raised.append(exc)
                    raise

        real_tee = rib.tee_stderr

        @contextlib.contextmanager
        def tee_that_fails_to_restore_fd_2():
            with real_tee() as capture:
                yield capture
            # Exactly the state a failed guard.restore() leaves behind: fd 2
            # points at a pipe whose read end is already closed. Done AFTER
            # the real tee's teardown has joined its pump, so nothing can
            # repair it behind our back -- the reviewer's reproduction was
            # racy against the pump's emergency valve; this one is not.
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            os.dup2(write_fd, 2)
            os.close(write_fd)
            raise TeeStderrFailure("simulated restore failure")

        rescue_fd = os.dup(2)
        recorder = _RecordingStderr()
        monkeypatch.setattr(rib, "tee_stderr", tee_that_fails_to_restore_fd_2)
        monkeypatch.setattr(sys, "stderr", recorder)
        graph_path = tmp_path / "bench.graph"

        try:
            metrics = await rib.run_ingestion_benchmark(
                str(git_repo), "HEAD", graph_path, poll_interval=0.05
            )
        finally:
            # Repair fd 2 before pytest (or anything else) writes to it.
            os.dup2(rescue_fd, 2)
            os.close(rescue_fd)

        assert any(isinstance(exc, BrokenPipeError) for exc in recorder.raised), (
            "the handler's stderr never actually broke, so this test proved "
            f"nothing: {recorder.raised}"
        )
        assert _EXPECTED_METRIC_KEYS <= set(metrics)
        assert metrics["commits_ingested"] == 2
        assert metrics["stderr_capture_complete"] is False
        assert _exit_code(metrics) == 1


import asyncio
import time as _time

from evals.at_scale.run_ingestion_benchmark import _poll_during_ingestion


class TestPollerDoesNotStarveTheEventLoop:
    """#242: the poll must not block the event loop, and its share of
    _db_native_lock must stay bounded as the polled query grows."""

    @pytest.mark.asyncio
    async def test_event_loop_stays_responsive_while_poll_query_blocks(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda _q: _time.sleep(0.05))

        ticks: list[float] = []

        async def heartbeat(stop: asyncio.Event) -> None:
            while not stop.is_set():
                ticks.append(_time.perf_counter())
                await asyncio.sleep(0.01)

        stop = asyncio.Event()
        ingest_task = asyncio.create_task(asyncio.sleep(0.6))
        hb = asyncio.create_task(heartbeat(stop))
        await _poll_during_ingestion(ingest_task, poll_interval=0.0, duty_factor=0.0)
        await ingest_task
        stop.set()
        await hb

        # A free 0.6s loop ticking every 10ms yields ~60 ticks. With the poll
        # blocking the loop for 50ms per iteration it yields ~12 (one per poll).
        # 30 sits well clear of both.
        assert len(ticks) >= 30, f"event loop was starved: only {len(ticks)} ticks"

    @pytest.mark.asyncio
    async def test_interval_backs_off_when_the_polled_query_is_slow(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda _q: _time.sleep(0.05))

        ingest_task = asyncio.create_task(asyncio.sleep(1.1))
        _status, query_latencies, _offsets = await _poll_during_ingestion(
            ingest_task, poll_interval=0.0, duty_factor=10.0
        )
        await ingest_task

        # duty_factor=10 against a 50ms query forces a ~500ms sleep, so a 1.1s
        # run admits about 2-3 polls. Without the backoff it would poll
        # continuously and record ~20.
        assert len(query_latencies) <= 5, f"interval did not back off: {len(query_latencies)} polls"

    @pytest.mark.asyncio
    async def test_returns_poll_offsets_aligned_with_samples(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda _q: None)

        ingest_task = asyncio.create_task(asyncio.sleep(0.2))
        status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
            ingest_task, poll_interval=0.02, duty_factor=10.0
        )
        await ingest_task

        assert len(poll_offsets) == len(status_latencies) == len(query_latencies)
        assert poll_offsets == sorted(poll_offsets)


class TestResolveGraphPath:
    def test_without_an_argument_yields_a_temp_path_that_is_cleaned_up(self):
        """Omitting --graph-path must behave exactly as before this change --
        the recurring benchmark must not change."""
        with resolve_graph_path(None) as path:
            tmpdir = path.parent
            assert tmpdir.exists()
            assert path.name == "bench.graph"
        assert not tmpdir.exists()

    def test_with_an_argument_yields_that_path_and_keeps_it(self, tmp_path):
        target = tmp_path / "persistent" / "run.graph"
        with resolve_graph_path(str(target)) as path:
            assert path == target
            path.write_text("graph bytes")
        assert target.exists(), "a persistent graph must survive the context"

    def test_creates_the_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "run.graph"
        with resolve_graph_path(str(target)) as path:
            assert path.parent.is_dir()

    def test_refuses_an_existing_path(self, tmp_path):
        """CLAUDE.md's standing rule: graphs are rebuilt, never re-ingested in
        place. run_ingestion_benchmark's docstring states the same
        precondition; this enforces it."""
        target = tmp_path / "already.graph"
        target.write_text("pre-existing")
        with pytest.raises(SystemExit, match="already exists"):
            with resolve_graph_path(str(target)):
                pass

    def test_refuses_a_stale_wal_even_when_the_main_file_is_absent(self, tmp_path):
        """A crashed run can leave `<path>.wal` behind with the main graph
        file deleted (or never renamed into place). minigraf's open()
        replays a leftover .wal automatically, so a check that only looks at
        the main file would silently resurrect the dead run's writes."""
        target = tmp_path / "run.graph"
        Path(f"{target}.wal").write_text("stale wal")
        with pytest.raises(SystemExit, match="already exists"):
            with resolve_graph_path(str(target)):
                pass

    def test_refuses_a_stale_index_even_when_the_main_file_is_absent(self, tmp_path):
        """Same hazard as the .wal case, for the fact index sidecar. Uses
        fact_index.index_path_for so this stays correct under a
        MINIGRAF_INDEX_PATH override, same as the implementation."""
        import fact_index

        target = tmp_path / "run.graph"
        Path(fact_index.index_path_for(str(target))).write_text("stale index")
        with pytest.raises(SystemExit, match="already exists"):
            with resolve_graph_path(str(target)):
                pass


class TestProgressInterval:
    """#222 phase 4: mid-run progress on STDOUT (stderr is teed and scanned by
    stderr_capture), and never an extra status call."""

    def _status(self):
        return {
            "status": "running", "phase": "converging",
            "this_run": {"to_retire": 9000, "retired": 412, "seconds_since_progress": 3.2},
            "streams": {
                "forward": {"retired": 206, "rate_per_min": 11.2},
                "reverse": {"retired": 206, "rate_per_min": 11.0},
                "sweep": {"state": "waiting", "swept": 0, "to_sweep": None},
            },
            "visibility": {"verified": 8203, "total": 53289},
            "lineage": {"confirmed": 7800, "total": 53289},
        }

    def test_format(self):
        from evals.at_scale.run_ingestion_benchmark import format_progress_line
        assert format_progress_line(self._status()) == (
            "[progress] converging this_run 412/9000 · fwd 206 @11.2/min · "
            "rev 206 @11.0/min · sweep waiting · visibility 8203/53289 · "
            "lineage 7800/53289 · idle 3s"
        )

    def test_format_before_the_model_exists(self):
        from evals.at_scale.run_ingestion_benchmark import format_progress_line
        assert format_progress_line({"status": "starting"}) == "[progress] starting"

    def test_format_null_lineage_and_running_sweep(self):
        from evals.at_scale.run_ingestion_benchmark import format_progress_line
        s = self._status()
        s["lineage"]["confirmed"] = None
        s["streams"]["sweep"] = {"state": "running", "swept": 3, "to_sweep": 40}
        line = format_progress_line(s)
        assert "sweep running 3/40" in line and "lineage ?/53289" in line

    def test_format_done_sweep_with_zero_to_sweep(self):
        """ingest_progress.py sets to_sweep=0 when the sweep reaches "done" via
        "reached-ceiling" — a real state, not an absent count. A truthiness
        check on to_sweep drops the count here; must use `is not None`."""
        from evals.at_scale.run_ingestion_benchmark import format_progress_line
        s = self._status()
        s["streams"]["sweep"] = {"state": "done", "swept": 0, "to_sweep": 0}
        assert "sweep done 0/0" in format_progress_line(s)

    @pytest.mark.parametrize("interval", [None, 0.0])
    async def test_status_calls_equal_polls_with_or_without_the_flag(self, interval, monkeypatch, capsys):
        import asyncio
        import mcp_server
        from evals.at_scale.run_ingestion_benchmark import _poll_during_ingestion
        calls = {"status": 0}

        def fake_status():
            calls["status"] += 1
            return self._status()

        monkeypatch.setattr(mcp_server, "handle_minigraf_ingest_status", fake_status)
        monkeypatch.setattr(mcp_server, "handle_minigraf_query", lambda q: {"ok": True})
        task = asyncio.create_task(asyncio.sleep(0.3))
        _s, _q, offsets = await _poll_during_ingestion(task, 0.05, 1.0, progress_interval=interval)
        assert calls["status"] == len(offsets)
        out, err = capsys.readouterr()
        assert "[progress]" not in err
        assert ("[progress]" in out) is (interval is not None)
