"""Pure metric-computation helpers for the at-scale benchmark tier (#120)."""

from __future__ import annotations


def latency_stats(samples: list[float]) -> dict[str, float]:
    """Return {min, p50, p99, max} for a list of latency samples (seconds).

    All-zero dict for an empty list. p50/p99 use nearest-rank on the sorted
    list — simple and sufficient for benchmark reporting, not a statistics
    library dependency.
    """
    if not samples:
        return {"min": 0.0, "p50": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(samples)
    n = len(ordered)

    def _percentile(p: float) -> float:
        idx = min(n - 1, int(round(p * (n - 1))))
        return ordered[idx]

    return {
        "min": ordered[0],
        "p50": _percentile(0.50),
        "p99": _percentile(0.99),
        "max": ordered[-1],
    }


def throughput_per_minute(count: int, elapsed_seconds: float) -> float:
    """Return count / (elapsed_seconds / 60), or 0.0 if elapsed_seconds <= 0."""
    if elapsed_seconds <= 0:
        return 0.0
    return count / (elapsed_seconds / 60.0)
