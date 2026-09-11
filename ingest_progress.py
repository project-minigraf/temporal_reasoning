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
