"""Safe math utilities to guard against NaN/Inf values."""

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


def safe_score(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a value to [lo, hi], returning lo if NaN or Inf."""
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, value))


def safe_ratio(value: float) -> float:
    """Return 0.0 if value is NaN or Inf, otherwise return the value unchanged."""
    if not math.isfinite(value):
        return 0.0
    return value


def _mean_std(values) -> tuple[float, float]:
    """Population mean and std-dev. Caller must ensure values is non-empty."""
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(variance)


# ─── Rolling Z-Score ──────────────────────────────────────────────────────────

@dataclass
class RollingZScore:
    """Incrementally compute z-scores over a rolling window.

    Maintains a deque of values and computes mean/std on demand.
    Use one instance per (symbol, metric) pair.
    """
    window: int = 100
    _values: deque = field(init=False)

    def __post_init__(self):
        self._values = deque(maxlen=self.window)

    def push(self, value: float) -> None:
        if math.isfinite(value):
            self._values.append(value)

    def zscore(self, value: Optional[float] = None) -> float:
        """Compute z-score for the given value (or latest pushed value).

        Returns 0.0 if insufficient data (<10 samples) or zero std dev.
        """
        if len(self._values) < 10:
            return 0.0
        if value is None:
            if not self._values:
                return 0.0
            value = self._values[-1]
        if not math.isfinite(value):
            return 0.0

        mean, std = _mean_std(self._values)
        if std < 1e-12:
            return 0.0
        return safe_ratio((value - mean) / std)

    @property
    def mean(self) -> float:
        if not self._values:
            return 0.0
        return sum(self._values) / len(self._values)

    @property
    def std(self) -> float:
        if len(self._values) < 2:
            return 0.0
        return _mean_std(self._values)[1]

    @property
    def count(self) -> int:
        return len(self._values)


def compute_zscore(values: list[float], current: float) -> float:
    """One-shot z-score computation from a list of historical values.

    Returns 0.0 if insufficient data or zero std dev.
    """
    finite_vals = [v for v in values if math.isfinite(v)]
    if len(finite_vals) < 10:
        return 0.0
    mean, std = _mean_std(finite_vals)
    if std < 1e-12:
        return 0.0
    return safe_ratio((current - mean) / std)
