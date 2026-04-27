"""Cumulative Volume Delta (CVD) tracker for Binance Futures.

CVD = sum of buyer-initiated volume minus seller-initiated volume. The
direction of CVD relative to price is one of the cleanest 15min-1h reversal
signals in crypto:

  Price ↓ + CVD ↑  → bears exhausting, real buyers absorbing → bullish divergence
  Price ↑ + CVD ↓  → rally without real buying → bearish divergence
  Price ↓ + CVD ↓  → flush continuation, no edge for fade
  Price ↑ + CVD ↑  → trend continuation

Subscribes to Binance Futures `!aggTrade@arr`-equivalent (we sub per-symbol
since there's no all-symbol aggTrade combined stream — instead we route the
full `aggTrade` for any symbol the bot is interested in via dynamic subs).

For the funding-squeeze bot: CVD divergence at the entry moment is the
filter that distinguishes "real squeeze in progress" from "spoof signal".
"""

from __future__ import annotations

import collections
import json
import threading
import time
from typing import Optional

from src.engine.log import log


_BINANCE_WS = "wss://fstream.binance.com/stream"


class CVDTracker:
    """Per-symbol cumulative volume delta with rolling 1h history.

    Storage: per symbol, a deque of (timestamp, signed_qty_usd) tuples — the
    raw signed flow. CVD over a window is the sum. Pruned to 1h on each
    insert (deque maxlen is the safety net).

    A position open subscribes the symbol; close unsubscribes. Without an
    active subscription, we don't burn quota tracking trades for unrelated
    symbols.
    """

    _RETAIN_SECONDS = 3600
    _MAX_PER_SYMBOL = 50_000  # bound under burst load (deque maxlen safety net)

    def __init__(self):
        self._lock = threading.Lock()
        # symbol → deque of (ts, signed_usd) — buyer-initiated positive,
        # seller-initiated negative.
        self._flows: dict[str, collections.deque] = {}
        self._subbed: set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._status = "disconnected"

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cvd-tracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self, symbol: str) -> None:
        """Add a symbol to the WS subscription. Idempotent.
        Order matters: ensure the flows deque exists BEFORE marking as
        subscribed, so a concurrent _on_message can't see _subbed=true with
        no deque to write into."""
        sym = (symbol or "").upper()
        if not sym or sym in self._subbed:
            return
        with self._lock:
            self._flows.setdefault(sym, collections.deque(maxlen=self._MAX_PER_SYMBOL))
        binance_sym = f"{sym}USDT".lower()
        msg = {"method": "SUBSCRIBE", "params": [f"{binance_sym}@aggTrade"], "id": int(time.time())}
        self._send(msg)
        self._subbed.add(sym)

    def unsubscribe(self, symbol: str) -> None:
        """Remove a symbol from WS sub AND clear its flow history.
        Stale history on re-open is a real signal contamination risk: a
        previous position's exhaustive sell flow would otherwise contaminate
        the new entry's CVD reading. Clean wipe is safer than carrying it."""
        sym = (symbol or "").upper()
        if sym not in self._subbed:
            return
        binance_sym = f"{sym}USDT".lower()
        msg = {"method": "UNSUBSCRIBE", "params": [f"{binance_sym}@aggTrade"], "id": int(time.time())}
        self._send(msg)
        self._subbed.discard(sym)
        # Clear flow history so a re-sub starts with a fresh slate. Without
        # this, the previous position's CVD imbalance contaminates the new
        # signal — review caught this as a P1 false-signal risk.
        with self._lock:
            self._flows.pop(sym, None)

    def cvd(self, symbol: str, window_seconds: int = 900) -> Optional[float]:
        """Cumulative volume delta in USD over the last `window_seconds`.
        Returns None if no data (subscribe just landed) so callers can skip."""
        sym = (symbol or "").upper()
        cutoff = time.time() - window_seconds
        with self._lock:
            d = self._flows.get(sym)
            if not d:
                return None
            return sum(usd for ts, usd in d if ts >= cutoff)

    def divergence_signal(self, symbol: str, price_delta_pct: float,
                           window_seconds: int = 900) -> Optional[str]:
        """Compare CVD direction to a caller-provided price delta over the
        same window. Returns:
          'bullish_divergence' — price down, CVD up (fade short, take long)
          'bearish_divergence' — price up, CVD down (fade long, take short)
          'continuation'       — both move same direction
          None                 — insufficient data, or no clear signal

        IMPORTANT: `price_delta_pct` MUST be measured over the same
        `window_seconds` interval as the CVD lookup or the comparison is
        meaningless. Caller is responsible — if you pass a 1h price delta
        with a 15min CVD, you're comparing oranges to apples and the
        result is garbage. (TODO: take prices, not delta, so we can slice
        the window ourselves.)
        """
        cvd = self.cvd(symbol, window_seconds)
        if cvd is None:
            return None
        # Threshold: avoid noise. Need both moves to be meaningful.
        if abs(price_delta_pct) < 0.005:  # < 0.5%
            return None
        if abs(cvd) < 10_000:  # < $10k flow imbalance
            return None
        if price_delta_pct < 0 and cvd > 0:
            return "bullish_divergence"
        if price_delta_pct > 0 and cvd < 0:
            return "bearish_divergence"
        return "continuation"

    @property
    def status(self) -> str:
        return self._status

    @property
    def subscribed_symbols(self) -> frozenset:
        return frozenset(self._subbed)

    # ── WS internals ────────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            log("warn", f"cvd-tracker send failed: {e}")

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            log("error", "cvd-tracker: websocket-client not installed")
            return

        while not self._stop.is_set():
            try:
                # Connect with no streams; subscribe dynamically via _send.
                ws = websocket.WebSocketApp(
                    _BINANCE_WS,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log("warn", f"cvd-tracker WS error: {e}")
            self._status = "disconnected"
            self._ws = None
            if self._stop.is_set():
                break
            log("info", "cvd-tracker reconnecting in 5s...")
            self._stop.wait(timeout=5)

    def _on_open(self, ws):
        self._status = "connected"
        log("info", "CVD tracker WS connected")
        # Re-subscribe everything after a reconnect.
        for sym in list(self._subbed):
            binance_sym = f"{sym}USDT".lower()
            self._send({"method": "SUBSCRIBE",
                        "params": [f"{binance_sym}@aggTrade"],
                        "id": int(time.time())})

    def _on_close(self, ws, code, msg):
        self._status = "disconnected"

    def _on_error(self, ws, error):
        log("warn", f"cvd-tracker WS error: {error}")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "stream" not in data:
                return  # subscription ack, ignore
            payload = data.get("data") or {}
            sym_raw = payload.get("s", "")
            if not sym_raw.endswith("USDT"):
                return
            sym = sym_raw.replace("USDT", "")
            qty = float(payload.get("q", 0))
            price = float(payload.get("p", 0))
            usd = qty * price
            if usd <= 0:
                return
            # Binance aggTrade 'm' field: True if buyer is the market maker
            # (passive), i.e. a SELL hit the bid → seller-initiated.
            # False → BUY lifted the ask → buyer-initiated.
            is_buyer_maker = bool(payload.get("m", False))
            signed = -usd if is_buyer_maker else usd
            ts = time.time()
            with self._lock:
                d = self._flows.get(sym)
                if d is None:
                    # We may receive a few trades after unsubscribe; ignore.
                    return
                d.append((ts, signed))
                cutoff = ts - self._RETAIN_SECONDS
                while d and d[0][0] < cutoff:
                    d.popleft()
        except Exception:
            pass


# Process-wide singleton, double-checked-locked.
_tracker: Optional[CVDTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> CVDTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = CVDTracker()
    return _tracker
