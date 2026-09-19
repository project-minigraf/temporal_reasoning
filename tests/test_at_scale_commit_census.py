# tests/test_at_scale_commit_census.py
"""#317: the commit-entity census, the one loss no fact-level check can see.

The unit under test is PURE -- it compares three integers somebody else
collected -- so these tests hand it integers rather than a repo and a graph.
The collection half (a `git rev-list` and a graph query) is exercised against
a real repo and a real graph in tests/test_at_scale_ingestion_benchmark.py;
a pure function cannot prove the numbers it is handed mean what it assumes.
"""

import pytest

from evals.at_scale.commit_census import commit_census, orphaned_commits


def _clean(**overrides):
    """A census of a healthy 847-commit run, before overrides."""
    kwargs = dict(
        repo_commits=847,
        walk_claimed=847,
        graph_commit_entities=847,
        distinct_commit_idents=847,
        final_status="complete",
    )
    kwargs.update(overrides)
    return commit_census(**kwargs)


class TestTheCleanRun:
    def test_three_matching_counts_pass(self):
        assert _clean()["ok"] is True

    def test_every_delta_is_zero(self):
        result = _clean()
        assert result["repo_vs_walk"] == 0
        assert result["walk_vs_graph"] == 0
        assert result["repo_vs_graph"] == 0

    def test_all_three_raw_counts_survive_in_the_result(self):
        """Reported as three numbers, not one delta, so a mismatch says WHERE
        it happened -- never walked, or walked and then lost."""
        result = _clean()
        assert result["repo_commits"] == 847
        assert result["walk_claimed"] == 847
        assert result["graph_commit_entities"] == 847


class TestACommitWalkedAndThenLost:
    """The walk claims it applied N, the graph can only produce N-1."""

    def test_it_fails(self):
        assert _clean(graph_commit_entities=846)["ok"] is False

    def test_the_delta_localises_it_to_the_write(self):
        result = _clean(graph_commit_entities=846)
        assert result["walk_vs_graph"] == 1
        assert result["repo_vs_walk"] == 0

    def test_the_interpretation_names_the_write_path(self):
        assert "walk" in _clean(graph_commit_entities=846)["interpretation"]


class TestACommitNeverWalkedAtAll:
    """The case no in-process counter can see: the counter and the walk share
    the bug. Only the repo reference catches it."""

    def test_it_fails(self):
        assert _clean(walk_claimed=846, graph_commit_entities=846)["ok"] is False

    def test_the_two_in_process_numbers_agree_perfectly(self):
        """This is the point of comparing against the repo at all. A census of
        `walk_claimed` against `graph_commit_entities` alone reads 0 here."""
        result = _clean(walk_claimed=846, graph_commit_entities=846)
        assert result["walk_vs_graph"] == 0
        assert result["repo_vs_walk"] == 1
        assert result["repo_vs_graph"] == 1


class TestAnIncompleteRunIsNotFailedForWalkingFewer:
    """`final_status` has five non-running values and only one of them means
    the walk was supposed to reach the end. A `stopped` run walked fewer
    BY DESIGN, and failing it here would make every interrupted at-scale run
    red for doing exactly what it was asked."""

    @pytest.mark.parametrize("status", ["stopped", "skipped", "error"])
    def test_a_short_walk_is_not_failed(self, status):
        result = _clean(
            walk_claimed=400, graph_commit_entities=400, final_status=status
        )
        assert result["ok"] is True

    @pytest.mark.parametrize("status", ["stopped", "skipped", "error"])
    def test_the_deltas_are_still_reported(self, status):
        """Not gated is not unmeasured. The numbers still ship, so a reader
        comparing runs can see how far the walk got."""
        result = _clean(
            walk_claimed=400, graph_commit_entities=400, final_status=status
        )
        assert result["repo_vs_walk"] == 447

    def test_walk_versus_graph_IS_still_gated_on_an_incomplete_run(self):
        """The one delta that needs no completion assumption: however few
        commits the walk claimed, the graph must hold that many. A run that
        stopped at 400 and produced 399 lost one."""
        result = _clean(
            walk_claimed=400, graph_commit_entities=399, final_status="stopped"
        )
        assert result["ok"] is False
        assert result["walk_vs_graph"] == 1


class TestTheDenominatorIsThePositiveControl:
    """CLAUDE.md's standing requirement: a zero-tolerance gate reports the
    denominator that makes its zero believable. Three counts that are all
    zero also match, and a census that proved nothing must not read as a
    census that proved something."""

    def test_an_empty_repo_proves_nothing_and_is_not_failed(self):
        result = commit_census(
            repo_commits=0, walk_claimed=0, graph_commit_entities=0,
            distinct_commit_idents=0, final_status="complete",
        )
        assert result["ok"] is True
        assert result["proved_nothing"] is True

    def test_a_real_repo_is_not_marked_as_proving_nothing(self):
        assert _clean()["proved_nothing"] is False

    def test_repo_commits_is_the_denominator_and_always_ships(self):
        assert _clean()["repo_commits"] == 847


class TestTheTwelveCharIdentCollision:
    """`:commit/{hash[:12]}`. Two commits sharing a 12-char prefix collapse
    into ONE entity, so the graph legitimately holds fewer than the repo --
    and the census would otherwise blame the write path for an ident-rule
    loss. It is still a genuine loss, so it still fails; the extra number
    only makes the failure attributable."""

    def test_a_collision_is_reported(self):
        result = _clean(distinct_commit_idents=846, graph_commit_entities=846)
        assert result["ident_collisions"] == 1

    def test_a_collision_still_fails_the_census(self):
        result = _clean(distinct_commit_idents=846, graph_commit_entities=846)
        assert result["ok"] is False

    def test_the_interpretation_names_the_ident_rule(self):
        result = _clean(distinct_commit_idents=846, graph_commit_entities=846)
        assert "ident" in result["interpretation"]

    def test_a_healthy_repo_reports_zero_collisions(self):
        assert _clean()["ident_collisions"] == 0


class TestACensusThatCouldNotRun:
    """Unverified is not verified-clean -- the same reasoning as the fact
    audit's `audit_error` and the harness's `stderr_capture_complete`."""

    def test_an_error_fails_the_census(self):
        result = commit_census(
            repo_commits=0, walk_claimed=847, graph_commit_entities=847,
            distinct_commit_idents=0, final_status="complete",
            census_error="git rev-list failed: not a git repository",
        )
        assert result["ok"] is False

    def test_the_error_text_survives(self):
        result = commit_census(
            repo_commits=0, walk_claimed=847, graph_commit_entities=847,
            distinct_commit_idents=0, final_status="complete",
            census_error="boom",
        )
        assert result["census_error"] == "boom"

    def test_an_error_beats_the_empty_repo_exemption(self):
        """`repo_commits == 0` is exactly what a failed `git rev-list` leaves
        behind, so the exemption above must not swallow it."""
        result = commit_census(
            repo_commits=0, walk_claimed=0, graph_commit_entities=0,
            distinct_commit_idents=0, final_status="complete",
            census_error="boom",
        )
        assert result["ok"] is False

    def test_a_clean_census_records_no_error(self):
        assert _clean()["census_error"] is None


class TestWhichDiagnosisWinsWhenSeveralApply:
    """The deltas overlap: an ident collision produces a `walk_vs_graph` of
    exactly the same shape as a lost write. The `interpretation` must name the
    more SPECIFIC cause, or a collision reads as a write-path bug and sends a
    reader to the wrong code. Every raw delta ships regardless, so nothing is
    hidden by the choice -- only the headline."""

    def test_a_collision_beats_the_generic_write_loss_message(self):
        result = _clean(distinct_commit_idents=846, graph_commit_entities=846)
        assert result["walk_vs_graph"] == 1
        assert "ident" in result["interpretation"]
        assert "write path" not in result["interpretation"].split("not the")[0]

    def test_an_error_beats_every_delta(self):
        result = commit_census(
            repo_commits=847, walk_claimed=800, graph_commit_entities=700,
            distinct_commit_idents=847, final_status="complete",
            census_error="boom",
        )
        assert result["ok"] is False
        assert "boom" in result["interpretation"]

    def test_the_deltas_are_still_all_reported_under_any_diagnosis(self):
        result = _clean(distinct_commit_idents=846, graph_commit_entities=845)
        assert result["ident_collisions"] == 1
        assert result["walk_vs_graph"] == 2
        assert result["repo_vs_graph"] == 2


class TestOrphanedCommits:
    """#222 phase 5 item B. A force-push leaves :type/commit entities for
    commits no longer in history, with every :introduced-by / :modified-in /
    :parent / :tagged-commit reference to them still live."""

    def test_clean_graph_reports_zero_with_a_denominator(self):
        r = orphaned_commits({"a", "b"}, {"a", "b"}, "master", "master")
        assert r["entities"] == 0
        assert r["commit_entities_scanned"] == 2
        assert r["proved_nothing"] is False

    def test_rewritten_commit_is_reported(self):
        r = orphaned_commits({"a", "dead"}, {"a", "new"}, "master", "master")
        assert r["entities"] == 1
        assert r["sample"] == ["dead"]

    def test_branch_mismatch_proves_nothing_but_still_ships_the_real_count(self):
        """The decisive false-positive guard, and the contract that it does
        NOT hide the number. A graph ingested against `develop` and audited
        against `master` legitimately holds commits absent from master's
        linearization -- condemning it would delete a real branch's history,
        so `proved_nothing` is True and clause 9 does not gate it. But the
        count is still COMPUTED and shipped: a placeholder 0 here beside a
        denominator of 2 would read as "verified clean", which is exactly the
        misreading this arc refuses (#316's idiom: report, never gate)."""
        r = orphaned_commits({"a", "b"}, {"a"}, "develop", "master")
        assert r["proved_nothing"] is True
        assert r["entities"] == 1, (
            "an uninterpretable run must still ship the real difference, not "
            "a placeholder 0 that reads as clean"
        )
        assert r["sample"] == ["b"]
        assert r["commit_entities_scanned"] == 2

    def test_absent_branch_proves_nothing_but_still_ships_the_real_count(self):
        """A graph predating :ingestion/branch cannot be retro-audited -- but
        what it holds outside the ref is still reported, just not gated."""
        r = orphaned_commits({"a", "b"}, {"a"}, None, "master")
        assert r["proved_nothing"] is True
        assert r["entities"] == 1
        assert r["sample"] == ["b"]

    def test_empty_graph_proves_nothing(self):
        """A check that scanned no commit entities also reports 0."""
        r = orphaned_commits(set(), {"a"}, "master", "master")
        assert r["proved_nothing"] is True
        assert r["commit_entities_scanned"] == 0

    def test_sample_is_capped_and_sorted(self):
        dead = {f"d{i:03d}" for i in range(50)}
        r = orphaned_commits(dead, set(), "master", "master", sample_cap=3)
        assert r["entities"] == 50
        assert r["sample"] == sorted(dead)[:3]


class TestWalkClaimedFromProgress:
    """#222 phase 4: `processed` is gone; walk_claimed is prior_ingested +
    this run's retired count -- the SAME value `processed` held, so every
    commit_census gate keeps its meaning."""

    def test_prior_plus_retired(self):
        from evals.at_scale.commit_census import walk_claimed_from_progress
        from ingest_progress import RunProgress
        run = RunProgress([f"{i:040x}" for i in range(20)], 9, -1, None, None)
        run.stage_a_started()
        for pos in range(9):
            run.retired("rev", "written", pos)
        assert walk_claimed_from_progress({"prior_ingested": 19, "_run": run}) == 28

    def test_a_run_that_failed_before_construction_claims_prior_only(self):
        from evals.at_scale.commit_census import walk_claimed_from_progress
        assert walk_claimed_from_progress({"prior_ingested": 0, "_run": None}) == 0
        assert walk_claimed_from_progress({"status": "error"}) == 0
