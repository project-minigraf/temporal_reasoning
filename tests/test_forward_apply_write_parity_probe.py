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


# --------------------------------------------------------------------------
# Fix round 1. Each test below is ablation-proven: the ablation that reddens
# it is named in its docstring, and each was run.
# --------------------------------------------------------------------------


def _write_with_meta(tmp_path, name, rows, *, corpus="c1", fmt=None, untagged=0,
                     ratio="1:1", trailer=True):
    lines = [json.dumps({"header": 1,
                         "probe_format": fmt if fmt is not None else probe.PROBE_FORMAT,
                         "corpus_id": corpus, "corpus_label": corpus,
                         "ratio": ratio, "branch": "master"})]
    lines += [json.dumps(r) for r in rows]
    if trailer:
        lines.append(json.dumps({"trailer": 1, "commands": len(rows),
                                 "untagged_commands": untagged}))
    p = tmp_path / name
    p.write_text("\n".join(lines))
    return p


def test_two_apply_windows_of_one_hash_are_distinct_sequences(tmp_path):
    """The blind spot the occurrence index closes, and the one this refactor
    is most likely to produce: a write moved from the TAIL of Stage A's apply
    to the HEAD of Stage B's `_forward_apply(lifecycle_only=True)` apply of
    the SAME hash. Both recordings hold [X, Y, Z] for that hash, so a
    hash-only grouping key concatenates them identically and reports clean.

    Ablation: drop `occ` from `_read`'s key (key = row["commit"]) and this
    test goes green while the defect it names is live."""
    a = _write_with_meta(tmp_path, "a.jsonl", [
        {"commit": "abc", "cmd": "X"},
        {"commit": "abc", "cmd": "Y"},            # tail of window 0
        {"commit": "abc", "cmd": "Z", "occ": 1},
    ])
    b = _write_with_meta(tmp_path, "b.jsonl", [
        {"commit": "abc", "cmd": "X"},
        {"commit": "abc", "cmd": "Y", "occ": 1},  # moved to the head of window 1
        {"commit": "abc", "cmd": "Z", "occ": 1},
    ])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert [d["commit"] for d in result["differing_commits"]] == ["abc", "abc#1"]


def test_a_second_apply_window_renders_with_its_occurrence_suffix(tmp_path):
    """The key must stay unambiguous: occurrence 0 renders as the bare hash
    (so format-1 rows and the comparator's own fixtures still read), and a
    later window renders `<hash>#<n>`, which no commit hash can collide with."""
    a = _write_with_meta(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"}])
    b = _write_with_meta(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "X"},
                                               {"commit": "abc", "cmd": "X", "occ": 1}])
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["commits_only_in_b"] == ["abc#1"]


def test_two_different_corpora_are_refused_not_diffed(tmp_path):
    """Two arms that walked different histories must not be reported as
    catastrophic divergence in the code under test. REFUSED, and
    `proved_nothing` with it -- a refused comparison proved nothing about
    parity.

    Ablation: drop the `corpus_mismatch` guard and this reports two
    one-sided commits and `proved_nothing: False`."""
    a = _write_with_meta(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"}], corpus="c1")
    b = _write_with_meta(tmp_path, "b.jsonl", [{"commit": "def", "cmd": "X"}], corpus="c2")
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["corpus_mismatch"] is True
    assert result["proved_nothing"] is True
    # Refused means NOT diffed: no per-window findings are produced at all.
    assert result["commits_only_in_a"] == []
    assert result["commits_only_in_b"] == []
    assert result["differing_commits"] == []


def test_the_same_corpus_at_two_ratios_is_still_compared(tmp_path):
    """A ratio difference is reported and deliberately does NOT refuse: the
    probe's own negative control compares a 1:1 recording against a
    forward-only one to prove the comparator can see a real difference."""
    a = _write_with_meta(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"}], ratio="1:1")
    b = _write_with_meta(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "Y"}],
                         ratio="forward-only")
    result = probe.compare(a, b)
    assert result["ratio_mismatch"] is True
    assert result["corpus_mismatch"] is False
    assert [d["commit"] for d in result["differing_commits"]] == ["abc"]


def test_two_recording_formats_are_refused(tmp_path):
    a = _write_with_meta(tmp_path, "a.jsonl", [{"commit": "abc", "cmd": "X"}], fmt=1)
    b = _write_with_meta(tmp_path, "b.jsonl", [{"commit": "abc", "cmd": "X"}], fmt=2)
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["format_mismatch"] is True
    assert result["proved_nothing"] is True


def test_a_changed_untagged_count_is_reported_but_does_not_gate(tmp_path):
    """Only commands issued inside an apply frame are compared. A change in
    what everything ELSE issued is invisible to the per-window diff, so the
    dropped count rides in the trailer and is reported as `untagged_mismatch`
    -- but it must not fail the comparison.

    Measured (task-1 report): the SAME 303-commit corpus, SAME code, on two
    separate invocation batches produced untagged_commands 475641 vs 475623
    with total `commands` identical at 566360 -- so this count drifts on
    unmodified code between batches, and gating on it false-fails every task
    whose baseline predates its comparison arm.

    Ablation for the 'reported' half: delete the untagged_mismatch
    computation entirely and `untagged_mismatch` reads False here even though
    the two trailers plainly differ -- a test asserting only `ok is True`
    would not catch that, so both halves are asserted below."""
    rows = [{"commit": "abc", "cmd": "X"}]
    a = _write_with_meta(tmp_path, "a.jsonl", rows, untagged=100)
    b = _write_with_meta(tmp_path, "b.jsonl", rows, untagged=101)
    result = probe.compare(a, b)
    # The informational half: still computed and surfaced.
    assert result["untagged_mismatch"] is True
    assert result["untagged_a"] == 100
    assert result["untagged_b"] == 101
    # The gating half: an untagged-only difference must not fail the compare.
    assert result["ok"] is True
    assert result["differing_commits"] == []
    assert result["proved_nothing"] is False


def test_a_truncated_recording_is_caught_by_its_missing_trailer(tmp_path):
    """The trailer is the last thing record_run writes, so its absence on one
    side means that recording was cut short -- an integrity failure, not a
    finding about the code."""
    rows = [{"commit": "abc", "cmd": "X"}]
    a = _write_with_meta(tmp_path, "a.jsonl", rows)
    b = _write_with_meta(tmp_path, "b.jsonl", rows, trailer=False)
    result = probe.compare(a, b)
    assert result["ok"] is False
    assert result["trailer_mismatch"] is True


def test_a_degraded_arm_is_not_a_usable_baseline():
    """`commands > 0` alone is not enough: a run can report `error` after
    writing plenty of commands, and one can report `complete` while having
    lost commits (#317's census exists for exactly that).

    Ablation: check only `commands` and the last two cases return []."""
    healthy = {"commands": 10, "status": "complete", "graph_commits": 5}
    assert probe.arm_problems(healthy, 5) == []
    assert probe.arm_problems({**healthy, "commands": 0}, 5)
    assert probe.arm_problems({**healthy, "status": "error"}, 5)
    assert probe.arm_problems({**healthy, "graph_commits": 4}, 5)


def test_a_reused_truncation_workdir_is_refused_once_the_tip_moves(tmp_path):
    """(source, branch, keep) stays identical while `master` advances, and
    the same `keep` then cuts a DIFFERENT set of commits. Without the tip SHA
    in the marker key the stale clone is handed back and the two arms of one
    comparison walk two histories.

    Ablation: drop `tip` from the key and the second call returns the stale
    dest instead of raising."""
    src = tmp_path / "src"
    src.mkdir()
    probe._git(src, "init", "-q", "-b", "master")
    probe._git(src, "config", "user.email", "t@t.com")
    probe._git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("1")
    probe._git(src, "add", ".")
    probe._git(src, "commit", "-q", "-m", "c0")

    dest = tmp_path / "clone"
    assert probe.truncate_repo(src, dest, 10, "master") == dest
    # Same call again is a legitimate reuse while the tip has not moved.
    assert probe.truncate_repo(src, dest, 10, "master") == dest

    (src / "a.txt").write_text("2")
    probe._git(src, "add", ".")
    probe._git(src, "commit", "-q", "-m", "c1")
    with pytest.raises(RuntimeError, match="already exists"):
        probe.truncate_repo(src, dest, 10, "master")


def test_a_real_run_records_fact_index_traffic(tmp_path, monkeypatch):
    """`_index_write` and `_commit_index_writer_safe` are recorded, not just
    `_db_execute`. `_commit_index_writer_safe` is literally the last
    statement of `_forward_apply`'s write tail, and the fact index is the
    graph's only independent witness (#302) -- a task that moved or dropped
    that commit, or changed which triples reach the index, read `ok: true`
    before this.

    A real ingestion, per docs/testing-conventions.md. Ablation: remove the
    two wrappers and no `(index-` row is written at all."""
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    repo = tmp_path / "repo"
    repo.mkdir()
    probe._git(repo, "init", "-q", "-b", "master")
    probe._git(repo, "config", "user.email", "t@t.com")
    probe._git(repo, "config", "user.name", "T")
    for i in range(3):
        (repo / "m.py").write_text(f"def f{i}():\n    return {i}\n")
        probe._git(repo, "add", ".")
        probe._git(repo, "commit", "-q", "-m", f"c{i}")

    out = tmp_path / "rec.jsonl"
    result = probe.record_run(repo, tmp_path / "g.graph", "1:1", out, branch="master")
    assert result["status"] == "complete"
    assert result["commands"] > 0

    cmds = [json.loads(line)["cmd"] for line in out.read_text().splitlines()
            if line and "cmd" in json.loads(line)]
    assert any(c.startswith("(index-insert ") for c in cmds), "no index inserts recorded"
    assert any(c.startswith("(index-commit ") for c in cmds), "no index commit recorded"
    # The header and trailer are real rows, not decoration.
    rows = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert rows[0]["probe_format"] == probe.PROBE_FORMAT
    assert rows[0]["corpus_id"]
    assert rows[-1]["trailer"] == 1
    assert rows[-1]["untagged_commands"] == result["untagged_commands"]


def test_window_and_commit_counts_are_reported_separately(tmp_path):
    """One hash gets a second window when Stage B sweeps a commit Stage A
    already applied, so on the real 303-commit corpus the window count is
    454. Both numbers ship and each says what it counts.

    Ablation: report `len(grouped)` as `commits_a` and this reads 2 commits
    for a one-commit recording."""
    rows = [{"commit": "abc", "cmd": "X"}, {"commit": "abc", "cmd": "Y", "occ": 1}]
    a = _write_with_meta(tmp_path, "a.jsonl", rows)
    b = _write_with_meta(tmp_path, "b.jsonl", rows)
    result = probe.compare(a, b)
    assert result["ok"] is True
    assert result["windows_a"] == 2
    assert result["commits_a"] == 1
