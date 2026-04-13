"""Proactive regime-based parameter scaling.

Adjusts stop distances and position sizes BEFORE trades based on
current ATR percentile relative to recent history. High volatility
widens stops and reduces size; low volatility does the opposite.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.indicators.core import OHLCV, compute_atr


@dataclass(frozen=True)
class RegimeScaling:
    """Multipliers to apply to stops and position sizes."""
    stop_multiplier: float
    size_multiplier: float
    atr_percentile: float

_MIN_STOP_MULT = 0.7
_MAX_STOP_MULT = 1.5
_MIN_SIZE_MULT = 0.4
_MAX_SIZE_MULT = 1.4


def compute_atr_percentile(
    candles: list[OHLCV],
    lookback: int = 90,
    atr_period: int = 14,
) -> Optional[float]:
    """Compute where current ATR sits relative to rolling history.

    Returns a percentile (0.0 = lowest vol, 1.0 = highest vol)
    or None if insufficient data.
    """
    min_candles = lookback + atr_period
    if len(candles) < min_candles:
        return None

    atr_values: list[float] = []
    for end in range(atr_period + 1, len(candles) + 1):
        window = candles[max(0, end - atr_period - 1):end]
        atr = compute_atr(window, atr_period)
        if atr is not None:
            atr_values.append(atr)

    if len(atr_values) < lookback:
        return None

    recent = atr_values[-lookback:]
    current_atr = recent[-1]
    count_below = sum(1 for v in recent if v <= current_atr)
    return count_below / len(recent)


def scale_for_regime(atr_percentile: float) -> RegimeScaling:
    """Compute scaling multipliers from ATR percentile.

    Linear interpolation:
    - percentile 0.0 (calm):   stop=0.8x, size=1.3x
    - percentile 0.5 (normal): stop=1.0x, size=1.0x
    - percentile 1.0 (wild):   stop=1.4x, size=0.5x
    """
    p = max(0.0, min(1.0, atr_percentile))

    # Piecewise linear: calm [0,0.5] -> [0.8,1.0], wild [0.5,1.0] -> [1.0,1.4]
    if p <= 0.5:
        stop_mult = 0.8 + 0.4 * p  # 0.8 at 0, 1.0 at 0.5
    else:
        stop_mult = 1.0 + 0.8 * (p - 0.5)  # 1.0 at 0.5, 1.4 at 1.0
    stop_mult = max(_MIN_STOP_MULT, min(_MAX_STOP_MULT, stop_mult))

    # Piecewise linear: calm [0,0.5] -> [1.3,1.0], wild [0.5,1.0] -> [1.0,0.5]
    if p <= 0.5:
        size_mult = 1.3 - 0.6 * p  # 1.3 at 0, 1.0 at 0.5
    else:
        size_mult = 1.0 - 1.0 * (p - 0.5)  # 1.0 at 0.5, 0.5 at 1.0
    size_mult = max(_MIN_SIZE_MULT, min(_MAX_SIZE_MULT, size_mult))

    return RegimeScaling(
        stop_multiplier=stop_mult,
        size_multiplier=size_mult,
        atr_percentile=p,
    )


def get_regime_scaling(symbol: str = "BTC") -> RegimeScaling:
    """Get current regime scaling for a symbol.

    Uses the candle buffer from indicators.core. Falls back to
    neutral scaling if insufficient data.
    """
    from src.indicators.core import get_candles
    candles = get_candles(symbol)
    pct = compute_atr_percentile(candles)
    if pct is None:
        return RegimeScaling(stop_multiplier=1.0, size_multiplier=1.0, atr_percentile=0.5)
    return scale_for_regime(pct)
