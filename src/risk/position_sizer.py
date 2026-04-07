"""Position sizing — Kelly Criterion + fixed fractional hybrid."""

import time
from dataclasses import dataclass
from typing import Optional

from src.config import env
from src.storage.database import get_closed_trades

_MIN_SIZE_USD = 10
_MIN_HISTORY = 10
_KELLY_FRACTION = 0.25


@dataclass
class StrategyStats:
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    sample_size: int


_stats_cache: dict[str, tuple[StrategyStats, float]] = {}
_STATS_TTL_MS = 600_000


def _compute_strategy_stats(strategy: str) -> StrategyStats:
    cached = _stats_cache.get(strategy)
    now = time.time() * 1000
    if cached and now - cached[1] < _STATS_TTL_MS:
        return cached[0]

    trades = [t for t in get_closed_trades(200) if t.strategy == strategy and t.pnl_pct is not None]

    if len(trades) < _MIN_HISTORY:
        stats = StrategyStats(win_rate=0.5, avg_win_pct=0.04, avg_loss_pct=0.03, sample_size=len(trades))
        _stats_cache[strategy] = (stats, now)
        return stats

    wins = [t for t in trades if (t.pnl_pct or 0) > 0]
    losses = [t for t in trades if (t.pnl_pct or 0) <= 0]

    win_rate = len(wins) / len(trades)
    avg_win_pct = sum(t.pnl_pct or 0 for t in wins) / len(wins) if wins else 0.04
    avg_loss_pct = abs(sum(t.pnl_pct or 0 for t in losses) / len(losses)) if losses else 0.03

    stats = StrategyStats(win_rate=win_rate, avg_win_pct=avg_win_pct, avg_loss_pct=avg_loss_pct, sample_size=len(trades))
    _stats_cache[strategy] = (stats, now)
    return stats


def kelly_size(strategy: str, portfolio_usd: float, qual_score: float) -> float:
    stats = _compute_strategy_stats(strategy)

    if stats.sample_size < _MIN_HISTORY:
        fraction = 0.01
    else:
        b = stats.avg_win_pct / stats.avg_loss_pct if stats.avg_loss_pct > 0 else 1
        p = stats.win_rate
        q = 1 - p
        raw_kelly = (b * p - q) / b
        if raw_kelly <= 0:
            return 0
        fraction = raw_kelly * _KELLY_FRACTION

    qual_multiplier = 0.5 + (qual_score / 100)
    raw_usd = fraction * portfolio_usd * qual_multiplier
    return max(_MIN_SIZE_USD, min(env.max_position_usd, raw_usd))


def log_kelly_rationale(strategy: str) -> str:
    stats = _compute_strategy_stats(strategy)
    if stats.sample_size < _MIN_HISTORY:
        return f"{strategy}: insufficient history ({stats.sample_size}/{_MIN_HISTORY}) — using 1% fixed-fractional"
    b = stats.avg_win_pct / stats.avg_loss_pct if stats.avg_loss_pct > 0 else 1
    p = stats.win_rate
    q = 1 - p
    raw_kelly = (b * p - q) / b
    return (f"{strategy}: win_rate={p*100:.0f}% avg_win={stats.avg_win_pct*100:.1f}% "
            f"avg_loss={stats.avg_loss_pct*100:.1f}% kelly={raw_kelly*100:.1f}% "
            f"-> quarter_kelly={raw_kelly*_KELLY_FRACTION*100:.2f}%")
