"""Track 1h price acceleration for ALL symbols using WebSocket data.

Replaces the slow REST kline fetching (20 symbols, 10+ seconds) with
real-time computation from the Binance WS price stream (700+ symbols, 0 API calls).

Stores one price snapshot per minute per symbol in a ring buffer.
Computes 1h change by comparing current price to the snapshot ~60 minutes ago.
Memory: ~700 symbols x 120 entries x 16 bytes = ~1.3MB.
"""

from __future__ import annotations

import time
from collections import deque


class AccelerationTracker:
    """Real-time 1h price acceleration from WebSocket ticks."""

    # Ring buffer holds ~2h of 1-min snapshots (extra headroom for clock drift)
    _MAX_SNAPSHOTS = 120
    # Only record one snapshot per 60s per symbol
    _SNAP_INTERVAL_S = 60.0
    # Need at least ~55 min of data to compute a meaningful 1h change
    _MIN_HISTORY_S = 55 * 60
    # Target lookback for 1h change
    _TARGET_LOOKBACK_S = 60 * 60

    def __init__(self) -> None:
        # {symbol: deque of (timestamp_s, price)}
        self._snapshots: dict[str, deque[tuple[float, float]]] = {}
        # {symbol: last snapshot timestamp} -- throttle writes
        self._last_snap_ts: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def update(self, symbol: str, price: float, ts: float | None = None) -> None:
        """Record a price tick. Called from the WebSocket stream.

        Throttles to at most one snapshot per 60s per symbol.
        """
        now = ts if ts is not None else time.time()

        last = self._last_snap_ts.get(symbol, 0.0)
        if now - last < self._SNAP_INTERVAL_S:
            return

        if symbol not in self._snapshots:
            self._snapshots[symbol] = deque(maxlen=self._MAX_SNAPSHOTS)

        self._snapshots[symbol].append((now, price))
        self._last_snap_ts[symbol] = now

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_1h_change(self, symbol: str) -> float | None:
        """Return the approximate 1h percentage change for *symbol*.

        Finds the snapshot closest to 60 min ago and computes:
            (current - old) / old * 100

        Returns None if there is not enough history.
        """
        buf = self._snapshots.get(symbol)
        if not buf or len(buf) < 2:
            return None

        now_ts, now_price = buf[-1]
        target_ts = now_ts - self._TARGET_LOOKBACK_S

        # Not enough history yet
        oldest_ts = buf[0][0]
        if now_ts - oldest_ts < self._MIN_HISTORY_S:
            return None

        # Linear scan (small buffer, <=120 entries) to find closest to target
        best_snap: tuple[float, float] | None = None
        best_diff = float("inf")
        for snap_ts, snap_price in buf:
            diff = abs(snap_ts - target_ts)
            if diff < best_diff:
                best_diff = diff
                best_snap = (snap_ts, snap_price)
            # Once we pass the target, further entries are farther away
            if snap_ts > target_ts:
                break

        if best_snap is None or best_snap[1] == 0:
            return None

        return (now_price - best_snap[1]) / best_snap[1] * 100.0

    def get_all_accelerations(self, min_abs_pct: float = 2.0) -> dict[str, float]:
        """Return {symbol: pct_change} for all symbols exceeding the threshold."""
        result: dict[str, float] = {}
        for symbol in self._snapshots:
            change = self.get_1h_change(symbol)
            if change is not None and abs(change) >= min_abs_pct:
                result[symbol] = change
        return result

    def get_top_accelerators(self, n: int = 20) -> list[tuple[str, float]]:
        """Return the top *n* symbols ranked by absolute 1h acceleration."""
        all_changes: list[tuple[str, float]] = []
        for symbol in self._snapshots:
            change = self.get_1h_change(symbol)
            if change is not None:
                all_changes.append((symbol, change))

        all_changes.sort(key=lambda x: abs(x[1]), reverse=True)
        return all_changes[:n]
