"""Track win rate and performance by hour-of-day per strategy.

Computes hourly performance from closed trade history, enabling
the qualification scorer to boost/penalize signals based on when
a strategy historically performs best.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.storage.database import get_closed_trades
from src.utils.cache import TTLCache
from src.utils.safe_math import safe_score


@dataclass
class HourlyBucket:
    hour: int  # 0-23 UTC
    trades: int = 0
    wins: int = 0
    total_pnl_pct: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def avg_pnl_pct(self) -> float:
        return self.total_pnl_pct / self.trades if self.trades > 0 else 0.0


_cache: TTLCache[str, list[HourlyBucket]] = TTLCache(ttl_s=1800)
_MIN_TRADES_PER_HOUR = 3


def get_hourly_stats(strategy: str) -> list[HourlyBucket]:
    """Get 24 hourly performance buckets for a strategy."""
    cached = _cache.get(strategy)
    if cached is not None:
        return cached

    buckets = [HourlyBucket(hour=h) for h in range(24)]

    trades = [
        t for t in get_closed_trades(500)
        if t.strategy == strategy and t.pnl_pct is not None and t.opened_at
    ]

    for t in trades:
        hour = datetime.fromtimestamp(t.opened_at / 1000, tz=timezone.utc).hour
        buckets[hour].trades += 1
        buckets[hour].total_pnl_pct += t.pnl_pct
        if t.pnl_pct > 0:
            buckets[hour].wins += 1

    _cache.set(strategy, buckets)
    return buckets


def get_hour_performance(strategy: str, hour: Optional[int] = None) -> Optional[HourlyBucket]:
    """Get performance for a specific hour (default: current UTC hour)."""
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    buckets = get_hourly_stats(strategy)
    bucket = buckets[hour]
    if bucket.trades < _MIN_TRADES_PER_HOUR:
        return None
    return bucket


def get_hour_adjustment(strategy: str, hour: Optional[int] = None) -> float:
    """Return a score adjustment (-10 to +10) based on hourly performance."""
    bucket = get_hour_performance(strategy, hour)
    if bucket is None:
        return 0.0

    win_delta = bucket.win_rate - 0.50
    adjustment = win_delta * 50
    return safe_score(adjustment, -10.0, 10.0)
