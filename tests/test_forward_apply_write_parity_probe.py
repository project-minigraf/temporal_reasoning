"""Comparator tests for the #346 write-sequence parity probe.

These cover `compare()` only -- the pure half. `record_run()` drives a real
`_run_ingestion` and is exercised by the probe's own CLI (see the task-1
report's four baseline arms), not from the suite: one arm costs minutes.
"""
import json
import pytest
from evals.at_scale import probe_forward_apply_write_parity as probe


def _write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def test_identical_recordings_compare_equal(tmp_path):
    rows = [{"commit": "abc", "cmd": "(transact [[:a :b 1]])"},
            {"commit": "abc", "cmd": "(query [:find ?e])"}]
    a = _write(tmp_path, "a.jsonl", rows)
    b = _write(tmp_path, "b.jsonl", rows)
    assert probe.compare(a, b)["ok"] is True


def test_a_reordered_command_within_one_commit_is_a_difference(tmp_path):
    a = _write(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"},
                                     {"commit": "abc", "cmd": "Y"}])
    b = _write(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "Y"},
                                     {"commit": "abc", "cmd": "X"}])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert [d["commit"] for d in result["differing_commits"]] == ["abc"]


def test_a_missing_commit_is_a_difference(tmp_path):
    a = _write(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"},
                                     {"commit": "def", "cmd": "Y"}])
    b = _write(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "X"}])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["commits_only_in_a"] == ["def"]


def test_empty_recordings_are_not_reported_as_clean(tmp_path):
    """A comparator that reads two empty files as 'ok' fails OPEN: a probe
    that recorded nothing at all would certify the refactor. This is the
    positive control on the gate itself."""
    a = _write(tmp_path, "a.jsonl", [])
    b = _write(tmp_path, "b.jsonl", [])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["proved_nothing"] is True


def test_one_empty_recording_proves_nothing_either(tmp_path):
    """Controller resolution 3: `proved_nothing` is True when EITHER side is
    empty, not only when both are. An arm that crashed before its first write
    would otherwise be reported as a real, diagnosable difference -- which
    reads as a finding about the refactor rather than about the run."""
    a = _write(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"}])
    b = _write(tmp_path, "b.jsonl", [])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["proved_nothing"] is True


def test_a_differing_command_reports_its_first_divergence_index(tmp_path):
    a = _write(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"},
                                     {"commit": "abc", "cmd": "Y"},
                                     {"commit": "abc", "cmd": "Z"}])
    b = _write(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "X"},
                                     {"commit": "abc", "cmd": "Q"},
                                     {"commit": "abc", "cmd": "Z"}])
    result = probe.compare(a, b)
    assert result["ok"] is False
    (d,) = result["differing_commits"]
    assert d["first_divergence_index"] == 1
    assert d["a"] == "Y"
    assert d["b"] == "Q"


def test_record_run_refuses_an_unpinned_hash_seed(monkeypatch):
    """PYTHONHASHSEED=0 is REFUSED-ON-UNSET, not merely documented. Several
    ingestion sites iterate sets of strings, so hash randomization reorders
    emitted triples between two runs of identical code -- an unpinned arm
    reports differences that are not real, and makes a real one look like
    ordinary noise."""
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    with pytest.raises(RuntimeError, match="PYTHONHASHSEED"):
        probe.record_run("/nonexistent", "/nonexistent.graph", "1:1", "/nonexistent.jsonl")
    monkeypatch.setenv("PYTHONHASHSEED", "12345")
    with pytest.raises(RuntimeError, match="PYTHONHASHSEED"):
        probe.record_run("/nonexistent", "/nonexistent.graph", "1:1", "/nonexistent.jsonl")
