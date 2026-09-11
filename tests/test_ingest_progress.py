"""#222 phase 4: the pure per-run progress model.

No DB, no git -- RunProgress only mirrors events _run_ingestion feeds it, so
every property here is checked against a fake clock and a synthetic
linearization. The end-to-end half lives in tests/test_mcp_server.py
(TestIngestStatusPhase4E2E).
"""

import random

import pytest

from ingest_progress import RunProgress, SWEEP_STATE_FOR_REASON


def _lin(n):
    return [f"{i:040x}" for i in range(n)]


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _model(n=10, to_retire=10, lineage_pos=-1, sweep_lo=None, sweep_through=None, clock=None):
    clock = clock or _Clock()
    return RunProgress(
        _lin(n), to_retire, lineage_pos, sweep_lo, sweep_through,
        mono=clock, wall=lambda: "2026-09-11T00:00:00.000Z",
    ), clock


class TestConstruction:
    def test_rejects_to_retire_outside_the_linearization(self):
        with pytest.raises(ValueError):
            RunProgress(_lin(3), 4, -1, None, None)
        with pytest.raises(ValueError):
            RunProgress(_lin(3), -1, -1, None, None)

    def test_a_fresh_model_snapshots_without_raising(self):
        m, _ = _model()
        s = m.snapshot()
        assert s["this_run"]["retired"] == 0
        assert s["this_run"]["to_retire"] == 10
        assert s["streams"]["forward"]["state"] == "not-started"
        assert s["streams"]["sweep"]["state"] == "waiting"
        assert s["visibility"] == {"verified": 0, "total": 10, "complete": False}

    def test_an_empty_gap_at_load_is_not_needed_and_visibility_complete(self):
        m, _ = _model(to_retire=0, lineage_pos=9)
        s = m.snapshot()
        assert s["streams"]["forward"]["state"] == "not-needed"
        assert s["streams"]["reverse"]["state"] == "not-needed"
        assert s["visibility"] == {"verified": 10, "total": 10, "complete": True}


class TestRetirementArithmetic:
    def test_retired_is_the_sum_of_outcomes(self):
        """retired <= to_retire is a property of the allocator (each gap position is claimed once), not of this model; it is guarded end-to-end by TestIngestStatusPhase4E2E's re-walk test in tests/test_mcp_server.py."""
        rng = random.Random(222)
        for _ in range(200):
            to_retire = rng.randint(0, 12)
            m, _ = _model(n=12, to_retire=to_retire)
            m.stage_a_started()
            for pos in range(to_retire):
                m.retired(rng.choice(["fwd", "rev"]), rng.choice(["written", "skipped", "failed"]), pos)
                s = m.snapshot()["this_run"]
                assert s["retired"] == s["written"] + s["skipped"] + s["failed"]

    def test_verified_is_total_minus_failed_at_a_completed_end(self):
        m, _ = _model(n=20, to_retire=20)
        m.stage_a_started()
        for pos in range(20):
            m.retired("rev", "failed" if pos == 7 else "written", pos)
        m.stage_a_finished(True)
        v = m.snapshot()["visibility"]
        assert v["verified"] == 19
        assert v["complete"] is False

    def test_visibility_completes_only_with_every_position_retired_and_none_failed(self):
        m, _ = _model(n=4, to_retire=2, lineage_pos=1)
        m.stage_a_started()
        m.retired("rev", "written", 3)
        assert m.snapshot()["visibility"]["complete"] is False
        m.retired("rev", "skipped", 2)
        assert m.snapshot()["visibility"] == {"verified": 4, "total": 4, "complete": True}

    def test_unknown_stream_or_outcome_is_rejected(self):
        m, _ = _model()
        with pytest.raises(ValueError):
            m.retired("sweep", "written", 0)
        with pytest.raises(ValueError):
            m.retired("fwd", "done", 0)


class TestStreamStates:
    def test_stage_a_lifecycle(self):
        m, _ = _model()
        m.stage_a_started()
        assert m.snapshot()["streams"]["forward"]["state"] == "running"
        m.retired("rev", "written", 9)
        m.stage_a_finished(True)
        st = m.snapshot()["streams"]
        assert st["reverse"]["state"] == "done"
        assert st["forward"]["state"] == "not-needed"  # zero claims

    def test_a_stopped_stage_a_marks_streams_stopped_and_sweep_not_run(self):
        m, _ = _model()
        m.stage_a_started()
        m.retired("fwd", "written", 0)
        m.stage_a_finished(False)
        st = m.snapshot()["streams"]
        assert st["forward"]["state"] == "stopped"
        assert st["reverse"]["state"] == "stopped"
        assert st["sweep"]["state"] == "not-run"

    def test_an_error_mid_stage_a_marks_running_streams_error(self):
        m, _ = _model()
        m.stage_a_started()
        m.ended("error")
        st = m.snapshot()["streams"]
        assert st["forward"]["state"] == "error"
        assert st["sweep"]["state"] == "not-run"

    def test_complete_does_not_rewrite_finished_states(self):
        m, _ = _model(to_retire=1)
        m.stage_a_started()
        m.retired("rev", "written", 9)
        m.stage_a_finished(True)
        m.sweep_planned("reached-ceiling", 9, 10, 9)
        m.ended("complete")
        st = m.snapshot()["streams"]
        assert st["reverse"]["state"] == "done"
        assert st["sweep"]["state"] == "done"


class TestSweep:
    def test_reason_table_covers_every_decline_reason(self):
        assert set(SWEEP_STATE_FOR_REASON) == {
            "no-frontier-high", "reached-ceiling", "gap-open",
            "fragmented", "stale-bound", "metadata-mismatch",
        }

    @pytest.mark.parametrize("reason,state,blocked", [
        ("no-frontier-high", "not-needed", None),
        ("reached-ceiling", "done", None),
        ("gap-open", "blocked", "gap-open"),
        ("fragmented", "blocked", "fragmented"),
        ("stale-bound", "blocked", "stale-bound"),
        ("metadata-mismatch", "blocked", "metadata-mismatch"),
    ])
    def test_planned_decline_maps_to_state(self, reason, state, blocked):
        m, _ = _model()
        m.sweep_planned(reason, None, None, None)
        sw = m.snapshot()["streams"]["sweep"]
        assert (sw["state"], sw["blocked_reason"]) == (state, blocked)

    def test_selected_plan_counts_swept_up_to_to_sweep(self):
        m, _ = _model(n=10, to_retire=0, lineage_pos=4)
        m.sweep_planned("selected", 5, 5, 9)
        sw = m.snapshot()["streams"]["sweep"]
        assert (sw["state"], sw["swept"], sw["to_sweep"]) == ("running", 0, 5)
        for pos in range(5, 10):
            m.swept(pos)
        m.sweep_ended("reached-ceiling")
        sw = m.snapshot()["streams"]["sweep"]
        assert (sw["state"], sw["swept"], sw["to_sweep"]) == ("done", 5, 5)

    @pytest.mark.parametrize("outcome", ["stopped", "aborted"])
    def test_sweep_can_end_stopped_or_aborted(self, outcome):
        m, _ = _model()
        m.sweep_planned("selected", 5, 5, 9)
        m.sweep_ended(outcome)
        assert m.snapshot()["streams"]["sweep"]["state"] == outcome

    def test_unknown_sweep_outcome_is_rejected(self):
        m, _ = _model()
        with pytest.raises(ValueError):
            m.sweep_ended("finished")


class TestLineage:
    def test_unset_watermark_is_zero_not_null(self):
        m, _ = _model(lineage_pos=-1)
        lin = m.snapshot()["lineage"]
        assert lin["confirmed"] == 0
        assert lin["confirmed_through"] is None

    def test_unresolvable_watermark_is_null_never_a_guess(self):
        m, _ = _model(lineage_pos=None)
        lin = m.snapshot()["lineage"]
        assert lin["confirmed"] is None
        assert lin["complete"] is False

    def test_forward_write_advances_the_watermark_but_a_failure_does_not(self):
        m, _ = _model()
        m.stage_a_started()
        m.retired("fwd", "written", 0)
        m.retired("fwd", "written", 1)
        m.retired("fwd", "failed", 2)
        m.retired("rev", "written", 9)
        lin = m.snapshot()["lineage"]
        assert lin["confirmed"] == 2
        assert lin["confirmed_through"] == _lin(10)[1]

    def test_swept_region_adds_to_the_contiguous_prefix(self):
        m, _ = _model(n=10, to_retire=0, lineage_pos=4)
        m.sweep_planned("selected", 5, 5, 9)
        m.swept(5)
        m.swept(6)
        assert m.snapshot()["lineage"]["confirmed"] == 7

    def test_union_does_not_double_count_a_region_the_prefix_covers(self):
        """Post-fold, +1 commit: the fold moved lineage_pos to the old tip (9)
        while frontier-high and the sweep watermark still describe [5, 9]. A
        sum would say 10 + 5 = 15 of 11."""
        m, _ = _model(n=11, to_retire=1, lineage_pos=9, sweep_lo=5, sweep_through=9)
        assert m.snapshot()["lineage"]["confirmed"] == 10
        m.sweep_planned("selected", 5, 10, 10)
        m.swept(10)
        assert m.snapshot()["lineage"]["confirmed"] == 11

    def test_a_plan_that_resumes_mid_region_counts_the_earlier_sweep(self):
        m, _ = _model(n=10, to_retire=0, lineage_pos=4)
        m.sweep_planned("selected", 5, 8, 9)  # an earlier run swept 5..7
        assert m.snapshot()["lineage"]["confirmed"] == 8

    def test_fold_completes_lineage(self):
        m, _ = _model(n=10, to_retire=0, lineage_pos=4)
        m.folded()
        lin = m.snapshot()["lineage"]
        assert lin == {"confirmed": 10, "total": 10, "complete": True,
                       "confirmed_through": _lin(10)[-1]}


class TestRatesAndLiveness:
    def test_rate_is_retired_per_minute_since_stage_a_started(self):
        m, clock = _model()
        m.stage_a_started()
        clock.t += 30.0
        m.retired("fwd", "written", 0)
        m.retired("fwd", "written", 1)
        clock.t += 30.0
        assert m.snapshot()["streams"]["forward"]["rate_per_min"] == 2.0
        assert m.snapshot()["streams"]["reverse"]["rate_per_min"] is None

    def test_seconds_since_progress_grows_while_nothing_retires(self):
        m, clock = _model()
        m.stage_a_started()
        m.retired("rev", "written", 9)
        clock.t += 47 * 60
        assert m.snapshot()["this_run"]["seconds_since_progress"] == pytest.approx(47 * 60)

    def test_seconds_since_progress_counts_from_construction_before_any_event(self):
        m, clock = _model()
        clock.t += 5.0
        assert m.snapshot()["this_run"]["seconds_since_progress"] == pytest.approx(5.0)
