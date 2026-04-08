"""Adaptive stop-loss sizing based on historical MAE (Maximum Adverse Excursion).

Instead of using fixed percentage stops, compute stops from the actual MAE
distribution of winning trades per strategy. If 80% of winning trades for
momentum_swing had MAE > -2.5%, then -2.5% is a good initial stop.
"""

from src.storage.database import get_closed_trades
from src.utils.cache import TTLCache
from src.utils.safe_math import safe_score

_mae_cache: TTLCache[str, float] = TTLCache(ttl_s=3600)
_MIN_TRADES = 20
_PERCENTILE = 0.80


def compute_adaptive_stop(strategy: str, fallback_trail_pct: float) -> float:
    """Return the adaptive stop percentage for a strategy.

    Uses the 80th percentile of MAE from winning trades.
    Falls back to fallback_trail_pct if insufficient history.
    """
    cached = _mae_cache.get(strategy)
    if cached is not None:
        return cached

    trades = [
        t for t in get_closed_trades(500)
        if t.strategy == strategy and t.pnl_pct is not None and t.pnl_pct > 0
        and t.mae_pct is not None and t.mae_pct != 0
    ]

    if len(trades) < _MIN_TRADES:
        _mae_cache.set(strategy, fallback_trail_pct)
        return fallback_trail_pct

    mae_values = sorted([abs(t.mae_pct) for t in trades])

    idx = min(int(len(mae_values) * _PERCENTILE), len(mae_values) - 1)
    adaptive_pct = mae_values[idx]

    # Buffer so we don't stop at the exact historical boundary
    adaptive_pct *= 1.10
    adaptive_pct = safe_score(adaptive_pct, 0.01, 0.15)

    _mae_cache.set(strategy, adaptive_pct)
    return adaptive_pct
