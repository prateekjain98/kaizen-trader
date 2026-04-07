"""Safe math utilities to guard against NaN/Inf values."""

import math


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
