"""Real-time liquidation tracker for Binance Futures.

Subscribes to `!forceOrder@arr` (all-symbol liquidation feed) and maintains
rolling per-symbol $-notional sums for short-window queries. Squeeze cascades
are the leading indicator for 5-30min directional moves — when $5M+ of
shorts get liquidated in 5min, longs ride the follow-through.

Per the data-source audit: this is the #1 priority addition for a funding-
squeeze bot. Free, sub-second latency.

Public API:
    LiquidationTracker.start()
    LiquidationTracker.recent_liquidations(symbol, side, window_seconds)
        -> total $-notional liquidated in `side` ('long' or 'short') over
           the last `window_seconds`
    LiquidationTracker.cascade_score(symbol)
        -> heuristic 0-100 indicating how 'fresh' a squeeze cascade is
"""

from __future__ import annotations

import collections
import json
import threading
import time
from typing import Optional

from src.engine.log import log

_BINANCE_WS = "wss://fstream.binance.com/stream?streams=!forceOrder@arr"

# Binance prefixes scaled-supply tokens with "1000" so the contract notional
# is reasonable. Allowlist instead of prefix-strip so a future ticker that
# happens to start with "1000" doesn't get silently mis-keyed.
_BINANCE_1000_PREFIX = frozenset({
    "1000SHIB", "1000LUNC", "1000PEPE", "1000FLOKI",
    "1000BONK", "1000SATS", "1000RATS", "1000XEC",
    "1000CHEEMS", "1000WHY", "1000CAT",
})


class LiquidationTracker:
    """Maintains rolling per-symbol liquidation $-notional history.

    Memory model: per symbol, two deques (longs liquidated, shorts liquidated)
    with (timestamp, usd_notional) tuples. Pruned to the last 1h on every
    insert to keep memory bounded across hundreds of symbols.
    """

    _RETAIN_SECONDS = 3600  # keep 1h of history per symbol

    def __init__(self):
        self._lock = threading.Lock()
        # symbol → deque of (ts, usd) — long-side liquidations (i.e. longs got liquidated)
        self._long_liqs: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._short_liqs: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status = "disconnected"
        self._total_events = 0

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="liq-tracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def recent_liquidations(self, symbol: str, side: str, window_seconds: int = 300) -> float:
        """Total $-notional of `side` liquidations on `symbol` in the last
        `window_seconds`. side='long' means longs were liquidated (price
        dropped sharply); side='short' means shorts were liquidated (price
        spiked, the squeeze setup we're trying to ride).

        ALL deque access is inside the lock — the WS thread can call popleft
        concurrently and `sum(... for x in d ...)` is not atomic vs that."""
        cutoff = time.time() - window_seconds
        sym = (symbol or "").upper()
        with self._lock:
            if side == "long":
                d = self._long_liqs.get(sym)
            elif side == "short":
                d = self._short_liqs.get(sym)
            else:
                return 0.0
            if not d:
                return 0.0
            return sum(usd for ts, usd in d if ts >= cutoff)

    def all_active_symbols(self) -> list[str]:
        """All symbols with any liquidation in our retained history.
        Used by the cascade poller to know which symbols to scan without
        re-walking every Binance perp every tick."""
        with self._lock:
            return list(set(self._long_liqs.keys()) | set(self._short_liqs.keys()))

    def largest_single_in_window(self, symbol: str, window_seconds: int = 300) -> float:
        """Largest single liquidation $-notional on `symbol` in the window
        (across both sides). Useful for outlier detection."""
        cutoff = time.time() - window_seconds
        sym = (symbol or "").upper()
        with self._lock:
            longs = self._long_liqs.get(sym) or ()
            shorts = self._short_liqs.get(sym) or ()
            biggest = 0.0
            for ts, usd in longs:
                if ts >= cutoff and usd > biggest:
                    biggest = usd
            for ts, usd in shorts:
                if ts >= cutoff and usd > biggest:
                    biggest = usd
            return biggest

    def liquidation_count(self, symbol: str, window_seconds: int = 300) -> int:
        """Count of liquidations on `symbol` (both sides) in the window."""
        cutoff = time.time() - window_seconds
        sym = (symbol or "").upper()
        with self._lock:
            longs = self._long_liqs.get(sym) or ()
            shorts = self._short_liqs.get(sym) or ()
            return sum(1 for ts, _ in longs if ts >= cutoff) + \
                   sum(1 for ts, _ in shorts if ts >= cutoff)

    def cascade_score(self, symbol: str, window_seconds: int = 300) -> dict:
        """Return a per-direction summary suitable for entry filters.
        {
          'long_liq_usd_5m': float,   # longs liquidated → bearish cascade
          'short_liq_usd_5m': float,  # shorts liquidated → bullish cascade (squeeze)
          'dominant_side': 'long' | 'short' | None,
          'imbalance_ratio': float,   # max/min, ≥1
        }"""
        long_liq = self.recent_liquidations(symbol, "long", window_seconds)
        short_liq = self.recent_liquidations(symbol, "short", window_seconds)
        dominant = None
        ratio = 1.0
        if long_liq > 0 or short_liq > 0:
            if long_liq > short_liq:
                dominant = "long"
                ratio = long_liq / max(short_liq, 1.0)
            else:
                dominant = "short"
                ratio = short_liq / max(long_liq, 1.0)
        return {
            "long_liq_usd_5m": long_liq,
            "short_liq_usd_5m": short_liq,
            "dominant_side": dominant,
            "imbalance_ratio": ratio,
        }

    # ── WS internals ────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            log("error", "liq-tracker: websocket-client not installed")
            return

        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    _BINANCE_WS,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log("warn", f"liq-tracker WS error: {e}")
            self._status = "disconnected"
            if self._stop.is_set():
                break
            log("info", "liq-tracker reconnecting in 5s...")
            self._stop.wait(timeout=5)

    def _on_open(self, ws):
        self._status = "connected"
        log("info", "Liquidation tracker WS connected (!forceOrder@arr)")

    def _on_close(self, ws, code, msg):
        self._status = "disconnected"

    def _on_error(self, ws, error):
        log("warn", f"liq-tracker WS error: {error}")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            payload = data.get("data") or {}
            order = payload.get("o") or {}
            sym_raw = order.get("s", "")
            if not sym_raw.endswith("USDT"):
                return
            sym = sym_raw.replace("USDT", "")
            if sym in _BINANCE_1000_PREFIX:
                sym = sym[4:]

            # Liquidation order side: 'BUY' means the SHORT was liquidated (forced
            # buy-to-close), 'SELL' means the LONG was liquidated.
            side_raw = order.get("S", "")
            qty = float(order.get("q", 0))
            avg_price = float(order.get("ap") or order.get("p", 0))
            usd = qty * avg_price
            if usd <= 0:
                return
            ts = time.time()

            with self._lock:
                if side_raw == "SELL":
                    # Long was liquidated
                    bucket = self._long_liqs[sym]
                else:
                    # Short was liquidated
                    bucket = self._short_liqs[sym]
                bucket.append((ts, usd))
                # Prune old entries — cheap because deque is sorted by ts.
                cutoff = ts - self._RETAIN_SECONDS
                while bucket and bucket[0][0] < cutoff:
                    bucket.popleft()

                self._total_events += 1
        except Exception:
            pass

    @property
    def status(self) -> str:
        return self._status

    @property
    def total_events(self) -> int:
        return self._total_events


# Process-wide singleton — initialized lazily by runner so the tracker only
# spins up when the engine is actually running. Double-checked locking so
# concurrent get_tracker() calls don't spawn zombie WS threads.
_tracker: Optional[LiquidationTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> LiquidationTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = LiquidationTracker()
    return _tracker
