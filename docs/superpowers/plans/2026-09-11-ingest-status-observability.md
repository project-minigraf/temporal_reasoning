# Ingestion Status and Progress Reporting (#222 phase 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `minigraf_ingest_status`'s seeded `processed` counter with a work-based per-run counter, per-stream state and rate, a Stage B sweep counter, and two termination signals (visibility, lineage). Also fix `last_commit` / `:total-ingested`, and add a benchmark `--progress-interval`.

**Architecture:** A new pure module `ingest_progress.py` holds `RunProgress`. It is a plain event-driven model with no DB, no git and injected clocks. `_run_ingestion` builds it once per run, right after `_frontier_load`, stores it at `_ingest_progress["_run"]`, and calls its event methods at the existing retirement and sweep sites. `handle_minigraf_ingest_status` merges `snapshot()` into its response. `processed` is kept alive in parallel until every consumer has moved (Tasks 3–5), then deleted (Task 6), so every task ends with a green suite.

**Tech Stack:** Python 3.10–3.14, minigraf 2.x (`MiniGrafDb`), pytest + pytest-asyncio (`asyncio_mode = "auto"`), real git repos in `tmp_path`.

**Spec:** `docs/superpowers/specs/2026-09-11-ingest-status-observability-design.md`

## Global Constraints

- Always run Python as `.venv/bin/python`. System python has minigraf 1.1.1 and fakes ~122 failures.
- `.claude/settings.local.json` exports `MINIGRAF_*` variables into this session, but `tests/conftest.py` scrubs them before each test, so no workaround is needed. A red test here is a real failure.
- Dependency bounds are unchanged: `minigraf>=2.0.0,<3.0.0`, `mcp<2.0.0`.
- No `GRAPH_FORMAT_VERSION` bump and no migration. This change adds no fact shape.
- Tests use a real backend only, never a `MagicMock` of `MiniGrafDb` (`docs/testing-conventions.md`).
- Every new guard must be ablation-proven. Revert the production line it guards, watch the test fail, restore it, and record the failure output in the commit message.
- Progress lines go to **stdout**, never stderr. stderr is teed and scanned by `evals/at_scale/stderr_capture.py`.
- The PR and every commit say "Part of #222" and carry **no closing keyword** (close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved followed by `#N`, even negated). #222 stays open for phase 5. Re-scan after every new commit:
  `git log master..HEAD --format=%B | grep -inE "\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^a-z]*#[0-9]+"` must print nothing.
- End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw
  ```
- Work on branch `222-phase4-status-observability` in place. No worktree.
- Never edit a file that a running background test or probe imports.

---

## File Structure

| File | Responsibility |
|---|---|
| `ingest_progress.py` (new) | `RunProgress`: pure per-run progress model and its `snapshot()` |
| `frontier_registry.py` | adds `FrontierAllocator.unclaimed_count()` |
| `pyproject.toml` | `ingest_progress` added to `py-modules` |
| `mcp_server.py` | `_build_run_progress`, `_correction_sweep_next`, wiring in `_run_ingestion`, handler rendering, `last_commit`/`:total-ingested`, `processed` removal, `_TOOLS` description |
| `evals/at_scale/commit_census.py` | `walk_claimed_from_progress()` shared helper, docstring |
| `evals/at_scale/run_ingestion_benchmark.py` | consumer re-point, `format_progress_line`, `--progress-interval` |
| `evals/at_scale/probe_resume_census.py` | consumer re-point, key renames |
| `evals/at_scale/profile_forward_reconcile_attribution.py`, `probe_dep_preload_exposure.py`, `stderr_capture.py` | consumer re-point / dict literals / docstring |
| `tests/test_ingest_progress.py` (new) | pure model tests |
| `tests/test_frontier_registry.py` | `unclaimed_count` test |
| `tests/test_mcp_server.py` | sweep-reason tests, phase-4 end-to-end tests, migration of `processed` assertions |
| `tests/test_at_scale_resume_census.py`, `tests/test_at_scale_commit_census.py`, `tests/test_at_scale_ingestion_benchmark.py` | consumer tests |
| `SKILL.md`, `README.md`, `tools/ingest_status.json`, `CLAUDE.md` | docs |

---

### Task 1: `RunProgress` pure model + `unclaimed_count()`

**Files:**
- Create: `ingest_progress.py`
- Create: `tests/test_ingest_progress.py`
- Modify: `frontier_registry.py` (add a method to `FrontierAllocator`, after `is_gap_empty`)
- Modify: `tests/test_frontier_registry.py` (append a test)
- Modify: `pyproject.toml:107`

**Interfaces:**
- Produces:
  - `frontier_registry.FrontierAllocator.unclaimed_count() -> int`
  - `ingest_progress.RunProgress(linearization: Sequence[str], to_retire: int, lineage_pos: Optional[int], sweep_lo: Optional[int], sweep_through: Optional[int], mono: Callable[[], float] = time.monotonic, wall: Callable[[], str] = utc_iso)`
    - `lineage_pos`: `-1` means unset and `None` means unresolvable.
    - `sweep_lo` / `sweep_through`: the already-swept part of frontier-high's region, or `None`.
  - Methods:
    - `stage_a_started()`
    - `retired(stream: str, outcome: str, pos: int)`, where `stream` ∈ `{"fwd","rev"}` and `outcome` ∈ `{"written","skipped","failed"}`
    - `stage_a_finished(completed: bool)`
    - `sweep_planned(reason: str, region_lo: Optional[int], start_pos: Optional[int], ceiling_pos: Optional[int])`
    - `swept(pos: int)`
    - `sweep_ended(outcome: str)`
    - `folded()`
    - `ended(status: str)`
    - `snapshot() -> Dict[str, Any]`
  - Properties: `retired_count`, `written`, `skipped`, `failed`, `to_retire`, `total`
  - Module constant: `SWEEP_STATE_FOR_REASON`
  - Reason strings: `"selected"`, `"no-frontier-high"`, `"reached-ceiling"`, `"gap-open"`, `"fragmented"`, `"stale-bound"`, `"metadata-mismatch"`

- [ ] **Step 1: Write the failing `unclaimed_count` test**

Append to `tests/test_frontier_registry.py`:

```python
class TestUnclaimedCount:
    """#222 phase 4: RunProgress's `to_retire` is the gap AT LOAD, so the
    allocator must say how many positions it will hand out -- counted over
    every hole, not gap_hi - gap_lo, which overcounts a fragmented gap."""

    def test_empty_allocator_counts_every_position(self):
        from frontier_registry import FrontierAllocator
        assert FrontierAllocator(7).unclaimed_count() == 7

    def test_counts_every_hole_of_a_fragmented_gap(self):
        from frontier_registry import FrontierAllocator, Interval, TAG_AUTHORITATIVE, TAG_PROVISIONAL
        a = FrontierAllocator(10, [
            Interval(0, 2, TAG_AUTHORITATIVE),
            Interval(5, 5, TAG_PROVISIONAL, anchor_pos=5, is_base=True),
            Interval(8, 8, TAG_PROVISIONAL, anchor_pos=8),
        ])
        # holes: 3-4, 6-7, 9 -> 5 positions; gap_hi - gap_lo + 1 would say 7
        assert a.unclaimed_count() == 5

    def test_zero_once_every_position_is_claimed(self):
        from frontier_registry import FrontierAllocator, Interval, TAG_AUTHORITATIVE
        assert FrontierAllocator(4, [Interval(0, 3, TAG_AUTHORITATIVE)]).unclaimed_count() == 0
```

- [ ] **Step 2: Write the failing model tests**

Create `tests/test_ingest_progress.py`:

```python
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
    def test_retired_is_the_sum_of_outcomes_and_never_exceeds_to_retire(self):
        rng = random.Random(222)
        for _ in range(200):
            to_retire = rng.randint(0, 12)
            m, _ = _model(n=12, to_retire=to_retire)
            m.stage_a_started()
            for pos in range(to_retire):
                m.retired(rng.choice(["fwd", "rev"]), rng.choice(["written", "skipped", "failed"]), pos)
                s = m.snapshot()["this_run"]
                assert s["retired"] == s["written"] + s["skipped"] + s["failed"]
                assert s["retired"] <= s["to_retire"]

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
```

- [ ] **Step 3: Run both test files and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ingest_progress.py tests/test_frontier_registry.py::TestUnclaimedCount -q`
Expected: collection error `ModuleNotFoundError: No module named 'ingest_progress'`, and `AttributeError: ... 'unclaimed_count'`.

- [ ] **Step 4: Add `unclaimed_count` to `FrontierAllocator`**

In `frontier_registry.py`, after `is_gap_empty`:

```python
    def unclaimed_count(self) -> int:
        """How many positions this allocator will still hand out -- summed
        over every hole of the complement, never `gap_hi - gap_lo + 1`,
        which counts the claimed intervals between holes too (#222 phase 4:
        RunProgress's `to_retire`)."""
        return sum(hi - lo + 1 for lo, hi in self._unclaimed())
```

- [ ] **Step 5: Create `ingest_progress.py`**

```python
"""Per-run ingestion progress model for #222 phase 4.

`_run_ingestion` used to report one seeded scalar, `processed`: the graph's
commit count at run start, then +1 for every position the walk retired. It
was shown as progress against `total`, which it exceeded on every re-walk
(28/20 measured), and a run that lost a commit read 20/20. This model
replaces it with numbers that each mean one thing:

  * this_run   -- work: positions in the gap at load, and how many of them
                  this run has retired (written / skipped / failed). Each
                  gap position is retired at most once per run, so
                  retired <= to_retire always.
  * visibility -- state: positions the frontier can prove complete. May dip
                  between runs after a failure; `this_run` is the monotonic
                  number within a run.
  * lineage    -- state: positions with authoritative lineage, the union of
                  the contiguous-from-C0 watermark and the swept part of
                  frontier-high's region.

Pure: no DB, no git, no import of mcp_server. Clocks are injected. See
docs/superpowers/specs/2026-09-11-ingest-status-observability-design.md.
"""
from __future__ import annotations

import datetime
import time
from typing import Any, Callable, Dict, Optional, Sequence

_STREAM_NAMES = {"fwd": "forward", "rev": "reverse"}
_OUTCOMES = ("written", "skipped", "failed")

# mcp_server._correction_sweep_next's decline reasons -> (sweep state,
# blocked_reason). "selected" is not here: it is the one reason that is not a
# decline.
SWEEP_STATE_FOR_REASON: Dict[str, tuple] = {
    "no-frontier-high": ("not-needed", None),
    "reached-ceiling": ("done", None),
    "gap-open": ("blocked", "gap-open"),
    "fragmented": ("blocked", "fragmented"),
    "stale-bound": ("blocked", "stale-bound"),
    "metadata-mismatch": ("blocked", "metadata-mismatch"),
}
_SWEEP_END_STATES = ("stopped", "aborted")


def utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


class _Stream:
    __slots__ = ("state", "written", "skipped", "failed", "last_at")

    def __init__(self, state: str):
        self.state = state
        self.written = 0
        self.skipped = 0
        self.failed = 0
        self.last_at: Optional[str] = None

    @property
    def retired(self) -> int:
        return self.written + self.skipped + self.failed


class RunProgress:
    def __init__(
        self,
        linearization: Sequence[str],
        to_retire: int,
        lineage_pos: Optional[int],
        sweep_lo: Optional[int],
        sweep_through: Optional[int],
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], str] = utc_iso,
    ):
        self._lin = linearization
        self.total = len(linearization)
        if not 0 <= to_retire <= self.total:
            raise ValueError(f"to_retire {to_retire} outside [0, {self.total}]")
        self.to_retire = to_retire
        # -1: watermark unset (nothing confirmed). None: watermark set but
        # its hash does not resolve in this linearization -- reported as
        # null, never guessed (#316's absent-is-not-zero idiom).
        self._lineage_pos = lineage_pos
        self._lineage_folded = False
        self._sweep_lo = sweep_lo
        self._sweep_through = sweep_through
        self._mono = mono
        self._wall = wall
        initial = "not-needed" if to_retire == 0 else "not-started"
        self._streams = {"fwd": _Stream(initial), "rev": _Stream(initial)}
        self._stage_a_mono: Optional[float] = None
        self._sweep_state = "waiting"
        self._sweep_blocked: Optional[str] = None
        self._swept = 0
        self._to_sweep: Optional[int] = None
        self._sweep_mono: Optional[float] = None
        self._sweep_last_at: Optional[str] = None
        self.started_at = wall()
        self._started_mono = mono()
        self._last_progress_at: Optional[str] = None
        self._last_progress_mono: Optional[float] = None

    # -- counts -------------------------------------------------------------
    @property
    def written(self) -> int:
        return sum(s.written for s in self._streams.values())

    @property
    def skipped(self) -> int:
        return sum(s.skipped for s in self._streams.values())

    @property
    def failed(self) -> int:
        return sum(s.failed for s in self._streams.values())

    @property
    def retired_count(self) -> int:
        return self.written + self.skipped + self.failed

    # -- events -------------------------------------------------------------
    def _progressed(self) -> str:
        self._last_progress_mono = self._mono()
        self._last_progress_at = self._wall()
        return self._last_progress_at

    def stage_a_started(self) -> None:
        self._stage_a_mono = self._mono()
        if self.to_retire > 0:
            for s in self._streams.values():
                s.state = "running"

    def retired(self, stream: str, outcome: str, pos: int) -> None:
        if stream not in self._streams or outcome not in _OUTCOMES:
            raise ValueError(f"unknown retirement {stream!r}/{outcome!r}")
        s = self._streams[stream]
        setattr(s, outcome, getattr(s, outcome) + 1)
        s.last_at = self._progressed()
        # A forward write that did not raise has just persisted
        # :ingestion/lineage-confirmed-through at this position
        # (_forward_apply, lifecycle_only=False). Mirror it.
        if stream == "fwd" and outcome == "written":
            self._lineage_pos = pos

    def stage_a_finished(self, completed: bool) -> None:
        for s in self._streams.values():
            if s.state in ("not-started", "running"):
                if completed:
                    s.state = "done" if s.retired else "not-needed"
                else:
                    s.state = "stopped"
        if not completed:
            self._sweep_state = "not-run"

    def sweep_planned(
        self,
        reason: str,
        region_lo: Optional[int],
        start_pos: Optional[int],
        ceiling_pos: Optional[int],
    ) -> None:
        self._sweep_mono = self._mono()
        # Whatever an earlier run already swept of this region is confirmed:
        # _correction_sweep_next starts at the persisted sweep watermark + 1.
        if region_lo is not None and start_pos is not None and start_pos > region_lo:
            self._sweep_lo = region_lo
            self._sweep_through = start_pos - 1
        if reason == "selected":
            self._sweep_state = "running"
            self._to_sweep = ceiling_pos - start_pos + 1
            if self._sweep_lo is None:
                self._sweep_lo = region_lo
            return
        if reason not in SWEEP_STATE_FOR_REASON:
            raise ValueError(f"unknown sweep reason {reason!r}")
        self._sweep_state, self._sweep_blocked = SWEEP_STATE_FOR_REASON[reason]
        if self._sweep_state == "done":
            self._to_sweep = 0

    def swept(self, pos: int) -> None:
        self._swept += 1
        self._sweep_through = pos
        self._sweep_last_at = self._progressed()

    def sweep_ended(self, outcome: str) -> None:
        if outcome in _SWEEP_END_STATES:
            self._sweep_state = outcome
            return
        if outcome not in SWEEP_STATE_FOR_REASON:
            raise ValueError(f"unknown sweep outcome {outcome!r}")
        self._sweep_state, self._sweep_blocked = SWEEP_STATE_FOR_REASON[outcome]

    def folded(self) -> None:
        self._lineage_folded = True

    def ended(self, status: str) -> None:
        if status == "complete":
            return
        for s in self._streams.values():
            if s.state in ("not-started", "running"):
                s.state = status
        if self._sweep_state == "waiting":
            self._sweep_state = "not-run"
        elif self._sweep_state == "running":
            self._sweep_state = status

    # -- derived ------------------------------------------------------------
    def _lineage_confirmed(self) -> Optional[int]:
        if self._lineage_folded:
            return self.total
        if self._lineage_pos is None:
            return None
        covered = self._lineage_pos + 1
        lo, through = self._sweep_lo, self._sweep_through
        if lo is not None and through is not None:
            extra_lo = max(lo, self._lineage_pos + 1)
            if through >= extra_lo:
                covered += through - extra_lo + 1
        return min(covered, self.total)

    def _rate(self, n: int, since: Optional[float], now: float) -> Optional[float]:
        if n == 0 or since is None or now <= since:
            return None
        return round(n / ((now - since) / 60.0), 2)

    def snapshot(self) -> Dict[str, Any]:
        now = self._mono()
        streams: Dict[str, Any] = {}
        for tag, s in self._streams.items():
            d: Dict[str, Any] = {
                "state": s.state, "retired": s.retired, "written": s.written,
                "failed": s.failed,
                "rate_per_min": self._rate(s.retired, self._stage_a_mono, now),
                "last_at": s.last_at,
            }
            if tag == "rev":
                d["skipped"] = s.skipped
            streams[_STREAM_NAMES[tag]] = d
        streams["sweep"] = {
            "state": self._sweep_state, "swept": self._swept,
            "to_sweep": self._to_sweep, "blocked_reason": self._sweep_blocked,
            "rate_per_min": self._rate(self._swept, self._sweep_mono, now),
            "last_at": self._sweep_last_at,
        }
        confirmed = self._lineage_confirmed()
        if self._lineage_folded:
            through = self._lin[-1] if self._lin else None
        elif self._lineage_pos is not None and self._lineage_pos >= 0:
            through = self._lin[self._lineage_pos]
        else:
            through = None
        last = self._last_progress_mono if self._last_progress_mono is not None else self._started_mono
        return {
            "this_run": {
                "to_retire": self.to_retire, "retired": self.retired_count,
                "written": self.written, "skipped": self.skipped, "failed": self.failed,
                "started_at": self.started_at,
                "last_progress_at": self._last_progress_at,
                "seconds_since_progress": round(now - last, 3),
            },
            "streams": streams,
            "visibility": {
                "verified": (self.total - self.to_retire) + self.written + self.skipped,
                "total": self.total,
                "complete": self.failed == 0 and self.retired_count == self.to_retire,
            },
            "lineage": {
                "confirmed": confirmed, "total": self.total,
                "complete": confirmed == self.total,
                "confirmed_through": through,
            },
        }
```

- [ ] **Step 6: Register the module for packaging**

`pyproject.toml:107`:

```toml
py-modules = ["mcp_server", "report_issue", "fact_index", "frontier_registry", "ingest_progress"]
```

- [ ] **Step 7: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ingest_progress.py tests/test_frontier_registry.py tests/test_packaging.py -q`
Expected: all pass. If a lineage/union test fails, fix the model, not the test. Each expected value above is derived in the test's docstring or comment.

- [ ] **Step 8: Ablation — prove the union test and the rejection tests guard something**

1. Temporarily replace the union branch in `_lineage_confirmed` with `covered += through - lo + 1` (a plain sum). Run `pytest tests/test_ingest_progress.py -q -k union`. Expected: FAIL, 15 != 10. Revert.
2. Temporarily delete the `raise ValueError` in `retired`. Run `-k rejected`. Expected: FAIL. Revert.

Record both failure lines in the commit message.

- [ ] **Step 9: Commit**

```bash
git add ingest_progress.py tests/test_ingest_progress.py frontier_registry.py tests/test_frontier_registry.py pyproject.toml
git commit -m "Add the RunProgress per-run progress model (part of #222 phase 4)

<ablation results>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UZDij8wb7jHW7CAj55ZUbw"
```

---

### Task 2: `_correction_sweep_next` — the sweep selector with a reason

**Files:**
- Modify: `mcp_server.py` (`_correction_sweep_select_position`, ~line 12227)
- Test: `tests/test_mcp_server.py` (new class appended at the end of the file)

**Interfaces:**
- Consumes: the reason strings from Task 1's `SWEEP_STATE_FOR_REASON`, plus `"selected"`.
- Produces: `mcp_server._SweepNext` (a `NamedTuple` with fields `selected: Optional[Tuple[str, str]]`, `reason: str`, `region_lo: Optional[int]`, `start_pos: Optional[int]`, `ceiling_pos: Optional[int]`) and `mcp_server._correction_sweep_next(db, linearization, commit_metadata, hash_to_pos=None, fragmented=None) -> _SweepNext`. `_correction_sweep_select_position` keeps its exact signature and return value.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`. Frontier state is seeded exactly the way `TestFrontierPromoteBaseIfMissing._seed_interval` (`tests/test_mcp_server.py:8543`) already seeds it: raw interval facts through the internal `_transact`, because `:type/ingest-interval` is deliberately unregistered and the public handler would refuse it. The sweep watermark is written through the real `_correction_sweep_through_update`.

```python
class TestCorrectionSweepNextReasons:
    """#222 phase 4: _correction_sweep_select_position returns None for
    seven reasons a caller cannot tell apart. _correction_sweep_next names
    them, so Stage B can report WHY it declined (C1: a reverse floor keeps
    the gap open and the sweep never runs, on a run reporting complete)."""

    LIN = [f"h{i}" for i in range(6)]
    META = [(h, "2026-09-04T00:00:00Z", "a", f"s{i}") for i, h in enumerate(LIN)]

    def _seed(self, db, ident, lo, hi, tag=":provisional"):
        import mcp_server
        facts = [
            f"[{ident} :entity-type :type/ingest-interval]",
            f"[{ident} :tag {tag}]",
            f'[{ident} :lo-hash "{self.LIN[lo]}"]',
            f'[{ident} :hi-hash "{self.LIN[hi]}"]',
            f"[{ident} :pos-count {hi - lo + 1}]",
        ]
        mcp_server._transact(db, "[" + " ".join(facts) + "]", "2026-09-04T00:00:00Z")

    def _seed_closed(self, db):
        """frontier-low [0,2] meets frontier-high [3,5]: the gap is closed."""
        import mcp_server
        self._seed(db, mcp_server._FRONTIER_LOW_IDENT, 0, 2, tag=":authoritative")
        self._seed(db, mcp_server._FRONTIER_HIGH_IDENT, 3, 5)

    def _seed_gap(self, db):
        """frontier-low [0,0], frontier-high [3,5]: positions 1-2 unclaimed."""
        import mcp_server
        self._seed(db, mcp_server._FRONTIER_LOW_IDENT, 0, 0, tag=":authoritative")
        self._seed(db, mcp_server._FRONTIER_HIGH_IDENT, 3, 5)

    def _next(self, db, lin=None, meta=None, fragmented=None):
        import mcp_server
        return mcp_server._correction_sweep_next(
            db, self.LIN if lin is None else lin, self.META if meta is None else meta,
            None, fragmented,
        )

    def test_no_frontier_high(self, real_db):
        r = self._next(real_db)
        assert (r.selected, r.reason) == (None, "no-frontier-high")

    def test_gap_open(self, real_db):
        self._seed_gap(real_db)
        r = self._next(real_db)
        assert (r.selected, r.reason, r.region_lo) == (None, "gap-open", 3)

    def test_selected_then_reached_ceiling(self, real_db):
        import mcp_server
        self._seed_closed(real_db)
        r = self._next(real_db)
        assert r.reason == "selected"
        assert r.selected == (self.LIN[3], self.META[3][1])
        assert (r.region_lo, r.start_pos, r.ceiling_pos) == (3, 3, 5)
        mcp_server._correction_sweep_through_update(real_db, self.LIN[5], self.META[5][1])
        r = self._next(real_db)
        assert (r.selected, r.reason, r.region_lo, r.start_pos, r.ceiling_pos) == (
            None, "reached-ceiling", 3, 6, 5,
        )

    def test_stale_bound(self, real_db):
        """frontier-high's :lo-hash no longer resolves (rewritten history)."""
        self._seed_closed(real_db)
        lin = ["rewritten" if i == 3 else h for i, h in enumerate(self.LIN)]
        meta = [(h, *m[1:]) for h, m in zip(lin, self.META)]
        r = self._next(real_db, lin=lin, meta=meta)
        assert (r.selected, r.reason) == (None, "stale-bound")

    def test_fragmented(self, real_db):
        self._seed_closed(real_db)
        r = self._next(real_db, fragmented=True)
        assert (r.selected, r.reason, r.region_lo) == (None, "fragmented", 3)

    def test_metadata_mismatch(self, real_db):
        self._seed_closed(real_db)
        r = self._next(real_db, meta=self.META[:-1])
        assert (r.selected, r.reason, r.start_pos, r.ceiling_pos) == (
            None, "metadata-mismatch", 3, 5,
        )

    @pytest.mark.parametrize("seed", ["none", "gap", "closed", "swept"])
    def test_wrapper_returns_exactly_selected(self, real_db, seed):
        import mcp_server
        if seed == "gap":
            self._seed_gap(real_db)
        elif seed in ("closed", "swept"):
            self._seed_closed(real_db)
        if seed == "swept":
            mcp_server._correction_sweep_through_update(real_db, self.LIN[5], self.META[5][1])
        assert mcp_server._correction_sweep_select_position(
            real_db, self.LIN, self.META,
        ) == self._next(real_db).selected
```

- [ ] **Step 2: Run and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCorrectionSweepNextReasons -q`
Expected: FAIL, `AttributeError: module 'mcp_server' has no attribute '_correction_sweep_next'`.

- [ ] **Step 3: Implement**

In `mcp_server.py`, directly above `def _correction_sweep_select_position`, add the `NamedTuple` (add `NamedTuple` to the `typing` import on line 34) and `_correction_sweep_next`. Move the **current body** of `_correction_sweep_select_position` into it unchanged, except for the returns:

```python
class _SweepNext(NamedTuple):
    """_correction_sweep_next's answer: the next commit to sweep, or why
    there is none. `reason` is "selected" or one of
    ingest_progress.SWEEP_STATE_FOR_REASON's keys. The positions are filled
    wherever the function got far enough to know them, so a caller can plan
    `to_sweep` (ceiling - start + 1) from the same call that selects."""
    selected: Optional[Tuple[str, str]]
    reason: str
    region_lo: Optional[int] = None
    start_pos: Optional[int] = None
    ceiling_pos: Optional[int] = None


def _correction_sweep_next(
    db: Any,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    hash_to_pos: Optional[Dict[str, int]] = None,
    fragmented: Optional[bool] = None,
) -> _SweepNext:
    """_correction_sweep_select_position's body, returning WHY as well as
    WHAT (#222 phase 4). Every gate, and the order of the gates, is
    unchanged -- see _correction_sweep_select_position's docstring for what
    each one guards."""
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is None:
        return _SweepNext(None, "no-frontier-high")
    if hash_to_pos is None:
        hash_to_pos = {h: i for i, h in enumerate(linearization)}
    if high_bounds[0] not in hash_to_pos:
        return _SweepNext(None, "stale-bound")
    region_lo = hash_to_pos[high_bounds[0]]
    if low_bounds is None:
        low_hi_pos = -1  # (keep the existing comment block verbatim here)
    else:
        if low_bounds[1] not in hash_to_pos:
            return _SweepNext(None, "stale-bound", region_lo)
        low_hi_pos = hash_to_pos[low_bounds[1]]
    if low_hi_pos + 1 != region_lo:
        return _SweepNext(None, "gap-open", region_lo)
    if fragmented if fragmented is not None else _intervals_read_extra(db):
        return _SweepNext(None, "fragmented", region_lo)
    if high_bounds[1] not in hash_to_pos:
        return _SweepNext(None, "stale-bound", region_lo)
    ceiling_pos = hash_to_pos[high_bounds[1]]
    through_hash = _correction_sweep_through_query(db)
    if through_hash is not None and through_hash in hash_to_pos:
        pos = hash_to_pos[through_hash] + 1
    else:
        pos = region_lo
    if pos > ceiling_pos:
        return _SweepNext(None, "reached-ceiling", region_lo, pos, ceiling_pos)
    if len(commit_metadata) != len(linearization) or commit_metadata[pos][0] != linearization[pos]:
        return _SweepNext(None, "metadata-mismatch", region_lo, pos, ceiling_pos)
    commit_hash, commit_ts_iso, _author, _subject = commit_metadata[pos]
    return _SweepNext((commit_hash, commit_ts_iso), "selected", region_lo, pos, ceiling_pos)
```

Carry every existing inline comment from the old body onto the matching line. The code above abbreviates them. Then replace the old body with:

```python
    return _correction_sweep_next(
        db, linearization, commit_metadata, hash_to_pos, fragmented,
    ).selected
```

Keep the old docstring intact above it.

- [ ] **Step 4: Run and verify they pass, and that the old selector tests still pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q -k "correction_sweep or CorrectionSweep or sweep"`
Expected: PASS, with no change in the count of previously-passing tests.

- [ ] **Step 5: Ablation**

Change the `"gap-open"` return to `"fragmented"` and run `-k TestCorrectionSweepNextReasons`. Expected: `test_gap_open` FAILS. Revert. Record the result in the commit message.

- [ ] **Step 6: Commit** (message: "Name why the correction sweep declines (part of #222 phase 4)", with the trailer)

---

### Task 3: Wire `RunProgress` into `_run_ingestion` and the status handler

`processed` stays maintained in this task, because consumers still read it. It is deleted in Task 6.

**Files:**
- Modify: `mcp_server.py`: import; `_build_run_progress`; `_run_ingestion` (top ~12962, lease block ~13132–13150, `submit_next` skip site ~13420–13431, pipeline start ~13439, extraction-failure site ~13475–13485, post-write site ~13596–13611, after the `while pending` loop ~13612, Stage B ~13843–13930, fold ~13936–13941, terminal paths ~13995–13999 and ~14034–14037); `handle_minigraf_ingest_status` (~14146)
- Test: `tests/test_mcp_server.py` (new class `TestIngestStatusPhase4E2E` + module-level helpers, appended)

**Interfaces:**
- Consumes: Task 1's `RunProgress`, `unclaimed_count()`. Task 2's `_correction_sweep_next` / `_SweepNext`.
- Produces:
  - `_ingest_progress["_run"]`: a `RunProgress` or `None`
  - `mcp_server._build_run_progress(linearization, allocator, lct_hash, sweep_through_hash) -> ingest_progress.RunProgress`
  - Status response keys `this_run`, `streams`, `visibility`, `lineage`
  - Test helpers `_phase4_linear_repo(tmp_path, n, name="repo")`, `_phase4_add_commit(repo, i)` and `_phase4_run(repo, graph_path, monkeypatch, branch="master")`, reused by Task 4

- [ ] **Step 1: Write the failing end-to-end tests**

Append to `tests/test_mcp_server.py`:

```python
# ---------------------------------------------------------------------------
# #222 phase 4: status observability, end to end (real backend, real git)
# ---------------------------------------------------------------------------

def _phase4_add_commit(repo, i):
    (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    ts = (
        datetime.datetime(2021, 3, 1, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(days=i)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = {**os.environ, "GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts}
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True,
                    capture_output=True, env=env)


def _phase4_linear_repo(tmp_path, n, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    for args in (["init", "-b", "master"], ["config", "user.email", "t@t.com"],
                 ["config", "user.name", "T"]):
        _subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    for i in range(n):
        _phase4_add_commit(repo, i)
    return repo


def _phase4_run(repo, graph_path, monkeypatch, branch="master"):
    """One real _run_ingestion against an on-disk graph, then the status the
    handler reports and the graph's true commit count. Leases are released
    around it (docs/testing-conventions.md, pattern 2)."""
    import mcp_server
    monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph_path))
    mcp_server._reset_db_state()
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        "phase": None, "positions_skipped": 0,
    }
    asyncio.run(mcp_server._run_ingestion(str(repo), branch))
    mcp_server._reset_db_state()
    status = mcp_server.handle_minigraf_ingest_status()
    with mcp_server.db_lease() as db:
        graph_commits = mcp_server._count_commit_entities(db)
    mcp_server._reset_db_state()
    return status, graph_commits


class TestIngestStatusPhase4E2E:
    def test_status_carries_the_phase4_blocks_after_a_fresh_run(self, tmp_path, monkeypatch):
        repo = _phase4_linear_repo(tmp_path, 10)
        status, graph_commits = _phase4_run(repo, tmp_path / "g.graph", monkeypatch)
        assert status["status"] == "complete"
        assert graph_commits == 10
        run = status["this_run"]
        assert (run["to_retire"], run["retired"], run["written"], run["failed"]) == (10, 10, 10, 0)
        assert status["visibility"] == {"verified": 10, "total": 10, "complete": True}
        assert status["lineage"]["complete"] is True
        assert status["streams"]["sweep"]["state"] == "done"
        assert status["streams"]["forward"]["state"] == "done"
        assert status["streams"]["reverse"]["state"] == "done"
        json.dumps(status)  # the RunProgress object itself must never leak into the response

    def test_a_no_op_rerun_reports_not_needed_streams(self, tmp_path, monkeypatch):
        repo = _phase4_linear_repo(tmp_path, 6)
        graph = tmp_path / "g.graph"
        _phase4_run(repo, graph, monkeypatch)
        status, _ = _phase4_run(repo, graph, monkeypatch)
        assert status["this_run"]["to_retire"] == 0
        assert status["streams"]["forward"]["state"] == "not-needed"
        assert status["streams"]["reverse"]["state"] == "not-needed"
        assert status["visibility"]["complete"] is True
        assert status["lineage"]["complete"] is True

    def test_stage_b_reports_sweep_progress_instead_of_freezing(self, tmp_path, monkeypatch):
        """Master: every sweep step sampled processed == total (20/20) with no
        sweep counter at all. Now `swept` climbs to `to_sweep`."""
        import mcp_server
        repo = _phase4_linear_repo(tmp_path, 12)
        samples = []
        real_apply = mcp_server._correction_sweep_apply

        def spy(*args, **kwargs):
            run = mcp_server._ingest_progress["_run"]
            sw = run.snapshot()["streams"]["sweep"]
            samples.append((mcp_server._ingest_progress["phase"], sw["state"], sw["swept"], sw["to_sweep"]))
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(mcp_server, "_correction_sweep_apply", spy)
        status, _ = _phase4_run(repo, tmp_path / "g.graph", monkeypatch)
        assert samples, "Stage B must sweep something on a fresh 1:1 run"
        to_sweep = samples[0][3]
        assert to_sweep == len(samples)
        assert [s[2] for s in samples] == list(range(len(samples)))
        assert all(s[0] == "sweeping" and s[1] == "running" for s in samples)
        sw = status["streams"]["sweep"]
        assert (sw["state"], sw["swept"], sw["to_sweep"]) == ("done", to_sweep, to_sweep)

    def _fail_second_reverse_write(self, monkeypatch):
        import mcp_server
        real = mcp_server._reverse_apply
        calls = {"n": 0}

        def failing(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected write failure")
            return real(*args, **kwargs)

        monkeypatch.setattr(mcp_server, "_reverse_apply", failing)
        return real

    def test_a_lost_commit_is_no_longer_reported_as_complete(self, tmp_path, monkeypatch):
        """C1. Master reported `complete` at 20/20 while the graph held 19
        commits and Stage B never ran."""
        import mcp_server
        repo = _phase4_linear_repo(tmp_path, 20)
        self._fail_second_reverse_write(monkeypatch)
        status, graph_commits = _phase4_run(repo, tmp_path / "g.graph", monkeypatch)
        assert graph_commits == 19
        assert status["status"] == "complete"  # the run itself finished
        assert status["this_run"]["failed"] == 1
        assert status["visibility"] == {"verified": 19, "total": 20, "complete": False}
        sw = status["streams"]["sweep"]
        assert (sw["state"], sw["blocked_reason"]) == ("blocked", "gap-open")
        assert status["lineage"]["complete"] is False

    def test_a_rewalk_never_reports_more_than_it_had_to_do(self, tmp_path, monkeypatch):
        """C2. The re-walk below C1's reverse floor. Master's seeded counter
        read 28/20 here; this also recomputes that old formula on the same
        samples, so the scenario provably reaches the defect."""
        import mcp_server
        repo = _phase4_linear_repo(tmp_path, 20)
        graph = tmp_path / "g.graph"
        real_reverse = self._fail_second_reverse_write(monkeypatch)
        _phase4_run(repo, graph, monkeypatch)
        monkeypatch.setattr(mcp_server, "_reverse_apply", real_reverse)

        samples = []

        def sampling(*args, **kwargs):
            p = mcp_server._ingest_progress
            run = p["_run"].snapshot()["this_run"]
            samples.append((run["retired"], run["to_retire"], p["prior_ingested"]))
            return real_reverse(*args, **kwargs)

        monkeypatch.setattr(mcp_server, "_reverse_apply", sampling)
        status, graph_commits = _phase4_run(repo, graph, monkeypatch)
        assert graph_commits == 20
        assert all(retired <= to_retire for retired, to_retire, _ in samples)
        run = status["this_run"]
        assert run["retired"] == run["to_retire"]
        old_processed = status["prior_ingested"] + run["retired"]
        assert old_processed > status["total"], (
            f"scenario no longer reaches the #222-phase-4 defect: old formula "
            f"{old_processed} <= total {status['total']}"
        )
        assert status["visibility"]["complete"] is True
        assert status["lineage"]["complete"] is True

    def test_new_counter_equals_old_processed_census_parity(self, tmp_path, monkeypatch):
        """#317's commit_census reads walk_claimed; after Task 6 it is
        prior_ingested + this_run.retired. It must be the SAME number
        `processed` held, or the census gates change meaning."""
        repo = _phase4_linear_repo(tmp_path, 8)
        graph = tmp_path / "g.graph"
        _phase4_run(repo, graph, monkeypatch)
        _phase4_add_commit(repo, 8)
        status, _ = _phase4_run(repo, graph, monkeypatch)
        assert status["processed"] == status["prior_ingested"] + status["this_run"]["retired"]
```

`datetime`, `os`, `json`, `asyncio`, `_subprocess` are already imported at the top of `tests/test_mcp_server.py`. Verify with `grep -n "^import\|^from" tests/test_mcp_server.py | head -30`, and add any that are missing.

- [ ] **Step 2: Run and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestStatusPhase4E2E -q`
Expected: every test FAILS, with `KeyError: 'this_run'` or `KeyError: '_run'`.

- [ ] **Step 3: Import and `_build_run_progress`**

`mcp_server.py` line 40, after `import frontier_registry`:

```python
import ingest_progress
```

Directly above `async def _run_ingestion`, add:

```python
def _build_run_progress(
    linearization: List[str],
    allocator: "frontier_registry.FrontierAllocator",
    lct_hash: Optional[str],
    sweep_through_hash: Optional[str],
) -> "ingest_progress.RunProgress":
    """#222 phase 4: the run's progress model, from the frontier AS LOADED.

    `to_retire` is the allocator's gap at load -- every position this run
    will claim, each retired at most once. The two watermarks are mapped
    into this run's position space: an unset lineage watermark is -1
    (nothing confirmed), one whose hash no longer resolves is None (reported
    as null, never guessed). The sweep watermark only counts when it falls
    inside the base provisional interval, the region the sweep confirms.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    lineage_pos: Optional[int] = -1 if lct_hash is None else hash_to_pos.get(lct_hash)
    base = next(
        (iv for iv in allocator.intervals()
         if iv.tag == frontier_registry.TAG_PROVISIONAL and iv.is_base),
        None,
    )
    sweep_lo = sweep_through = None
    if base is not None and sweep_through_hash is not None:
        p = hash_to_pos.get(sweep_through_hash)
        if p is not None and base.lo_pos <= p <= base.hi_pos:
            sweep_lo, sweep_through = base.lo_pos, p
    return ingest_progress.RunProgress(
        linearization, allocator.unclaimed_count(), lineage_pos, sweep_lo, sweep_through,
    )
```

- [ ] **Step 4: Reset at run start**

After `_ingest_progress["index_cross_check"] = None` (~line 12962), add:

```python
    # #222 phase 4: this run's progress model. None until it is built from
    # the loaded frontier below, for the same reason as index_cross_check: a
    # run refused or failing before that point must never show a previous
    # run's numbers. It lives in the dict, not a module global, so every
    # site that resets _ingest_progress by assignment clears it too.
    _ingest_progress["_run"] = None
```

- [ ] **Step 5: Build it in the frontier lease**

In the `async with db_lease_async() as db:` block that calls `_frontier_load` and `_completed_regions_load` (~13132–13149), append inside the block after `completed_regions = ...`:

```python
            lct_hash = await loop.run_in_executor(
                write_executor, _lineage_confirmed_through_query, db,
            )
            sweep_through_hash = await loop.run_in_executor(
                write_executor, _correction_sweep_through_query, db,
            )
        run_progress = _build_run_progress(
            linearization, allocator, lct_hash, sweep_through_hash,
        )
        _ingest_progress["_run"] = run_progress
```

`run_progress = ...` is dedented out of the `async with`. The next line is `claimer = _RoundRobinClaimer(...)`.

- [ ] **Step 6: The three Stage A retirement sites**

(a) In `submit_next`, directly after `_ingest_progress["processed"] += 1` (the skip path, ~13431):

```python
                        run_progress.retired(tag, "skipped", pos)
```

(b) In the extraction-failure `except Exception as e:`, directly after `_ingest_progress["processed"] += 1` (~13483):

```python
                        run_progress.retired(tag, "failed", pos)
```

(c) At the post-write site, directly after `_ingest_progress["processed"] += 1` (~13610):

```python
                    run_progress.retired(tag, "written" if _trace_write_ok else "failed", pos)
```

(d) Directly before `for _ in range(pipeline_depth):` (~13439):

```python
                run_progress.stage_a_started()
```

(e) Directly after the `while pending:` loop ends and before the `# #326: the walk may have ended with the gap empty...` comment (~13613), at the same indentation as `while pending:`:

```python
                run_progress.stage_a_finished(completed_all)
```

- [ ] **Step 7: Stage B**

Replace the loop from `while not _shutdown_requested.is_set():` through the post-loop `if _shutdown_requested.is_set(): completed_all = False` (~13846–13929) with the version below. The `try` body between `sweep_hash, sweep_ts = ...` and the checkpoint is unchanged except for the one added `run_progress.swept(...)` line. Keep every existing comment:

```python
                        # #222 phase 4: the first call both PLANS the sweep
                        # (to_sweep, or why it declines) and is the loop's
                        # first iteration, so planning costs no query.
                        nxt = await loop.run_in_executor(
                            write_executor, _correction_sweep_next,
                            db, linearization, commit_metadata, hash_to_pos,
                            sweep_fragmented,
                        )
                        run_progress.sweep_planned(
                            nxt.reason, nxt.region_lo, nxt.start_pos, nxt.ceiling_pos,
                        )
                        while not _shutdown_requested.is_set():
                            if nxt.selected is None:
                                run_progress.sweep_ended(nxt.reason)
                                break
                            sweep_hash, sweep_ts = nxt.selected
                            try:
                                # ... unchanged: _extract_commit, _correction_sweep_apply,
                                # _forward_apply(lifecycle_only=True) ...
                                await loop.run_in_executor(
                                    write_executor, _correction_sweep_through_update,
                                    db, sweep_hash, sweep_ts, index_con,
                                )
                                run_progress.swept(hash_to_pos[sweep_hash])
                                await loop.run_in_executor(write_executor, _db_checkpoint_gated, db)
                            except concurrent.futures.process.BrokenProcessPool:
                                raise
                            except Exception as e:
                                # ... unchanged print + e.__traceback__ = None ...
                                completed_all = False
                                run_progress.sweep_ended("aborted")
                                break
                            await asyncio.sleep(0)  # yield to event loop
                            nxt = await loop.run_in_executor(
                                write_executor, _correction_sweep_next,
                                db, linearization, commit_metadata, hash_to_pos,
                                sweep_fragmented,
                            )
                        if _shutdown_requested.is_set():
                            completed_all = False
                            run_progress.sweep_ended("stopped")
```

In the fold (`if should_fold:`), after the `_lineage_confirmed_through_update` executor call:

```python
                            run_progress.folded()
```

- [ ] **Step 8: Terminal paths**

After the `if completed_all: ... status = "complete" else: ... "stopped"` block (~13996–13999):

```python
            run_progress.ended(_ingest_progress["status"])
```

In the top-level `except Exception as e:` after `_ingest_progress["error_at"] = _now_utc_ms()` (~14037):

```python
        if _ingest_progress.get("_run") is not None:
            _ingest_progress["_run"].ended("error")
```

- [ ] **Step 9: The handler**

In `handle_minigraf_ingest_status`, replace:

```python
    result: Dict[str, Any] = {"ok": True, **_ingest_progress}
```

with:

```python
    # Keys starting with "_" are in-process objects (#222 phase 4's "_run"),
    # never response fields -- call_tool json.dumps this dict.
    result: Dict[str, Any] = {
        "ok": True,
        **{k: v for k, v in _ingest_progress.items() if not k.startswith("_")},
    }
    run = _ingest_progress.get("_run")
    if run is not None:
        result.update(run.snapshot())
```

Leave `processed_this_run` and `positions_skipped_this_run` in place for now. Task 6 removes them.

- [ ] **Step 10: Run the new tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestStatusPhase4E2E -q`
Expected: PASS.

If `test_a_lost_commit_is_no_longer_reported_as_complete` reports a `blocked_reason` other than `gap-open`, **stop and investigate**. Do not edit the expectation. The throwaway probe showed Stage B made zero sweep calls in C1, and the spec names `gap-open`, so a different reason means a different mechanism is at work. Report it to the controller.

- [ ] **Step 11: Run the full suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -5`
Expected: the same pass count as master plus the new tests, and 0 failures. Record the numbers.

- [ ] **Step 12: Ablations**

Do each one separately, run the named test, and revert:
1. Delete `run_progress.swept(...)`. Run `-k stage_b_reports`. Expected FAIL.
2. In `snapshot()` (ingest_progress.py), change `"complete": self.failed == 0 and ...` to `"complete": self.retired_count == self.to_retire`. Run `-k lost_commit`. Expected FAIL.
3. In `_build_run_progress`, pass `allocator.total_positions` instead of `allocator.unclaimed_count()`. Run `-k rewalk or no_op`. Expected FAIL.

Record all three in the commit message.

- [ ] **Step 13: Commit** (message: "Report per-run, per-stream and coverage progress from ingestion (part of #222 phase 4)", with the trailer)

---

### Task 4: `last_commit` names the tip; `:total-ingested` is the true count

**Files:**
- Modify: `mcp_server.py` (`_run_ingestion`: `last_hash` lines ~13075, ~13497, and the `_last_run_write` call ~13947–13951)
- Test: `tests/test_mcp_server.py` (`TestIngestStatusPhase4E2E`)

**Interfaces:**
- Consumes: Task 3's helpers `_phase4_linear_repo`, `_phase4_add_commit`, `_phase4_run`, `_fail_second_reverse_write`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

Add to `TestIngestStatusPhase4E2E`:

```python
    def _head(self, repo):
        return _subprocess.run(["git", "rev-parse", "master"], cwd=repo,
                               capture_output=True, text=True, check=True).stdout.strip()

    def test_last_commit_is_the_branch_tip_fresh_incremental_and_noop(self, tmp_path, monkeypatch):
        """Master: the meeting point on a fresh run, the forward watermark on a
        no-op rerun -- never HEAD."""
        repo = _phase4_linear_repo(tmp_path, 10)
        graph = tmp_path / "g.graph"
        status, _ = _phase4_run(repo, graph, monkeypatch)
        assert status["last_commit"] == self._head(repo)
        _phase4_add_commit(repo, 10)
        status, _ = _phase4_run(repo, graph, monkeypatch)
        assert status["last_commit"] == self._head(repo)
        status, _ = _phase4_run(repo, graph, monkeypatch)
        assert status["last_commit"] == self._head(repo)

    def test_total_ingested_is_the_true_commit_count_after_a_rewalk(self, tmp_path, monkeypatch):
        """Master persisted the seeded walk counter: 28 on a 20-commit graph."""
        import mcp_server
        repo = _phase4_linear_repo(tmp_path, 20)
        graph = tmp_path / "g.graph"
        real_reverse = self._fail_second_reverse_write(monkeypatch)
        _phase4_run(repo, graph, monkeypatch)
        monkeypatch.setattr(mcp_server, "_reverse_apply", real_reverse)
        _, graph_commits = _phase4_run(repo, graph, monkeypatch)
        with mcp_server.db_lease() as db:
            persisted = mcp_server._total_ingested_query(db)
        mcp_server._reset_db_state()
        assert persisted == graph_commits == 20
```

- [ ] **Step 2: Run and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestStatusPhase4E2E -q -k "last_commit or total_ingested"`
Expected: FAIL. `last_commit` differs from HEAD on the fresh run, and `persisted == 28`.

- [ ] **Step 3: Implement**

Delete `last_hash = watermark or ""` (~13075) and `last_hash = commit_hash` (~13497). Replace the `_last_run_write` call in the `if completed_all:` bookkeeping block with:

```python
                        # #222 phase 4: the tip this run covered, never
                        # whichever commit Stage A applied last -- on a
                        # converging run that was the meeting point, and on
                        # a no-op run the forward watermark. And the TRUE
                        # commit count, never the seeded walk counter, which
                        # exceeds the repo on any re-walk (28 of 20 measured).
                        graph_commits = await loop.run_in_executor(
                            write_executor, _count_commit_entities, db,
                        )
                        await loop.run_in_executor(
                            write_executor, _last_run_write, db, linearization[-1], now,
                            graph_commits, index_con,
                        )
```

Leave `watermark` in the preload tuple unpack. It is now unused; that is fine, and the preload's return shape does not change.

- [ ] **Step 4: Run and verify they pass, then run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestStatusPhase4E2E -q` then `.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -5`
Expected: PASS, and 0 failures in the full suite.

- [ ] **Step 5: Ablation**

Put `_ingest_progress["processed"]` back as the count argument and run `-k total_ingested`. Expected: FAIL `28 == 20`. Revert and record.

- [ ] **Step 6: Commit** (message: "Record the branch tip and the true commit count as the last run (part of #222 phase 4)", with the trailer)

---

### Task 5: Re-point the at-scale consumers

**Files:**
- Modify: `evals/at_scale/commit_census.py` (add the helper; docstring lines 24–25 and 55–58)
- Modify: `evals/at_scale/run_ingestion_benchmark.py:262`
- Modify: `evals/at_scale/probe_resume_census.py` (lines ~245–349, docstrings at ~47–110, `retention_engaged`)
- Modify: `evals/at_scale/profile_forward_reconcile_attribution.py:342,372`
- Modify: `evals/at_scale/stderr_capture.py:1-7` (docstring)
- Test: `tests/test_at_scale_commit_census.py`, `tests/test_at_scale_resume_census.py`

**Interfaces:**
- Consumes: `_ingest_progress["_run"]` (`RunProgress`: `.retired_count`, `.skipped`) and `_ingest_progress["prior_ingested"]`.
- Produces: `commit_census.walk_claimed_from_progress(progress: Mapping[str, Any]) -> int`. The resume-census output keys `retired_this_run` and `skipped_this_run` replace `processed_this_run` and `positions_skipped_this_run`.

- [ ] **Step 1: Write the failing helper test**

Append to `tests/test_at_scale_commit_census.py`:

```python
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
```

In `tests/test_at_scale_resume_census.py`, rename every `processed_this_run` to `retired_this_run` and `positions_skipped_this_run` to `skipped_this_run`: the `_census(...)` helper's parameters and defaults, the `test_prior_ingested_and_processed_this_run_attribute_correctly` assertion (`result["retired_this_run"] == 3`), and the test names and docstrings. Docstrings that explain `_ingest_progress["processed"]` get the new mechanism: "`walk_claimed_from_progress` = `prior_ingested` + this run's retired count".

- [ ] **Step 2: Run and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_commit_census.py tests/test_at_scale_resume_census.py -q`
Expected: FAIL (ImportError on `walk_claimed_from_progress`, `KeyError: 'retired_this_run'`).

- [ ] **Step 3: Implement the helper**

In `evals/at_scale/commit_census.py`, add after the imports:

```python
def walk_claimed_from_progress(progress: Mapping[str, Any]) -> int:
    """`walk_claimed` from `mcp_server._ingest_progress` (#222 phase 4).

    `prior_ingested` (the graph's commit count at run start) plus the
    positions this run RETIRED -- written, skipped or failed. That is exactly
    the number the removed `_ingest_progress["processed"]` held, so every gate
    below keeps its meaning. A run that failed before its RunProgress was
    built (`_run` None or absent) retired nothing.
    """
    run = progress.get("_run")
    return progress.get("prior_ingested", 0) + (run.retired_count if run is not None else 0)
```

(Add `Mapping` to its `typing` import if missing.) Update the module docstring: item 2 becomes ``walk_claimed` -- `walk_claimed_from_progress(_ingest_progress)`, what the walk CLAIMS it applied``. In the "EXTRACTION-SKIPPED COMMIT" bullet, change `does `_ingest_progress["processed"] += 1`` to "retires the position as `failed`".

- [ ] **Step 4: Re-point the consumers**

- `run_ingestion_benchmark.py:262`: `commits_ingested = walk_claimed_from_progress(mcp_server._ingest_progress)`, and extend the existing `from evals.at_scale.commit_census import collect_commit_census` import.
- `probe_resume_census.py`:
  ```python
      walk_claimed = walk_claimed_from_progress(mcp_server._ingest_progress)
      prior_ingested = mcp_server._ingest_progress.get("prior_ingested", 0)
      run = mcp_server._ingest_progress.get("_run")
      retired_this_run = run.retired_count if run is not None else 0
  ```
  Set `census["retired_this_run"] = retired_this_run` and `census["skipped_this_run"] = run.skipped if run is not None else 0`. In `retention_engaged`, use `census["retired_this_run"]`. Rewrite every docstring and comment in the file that says `processed_this_run`, `positions_skipped_this_run` or `_ingest_progress["processed"]` in the new terms. The mechanism is unchanged: retired counts positions RETIRED, `walk_claimed` = prior + retired. Each changed sentence must be checked against the code, never against neighbouring prose.
- `profile_forward_reconcile_attribution.py`:
  - In the timeline, replace `"processed": m._ingest_progress.get("processed")` with `"retired": (m._ingest_progress["_run"].retired_count if m._ingest_progress.get("_run") else None)`.
  - `"commits_processed"` keeps its key, with value `walk_claimed_from_progress(m._ingest_progress)` (import it).
- `stderr_capture.py` docstring: replace `_ingest_progress ["processed"] increments on the skip paths too. So neither `processed` nor` with `a failed position is still retired, so neither this run's retired count nor`.

- [ ] **Step 5: Run and verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_commit_census.py tests/test_at_scale_resume_census.py tests/test_at_scale_ingestion_benchmark.py tests/test_at_scale_stderr_capture.py -q`
Expected: PASS. The real-run benchmark tests (e.g. `test_a_real_run_censuses_its_own_commits`) must still pass unchanged. They prove `commits_ingested` is identical on a fresh run.

- [ ] **Step 6: Ablation**

In `walk_claimed_from_progress`, drop `progress.get("prior_ingested", 0) +` and run the helper test. Expected: FAIL `9 == 28`. Revert and record.

- [ ] **Step 7: Commit** (message: "Read the walk count from the run's progress model in the at-scale tier (part of #222 phase 4)", with the trailer)

---

### Task 6: Delete `processed`, `processed_this_run`, `positions_skipped(_this_run)`

**Files:**
- Modify: `mcp_server.py`: module-level `_ingest_progress` (~170–181); `_run_ingestion` (seed ~13071–13073, the three `processed += 1` sites and the `positions_skipped += 1` site, and the comments that name them at ~13184–13199 and ~13425–13431); `handle_minigraf_ingest_status` (the two derived keys and their comments); `handle_minigraf_ingest_git` (~14137); `main()` (~14611)
- Modify: `evals/at_scale/run_ingestion_benchmark.py` (dict literals ~150, ~417), `probe_resume_census.py` (`_CLEAN_INGEST_PROGRESS`), `profile_forward_reconcile_attribution.py` (~360), `probe_dep_preload_exposure.py` (~894)
- Test: `tests/test_mcp_server.py`, migrating every site listed below

**Interfaces:**
- Consumes: everything above.
- Produces: the final status contract. No key named `processed`, `processed_this_run`, `positions_skipped` or `positions_skipped_this_run` exists in `_ingest_progress` or the status response.

- [ ] **Step 1: Write the failing guard test**

Add to `TestIngestStatusPhase4E2E`:

```python
    def test_the_processed_family_is_gone(self, tmp_path, monkeypatch):
        import mcp_server
        repo = _phase4_linear_repo(tmp_path, 4)
        status, _ = _phase4_run(repo, tmp_path / "g.graph", monkeypatch)
        removed = {"processed", "processed_this_run", "positions_skipped", "positions_skipped_this_run"}
        assert removed.isdisjoint(status), removed & set(status)
        assert removed.isdisjoint(mcp_server._ingest_progress)
```

Update `_phase4_run`'s reset dict to drop `"processed"` and `"positions_skipped"`. In the census-parity test (Task 3), replace `status["processed"]` with the literal value computed by the old rule, so the test survives the deletion:

```python
        # Pinned before `processed` was deleted: on this scenario the old
        # counter read prior_ingested (8) + retired (1) = 9.
        assert status["prior_ingested"] + status["this_run"]["retired"] == 9
```

Verify the 9 by running it once **before** Step 3 with `status["processed"]` printed. It must equal 9. If not, use the observed value and say so in the commit.

- [ ] **Step 2: Run and verify the guard fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q -k processed_family_is_gone`
Expected: FAIL (`{'processed', ...}`).

- [ ] **Step 3: Delete from production**

- The three `_ingest_progress` initializers (module-level, `handle_minigraf_ingest_git`, `main()`): drop `"processed": 0` and `"positions_skipped": 0`, plus the module-level comment block about the `positions_skipped` name. That naming lesson now lives on `this_run.skipped`. Move a one-line version into `ingest_progress.py`'s `_Stream` if you want to keep it.
- `_run_ingestion`: delete `_ingest_progress["processed"] = prior_ingested` and `_ingest_progress["positions_skipped"] = 0`. Keep `prior_ingested`. Delete the three `_ingest_progress["processed"] += 1` lines and `_ingest_progress["positions_skipped"] += 1`. Rewrite the comments at those sites:
  - Skip site: "a skipped position is still RETIRED (RunProgress 'skipped'), which is what #317's commit_census reads through walk_claimed_from_progress -- excluding it would redefine walk_claimed".
  - ~13184–13199 comment: "does `processed += 1`" becomes "retires the position as failed".
- `handle_minigraf_ingest_status`: delete the `processed_this_run` and `positions_skipped_this_run` derivations and their comments.
- The eval dict literals listed under Files: drop the two keys.

- [ ] **Step 4: Migrate the test suite**

Add near the other helpers at the top of `tests/test_mcp_server.py` (after `execute_spy`):

```python
def _walk_claimed():
    """What `_ingest_progress["processed"]` used to read: prior_ingested plus
    this run's retired count (#222 phase 4)."""
    from evals.at_scale.commit_census import walk_claimed_from_progress
    import mcp_server
    return walk_claimed_from_progress(mcp_server._ingest_progress)
```

Then migrate every site. Find them with `grep -n '"processed"\|processed_this_run\|positions_skipped' tests/test_mcp_server.py`:

| Pattern | Replacement |
|---|---|
| `assert mcp_server._ingest_progress["processed"] == N` | `assert _walk_claimed() == N` (value-preserving; do NOT change N) |
| `first_run_processed = mcp_server._ingest_progress["processed"]` | `first_run_processed = _walk_claimed()` |
| `assert mcp_server._ingest_progress["processed"] <= mcp_server._ingest_progress["total"]` (~25960) | `run = mcp_server._ingest_progress["_run"].snapshot()["this_run"]; assert run["retired"] <= run["to_retire"]`, and rename the test `..._and_retired_never_exceeds_to_retire` |
| `status["positions_skipped_this_run"]` | `status["this_run"]["skipped"]` |
| `result["processed"] == 0` in `test_returns_idle_before_ingestion` | delete the assertion and add `assert "processed" not in result` |
| `result["processed"] == 3` in `test_running_status_skips_graph_query` | delete; the test's point (no graph query while running) is unchanged |
| `"processed": N` / `"positions_skipped": 0` keys inside `_ingest_progress = {...}` literals | delete the key |
| `test_processed_seeded_from_prior_ingested` / `test_processed_seed_ignores_stale_total_ingested_watermark` | keep them, rename to `test_walk_claimed_is_prior_plus_retired...`, assert `_walk_claimed() == 464` / `== 21717` and `mcp_server._ingest_progress["_run"].retired_count == 2` |

Docstrings in those tests that describe `processed` get one sentence of the new mechanism.

- [ ] **Step 5: Full suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -5`
Expected: 0 failures. Then `grep -rn '"processed"\|processed_this_run\|positions_skipped' mcp_server.py evals/ tests/ --include=*.py`. The only remaining hits allowed are the guard test's `removed` set and prose describing the removal. List any others and justify them.

- [ ] **Step 6: Commit** (message: "Remove the seeded processed counter from ingestion status (part of #222 phase 4)", with the trailer)

---

### Task 7: `--progress-interval` on the benchmark

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py` (`_poll_during_ingestion`, `run_ingestion_benchmark`, `main`, new `format_progress_line`)
- Test: `tests/test_at_scale_ingestion_benchmark.py`

**Interfaces:**
- Consumes: the status contract from Tasks 3/6.
- Produces:
  - `format_progress_line(status: Dict[str, Any]) -> str`
  - `_poll_during_ingestion(ingest_task, poll_interval, duty_factor=10.0, progress_interval: Optional[float] = None, out=None)`
  - `run_ingestion_benchmark(..., progress_interval: Optional[float] = None)`
  - CLI `--progress-interval SECONDS`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_ingestion_benchmark.py`:

```python
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
```

- [ ] **Step 2: Run and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py::TestProgressInterval -q`
Expected: FAIL (ImportError on `format_progress_line`, and an unexpected keyword `progress_interval`).

- [ ] **Step 3: Implement**

In `run_ingestion_benchmark.py`, add a module-level function:

```python
_SHORT = {"forward": "fwd", "reverse": "rev"}


def format_progress_line(status: Dict[str, Any]) -> str:
    """One human line from a minigraf_ingest_status response (#222 phase 4).
    Importable so a probe can reuse it; the benchmark prints it to STDOUT,
    never stderr, which stderr_capture scans for error signals."""
    run = status.get("this_run")
    if run is None:
        return f"[progress] {status.get('status')}"
    streams = status["streams"]

    def stream(name: str) -> str:
        st = streams[name]
        rate = st.get("rate_per_min")
        return f"{_SHORT[name]} {st['retired']}" + (f" @{rate:.1f}/min" if rate is not None else "")

    sw = streams["sweep"]
    sweep = f"sweep {sw['state']}" + (f" {sw['swept']}/{sw['to_sweep']}" if sw.get("to_sweep") else "")
    lin, vis = status["lineage"], status["visibility"]
    confirmed = "?" if lin["confirmed"] is None else lin["confirmed"]
    return (
        f"[progress] {status.get('phase') or status.get('status')} "
        f"this_run {run['retired']}/{run['to_retire']} · {stream('forward')} · "
        f"{stream('reverse')} · {sweep} · visibility {vis['verified']}/{vis['total']} · "
        f"lineage {confirmed}/{lin['total']} · idle {run['seconds_since_progress']:.0f}s"
    )
```

(Add `Dict` to the `typing` import if missing.)

In `_poll_during_ingestion`, add the parameters `progress_interval: Optional[float] = None, out=None`. Capture the status result the loop already fetches (`status = await loop.run_in_executor(poll_executor, mcp_server.handle_minigraf_ingest_status)`), and after `status_latencies.append(...)` add:

```python
            if progress_interval is not None:
                now = time.perf_counter()
                if last_progress is None or now - last_progress >= progress_interval:
                    print(format_progress_line(status), file=out or sys.stdout, flush=True)
                    last_progress = now
```

Initialize `last_progress: Optional[float] = None` before the loop. Add `"[progress] lines: --progress-interval (#222 phase 4)"` to the docstring, stating that the lines go to stdout and add no status call.

In `run_ingestion_benchmark`, add `progress_interval: Optional[float] = None` and pass it to `_poll_during_ingestion`. In `main()`:

```python
    parser.add_argument(
        "--progress-interval", type=float, default=None,
        help="Print one [progress] line to STDOUT at most every N seconds, "
             "from the existing status poll (#222 phase 4). Off by default.",
    )
```

and pass `progress_interval=args.progress_interval`.

- [ ] **Step 4: Run and verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py -q`
Expected: PASS.

- [ ] **Step 5: Ablation**

Change `file=out or sys.stdout` to `file=sys.stderr` and run `-k status_calls_equal`. Expected: FAIL. Revert and record.

- [ ] **Step 6: Commit** (message: "Add --progress-interval to the at-scale ingestion benchmark (part of #222 phase 4)", with the trailer)

---

### Task 8: Docs — SKILL.md, tool description, README, CLAUDE.md

**Files:**
- Modify: `SKILL.md` (`### minigraf_ingest_status` section, ~315–366)
- Modify: `mcp_server.py` (`_TOOLS` `minigraf_ingest_status` description, ~14477)
- Modify: `tools/ingest_status.json` (the `description` must equal `_TOOLS`')
- Modify: `README.md:289`
- Modify: `CLAUDE.md` (lines ~333, ~700, ~757–770, ~825, ~850–860, ~1095, ~1114)
- Test: `tests/test_tool_schemas.py`, `tests/test_skill_doc.py` (existing guards)

- [ ] **Step 1: `_TOOLS` description**

Replace the final sentence of the `minigraf_ingest_status` description (from `"positions_skipped_this_run counts positions retired without "` to the end) with:

```python
            "this_run reports this run's own work: to_retire (positions in "
            "the gap at load) and retired (written + skipped + failed), which "
            "never exceeds to_retire; this_run.skipped climbing while the "
            "commit count stays flat means the run is replaying an "
            "already-ingested region (#326). streams gives forward, reverse "
            "and sweep state, counts and rate_per_min; sweep.blocked_reason "
            "says why the confirmation pass declined. status=complete means "
            "only that the run finished: ingestion is done when "
            "visibility.complete and lineage.complete are both true."
```

Then rewrite `tools/ingest_status.json`'s `description` to the identical text with:

```bash
.venv/bin/python - <<'EOF'
import json, mcp_server
t = next(t for t in mcp_server._TOOLS if t.name == "minigraf_ingest_status")
p = "tools/ingest_status.json"
d = json.load(open(p))
d["description"] = t.description
open(p, "w").write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
EOF
git diff --stat tools/ingest_status.json
```

- [ ] **Step 2: SKILL.md**

In `### minigraf_ingest_status`:

1. Replace the example with:

```python
minigraf_ingest_status()
# → {"ok": true, "status": "running", "phase": "converging", "total": 20,
#    "prior_ingested": 19, "current_commit": "a3f2bc...", "error": null,
#    "this_run": {"to_retire": 9, "retired": 4, "written": 4, "skipped": 0,
#                 "failed": 0, "seconds_since_progress": 3.1, ...},
#    "streams": {"forward": {"state": "running", "retired": 2, "rate_per_min": 12.0, ...},
#                "reverse": {"state": "running", "retired": 2, "rate_per_min": 11.8, ...},
#                "sweep": {"state": "waiting", "swept": 0, "to_sweep": null,
#                          "blocked_reason": null, ...}},
#    "visibility": {"verified": 15, "total": 20, "complete": false},
#    "lineage": {"confirmed": 11, "total": 20, "complete": false,
#                "confirmed_through": "e1d4..."}}
```

2. Replace the paragraph starting "`error` also includes `error_at`... `processed` is the cumulative count..." through the end of the `positions_skipped_this_run` paragraph with:
   - `error` also includes `error_at`.
   - **`this_run`** is this run's own work: `to_retire` positions were in the gap when it loaded, and `retired` counts those it has finished (`written`, `skipped` because an earlier run already wrote them completely, or `failed`). It never exceeds `to_retire`. A climbing `skipped` with a flat commit count means the run is replaying an already-ingested region. `seconds_since_progress` growing while nothing retires is a stall.
   - **`streams`**: a table of states for `forward`/`reverse` (`not-started`, `running`, `done`, `not-needed`, `stopped`, `error`) and `sweep` (`waiting`, `running`, `done`, `not-needed`, `blocked` with `blocked_reason` ∈ `gap-open`/`fragmented`/`stale-bound`/`metadata-mismatch`, `not-run`, `stopped`, `aborted`, `error`).
   - **`visibility.verified`** is what the frontier can prove complete. It can be LOWER at the start of a run than the graph's commit count, when earlier positions must be re-walked.
   - **`lineage.confirmed`** is how much history has final `:introduced-by`, and `null` when a watermark no longer resolves (e.g. after a rewritten history).
   - **Done means `visibility.complete and lineage.complete`.** `status: complete` alone only says the run finished. A run that lost a commit or could not confirm lineage finishes with `status: complete` and one flag false.
   - When idle, `total_ingested` is the true persisted commit count, `last_commit` is the branch tip the last completed run covered, and `lineage_confirmed_through` is read from the graph.

3. Add `lineage_confirmed_through` to the handler's idle branch in `mcp_server.py`, inside the same `with db_lease() as db:` block:

```python
                result["lineage_confirmed_through"] = _lineage_confirmed_through_query(db)
```

In the `except Exception:` fallback, add `result["lineage_confirmed_through"] = None`. Add a test to `TestMinigrafIngestStatus`:

```python
    def test_idle_reports_the_lineage_watermark(self, real_db):
        import mcp_server
        mcp_server._ingest_progress = {"status": "idle", "total": 0,
                                       "current_commit": "", "error": None}
        mcp_server._lineage_confirmed_through_update(real_db, "abc123", "2026-01-01T00:00:00.000Z")
        assert mcp_server.handle_minigraf_ingest_status()["lineage_confirmed_through"] == "abc123"
```

This test must fail before the handler line is added: `KeyError`.

- [ ] **Step 3: README**

`README.md:289`: `- **minigraf_ingest_status** — Poll a running git ingestion: this run's work (to_retire/retired), per-stream state and rate, and whether the graph is fully visible and lineage-confirmed; when idle, reports the last completed run's time and branch tip`

- [ ] **Step 4: CLAUDE.md**

Rewrite every passage below against the **code** (`mcp_server.py`, `evals/at_scale/commit_census.py`, `ingest_progress.py`), never against the surrounding prose. The mechanism the census passages explain is unchanged, and only the counter's name moved. Cite the measured passage; do not re-derive it.

- ~333 (#317 census): `_ingest_progress["processed"]` → `walk_claimed_from_progress(_ingest_progress)` (`prior_ingested` + this run's retired count).
- ~700 and ~825 (#326): "does `processed += 1`" → "retires the position as failed".
- ~757–770 (#326 "A skipped position still costs `processed`"): restate as "A skipped position is still RETIRED". #317's `commit_census` reads `walk_claimed = prior_ingested + this_run.retired`, so excluding skips would redefine it. The counter is `this_run.skipped`. The paragraph about `walk_vs_graph` being nonzero on any resume stays true: `prior_ingested` is the seed and the retired count re-counts already-ingested positions.
- ~850–860 (`positions_skipped_this_run` paragraph): replace it with `this_run.skipped` and state that the bare/per-run pair is gone.
- ~1095 and ~1114 (#325 resume census): `_ingest_progress["processed"]` → `walk_claimed_from_progress`; `processed_this_run` → `retired_this_run`. Fix the stale "three increment sites, `mcp_server.py:12949, 13001, 13128`" to name the three `run_progress.retired(...)` sites by function context, not by line number.

Add one short paragraph at the end of the "Graph Storage" section:

> **Ingestion status (#222 phase 4).** `processed` is gone. It was the graph's commit count at run start plus every position retired, so it passed `total` on any re-walk (28/20 measured) and read 20/20 on a run that had lost a commit. `ingest_progress.RunProgress` now reports `this_run` (work, `retired <= to_retire`), per-stream state and rate, `visibility` (what the frontier can prove, which can dip between runs) and `lineage`. **Done is `visibility.complete and lineage.complete`, never `status: complete` alone.** Residual: a failed FORWARD write is swallowed by frontier-low's range on the next forward claim (#326 left forward failure semantics out of scope), so the next run's `visibility` counts it. The failing run itself reports `complete: false`, and `skipped_commits` stays loud.

- [ ] **Step 5: Run the doc and schema guards and the full suite**

Run: `.venv/bin/python -m pytest tests/test_tool_schemas.py tests/test_skill_doc.py -q` then `.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -5`
Expected: PASS, and 0 failures.

- [ ] **Step 6: Closing-keyword scan and commit**

Commit with the message "Document ingestion status v2 (part of #222 phase 4)" and the trailer. Then run:

```bash
git log master..HEAD --format=%B | grep -inE "\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[^a-z]*#[0-9]+" || echo "clean"
```

Expected: `clean`.

---

## After all tasks

- Final whole-branch review: run the throwaway probe's six scenarios as a sanity check against the finished branch, and compare with the spec's measured table.
- Open the PR with "Part of #222" and no closing keyword. Verify `gh pr view --json closingIssuesReferences` is empty after creating it. Merge with `--merge` (not squash), after CI is green on 3.10–3.14.
