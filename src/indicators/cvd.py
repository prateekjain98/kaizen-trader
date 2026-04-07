"""Cumulative Volume Delta — tracks aggressive buy vs sell volume.

CVD divergence from price is one of the most reliable short-term reversal signals.
Built from the Coinbase WS trade tape (ticker messages include side).
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.utils.safe_math import safe_ratio


@dataclass
class CVDSnapshot:
    """CVD state for a symbol at a point in time."""
    symbol: str
    ts: float
    cvd: float                      # cumulative volume delta
    cvd_1m: float = 0.0            # CVD over last 1 minute
    cvd_5m: float = 0.0            # CVD over last 5 minutes
    cvd_15m: float = 0.0           # CVD over last 15 minutes
    buy_volume_1m: float = 0.0     # aggressive buy volume last 1 min
    sell_volume_1m: float = 0.0    # aggressive sell volume last 1 min
    divergence_score: float = 0.0  # CVD vs price divergence (-1 to +1)


@dataclass
class _VolumeTick:
    ts: float
    delta: float  # positive = buy, negative = sell
    price: float


_MAX_SYMBOLS = 500
_MAX_TICKS = 1000  # ~15 minutes at high activity
_lock = threading.Lock()
_tick_buffers: dict[str, list[_VolumeTick]] = {}
_snapshot_cache: dict[str, tuple[CVDSnapshot, float]] = {}
_SNAPSHOT_TTL_MS = 2_000


def push_trade(symbol: str, price: float, size: float, side: str) -> None:
    """Record an individual trade from the WS feed.

    Args:
        symbol: e.g. "BTC"
        price: trade price
        size: trade size in base currency
        side: "buy" or "sell" — taker side
    """
    now = time.time() * 1000
    delta = size if side == "buy" else -size

    with _lock:
        if symbol not in _tick_buffers and len(_tick_buffers) >= _MAX_SYMBOLS:
            oldest = min(_tick_buffers, key=lambda k: _tick_buffers[k][-1].ts if _tick_buffers[k] else 0)
            del _tick_buffers[oldest]
            _snapshot_cache.pop(oldest, None)

        buf = _tick_buffers.setdefault(symbol, [])
        buf.append(_VolumeTick(ts=now, delta=delta, price=price))
        if len(buf) > _MAX_TICKS:
            buf.pop(0)


def get_cvd_snapshot(symbol: str) -> Optional[CVDSnapshot]:
    """Get CVD snapshot with multi-window aggregation."""
    now = time.time() * 1000

    with _lock:
        cached = _snapshot_cache.get(symbol)
        if cached and (now - cached[1]) < _SNAPSHOT_TTL_MS:
            return cached[0]
        buf = list(_tick_buffers.get(symbol, []))

    if len(buf) < 5:
        return None

    # Compute windowed CVDs
    cvd_total = 0.0
    cvd_1m = 0.0
    cvd_5m = 0.0
    cvd_15m = 0.0
    buy_vol_1m = 0.0
    sell_vol_1m = 0.0

    cutoff_1m = now - 60_000
    cutoff_5m = now - 300_000
    cutoff_15m = now - 900_000

    for tick in buf:
        cvd_total += tick.delta
        if tick.ts >= cutoff_15m:
            cvd_15m += tick.delta
        if tick.ts >= cutoff_5m:
            cvd_5m += tick.delta
        if tick.ts >= cutoff_1m:
            cvd_1m += tick.delta
            if tick.delta > 0:
                buy_vol_1m += tick.delta
            else:
                sell_vol_1m += abs(tick.delta)

    # Compute price-CVD divergence
    # Compare price direction (first vs last) with CVD direction
    divergence = _compute_divergence(buf, cvd_5m)

    snap = CVDSnapshot(
        symbol=symbol,
        ts=now,
        cvd=safe_ratio(cvd_total),
        cvd_1m=safe_ratio(cvd_1m),
        cvd_5m=safe_ratio(cvd_5m),
        cvd_15m=safe_ratio(cvd_15m),
        buy_volume_1m=buy_vol_1m,
        sell_volume_1m=sell_vol_1m,
        divergence_score=divergence,
    )

    with _lock:
        _snapshot_cache[symbol] = (snap, now)

    return snap


def _compute_divergence(ticks: list[_VolumeTick], cvd_5m: float) -> float:
    """Compute price-CVD divergence score.

    Returns a value from -1 to +1:
    - Positive: price rising but CVD falling (bearish divergence)
    - Negative: price falling but CVD rising (bullish divergence)
    - Near zero: price and CVD aligned (no divergence)
    """
    if len(ticks) < 10:
        return 0.0

    cutoff = ticks[-1].ts - 300_000  # last 5 minutes
    recent = [t for t in ticks if t.ts >= cutoff]
    if len(recent) < 5:
        return 0.0

    price_start = recent[0].price
    price_end = recent[-1].price
    if price_start == 0:
        return 0.0

    price_change_pct = (price_end - price_start) / price_start

    # Normalize CVD to a -1 to +1 range using total volume
    total_vol = sum(abs(t.delta) for t in recent)
    if total_vol == 0:
        return 0.0
    cvd_normalized = cvd_5m / total_vol  # -1 to +1

    # Divergence = price direction minus CVD direction
    # If price goes up (+) but CVD goes down (-), divergence is positive (bearish)
    price_dir = max(-1.0, min(1.0, price_change_pct * 100))  # scale up small %
    divergence = price_dir - cvd_normalized

    return max(-1.0, min(1.0, divergence / 2))


def get_cvd(symbol: str) -> Optional[float]:
    """Get the raw CVD value for a symbol."""
    snap = get_cvd_snapshot(symbol)
    return snap.cvd if snap else None


def get_buy_sell_ratio(symbol: str) -> Optional[float]:
    """Get the 1-minute buy/sell volume ratio. >1 = buy pressure, <1 = sell pressure."""
    snap = get_cvd_snapshot(symbol)
    if not snap or snap.sell_volume_1m == 0:
        return None
    return safe_ratio(snap.buy_volume_1m / snap.sell_volume_1m)
