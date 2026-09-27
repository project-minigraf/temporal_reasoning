"""Pure parts of evals/at_scale/probe_minigraf_upgrade_cost.py (#239)."""
import copy

import pytest

from evals.at_scale.probe_minigraf_upgrade_cost import (
    PARITY_FIELDS,
    classify,
    is_point_query,
    verdict,
)


class TestClassify:
    @pytest.mark.parametrize("datalog,label", [
        # The exact shapes mcp_server issues (#380's bound-entity point query).
        ("(query [:find ?c :where [:function/a-py-f :introduced-by ?c]])",
         "q1:entity::introduced-by"),
        ("(query [:find ?i :where [:module/a-py :ident ?i]])",
         "q1:entity::ident"),
        ("(query [:find ?e :where [:lineage/function-a-py-f :entity ?e]])",
         "q1:lineage::entity"),
        ("(query [:find ?h :where [:ingestion/watermark :hash ?h]])",
         "q1:control::hash"),
        ("(query [:find ?lo ?hi :where [:ingestion/frontier-high :lo-hash ?lo] "
         "[:ingestion/frontier-high :hi-hash ?hi]])",
         "qN:control::lo-hash"),
        ('(query [:find ?t :where [#uuid "0000-11" :entity-type ?t]])',
         "q1:uuid::entity-type"),
        ("(query [:find (count-distinct ?e) :any-valid-time :where "
         "[?e :entity-type :type/commit]])",
         "q1:scan::entity-type:avt"),
        ("(transact [[:a/b :c 1]])", "transact"),
        ("  (retract [[:a/b :c 1]])", "retract"),
        ("(rule [(r ?x) [?x :a ?y]])", "rule"),
        ("(explain [:find ?x])", "other"),
        ("(query [:find ?x])", "query:unparsed"),
    ])
    def test_labels(self, datalog, label):
        assert classify(datalog) == label

    def test_point_query_category(self):
        assert is_point_query("q1:entity::introduced-by")
        assert is_point_query("q1:lineage::entity")
        assert not is_point_query("q1:control::hash")
        assert not is_point_query("qN:entity::lo-hash")
        assert not is_point_query("q1:scan::entity-type")
        assert not is_point_query("transact")


def _result(wall, point_s, version="2.0.0", **parity_overrides):
    parity = {f: 1 for f in PARITY_FIELDS}
    parity.update(final_status="complete", divergence=0, audit_error=None,
                  census_ok=True)
    parity.update(parity_overrides)
    return {
        "minigraf_version": version,
        "wall_s": wall,
        "leaf": {
            "converging|q1:entity::introduced-by": {"n": 1, "exec_s": point_s, "wait_s": 0.0},
            "converging|transact": {"n": 1, "exec_s": 10.0, "wait_s": 0.0},
        },
        "named": {"sweeping|_retract": {"n": 1, "s": 2.0}},
        "drops": {"n": 1, "total_s": 3.0, "thirds_mean_s": None},
        "parity": parity,
    }


def _runs(a_walls, b_walls, a_point=30.0, b_point=3.0):
    runs = []
    for wa, wb in zip(a_walls, b_walls):
        runs.append({"arm": "A", "result": _result(wa, a_point)})
        runs.append({"arm": "B", "result": _result(wb, b_point, version="2.0.2")})
    return runs


class TestVerdict:
    def test_clean_faster_batch(self):
        v = verdict(_runs([100.0, 110.0], [80.0, 84.0]))
        assert v["parity_ok"] and v["parity_problems"] == []
        assert v["no_regression"] is True
        assert v["versions"] == {"A": ["2.0.0"], "B": ["2.0.2"]}
        assert v["wall_s"]["a_median"] == 105.0
        assert v["wall_s"]["b_over_a"] == pytest.approx(82.0 / 105.0)
        assert v["wall_s"]["a_spread"] == pytest.approx(10.0 / 105.0)
        # Only the point-query leaf bucket counts toward the point category.
        assert v["point_query_exec_s"]["a_median"] == 30.0
        assert v["db_exec_total_s"]["a_median"] == 40.0
        assert v["point_query_share_of_wall"]["a"] == pytest.approx(
            [30.0 / 100.0, 30.0 / 110.0])

    def test_regression_threshold_is_exclusive_above_five_percent(self):
        assert verdict(_runs([100.0], [105.0]))["no_regression"] is True
        assert verdict(_runs([100.0], [105.1]))["no_regression"] is False

    @pytest.mark.parametrize("field", PARITY_FIELDS)
    def test_any_differing_parity_field_fails_parity(self, field):
        runs = _runs([100.0, 100.0], [90.0, 90.0])
        runs[3]["result"]["parity"][field] = "different"
        v = verdict(runs)
        assert v["parity_ok"] is False
        assert v["parity_problems"] == ["2 distinct parity fingerprints"]

    @pytest.mark.parametrize("field,value,problem", [
        ("divergence", 3, "B[0] divergence 3"),
        ("audit_error", "boom", "B[0] audit_error boom"),
        ("census_ok", False, "B[0] census_ok False"),
        ("code_entities_scanned", 0, "B[0] no code entities (0): measured nothing"),
        ("code_entities_scanned", None,
         "B[0] no code entities (None): measured nothing"),
    ])
    def test_unclean_audit_or_census_fails_parity_even_when_all_agree(
        self, field, value, problem
    ):
        # Identical in every run, so the fingerprint check alone would pass:
        # the per-run cleanliness checks must catch it on their own.
        runs = _runs([100.0], [90.0])
        for r in runs:
            r["result"]["parity"][field] = value
        v = verdict(runs)
        assert v["parity_ok"] is False
        assert problem in v["parity_problems"]

    def test_missing_arm_fails_parity(self):
        runs = [r for r in _runs([100.0], [90.0]) if r["arm"] == "A"]
        v = verdict(runs)
        assert "an arm has no runs" in v["parity_problems"]
        assert v["no_regression"] is False

    def test_bucket_absent_from_one_arm_reads_as_zero(self):
        runs = _runs([100.0], [90.0])
        b = copy.deepcopy(runs[1])
        del b["result"]["leaf"]["converging|transact"]
        runs[1] = b
        v = verdict(runs)
        assert v["leaf_exec_s"]["converging|transact"]["b"] == [0.0]
