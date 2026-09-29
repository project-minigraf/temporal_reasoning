"""Pure parts of evals/at_scale/probe_sweep_retract_attribution.py (#369)."""
import pytest

from evals.at_scale.probe_sweep_retract_attribution import (
    call_site,
    same_unit,
    site_function,
    split_command,
    summarize,
)


class TestCallSite:
    def test_skips_plumbing_and_names_two_frames(self):
        frames = [("timed_execute", 1), ("_db_execute", 2), ("_retract", 3),
                  ("_ingest_close", 6392), ("_forward_apply", 13700), ("run", 5)]
        assert call_site(frames) == "_forward_apply>_ingest_close:6392"

    def test_apply_wrappers_are_plumbing(self):
        # The probe wraps the apply functions in `spy`; a direct call from an
        # apply function must name its executor frame, not the wrapper.
        frames = [("_retract", 1), ("_correction_sweep_apply", 14278),
                  ("spy", 9), ("run", 58)]
        assert call_site(frames) == "run>_correction_sweep_apply:14278"

    def test_single_frame(self):
        assert call_site([("_retract", 1), ("f", 7)]) == "f:7"

    def test_no_frames(self):
        assert call_site([("_retract", 1)]) == "?"


def test_site_function_drops_the_inner_line_only():
    assert (site_function("_correction_sweep_apply>_re_date_structural_facts:8883")
            == "_correction_sweep_apply>_re_date_structural_facts")
    assert site_function("?") == "?"


class TestSameUnit:
    def test_same(self):
        assert same_unit("sweeping@abc", "sweeping@abc")

    def test_different_phase_same_commit_is_not_same(self):
        # Stage A and Stage B both apply commit abc; they are different units.
        assert not same_unit("converging@abc", "sweeping@abc")

    def test_untagged_is_never_a_unit(self):
        assert not same_unit("sweeping@-", "sweeping@-")

    def test_none(self):
        assert not same_unit(None, "sweeping@abc")


class TestSplitCommand:
    @pytest.mark.parametrize("cmd,op,body", [
        ("(retract [[:a/b :c 1]])", "retract", "[[:a/b :c 1]]"),
        ('(transact {:valid-from "2020"} [[:a/b :c 1]])', "transact", "[[:a/b :c 1]]"),
        ('(transact {:valid-from "2020" :valid-to "2021"} [[:a/b :c 1]])',
         "transact-bounded", "[[:a/b :c 1]]"),
        ("(transact [[:a/b :c 1]])", "transact", "[[:a/b :c 1]]"),
        ("(query [:find ?x :where [?x :a 1]])", "other", ""),
    ])
    def test_split(self, cmd, op, body):
        assert split_command(cmd) == (op, body)


def test_summarize_sorts_by_exec_and_derives_rates():
    rows = {
        "small": {"n": 2, "facts": 4, "exec_s": 0.002, "samples": [0.001, 0.001],
                  "noop": 1},
        "big": {"n": 4, "facts": 4, "exec_s": 0.010,
                "samples": [0.001, 0.001, 0.001, 0.007], "noop": 0},
    }
    out = summarize(rows)
    assert [r["key"] for r in out] == ["big", "small"]
    big = out[0]
    assert big["ms_per_call"] == pytest.approx(2.5)
    assert big["ms_per_fact"] == pytest.approx(2.5)
    assert big["p50_ms"] == pytest.approx(1.0)
    assert big["p99_ms"] == pytest.approx(7.0)
    assert out[1]["ms_per_fact"] == pytest.approx(0.5)
    assert out[1]["noop"] == 1
    assert "samples" not in big
