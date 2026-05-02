"""Filtered Order-Book Imbalance (OBI-F) tracker.

Per arXiv 2507.22712 (Jul 2025): top-of-book L2 imbalance, after FILTERING
transient/spoof orders that appear and disappear within ~3 snapshots,
is a small but persistent retail edge — best used as a mean-reversion
trigger when the persistent imbalance runs OPPOSITE the prevailing 1h
trend (i.e. the book is loaded for the snap-back).

Subscribes to Binance Futures `<symbol>@depth20@100ms` for a small set
of ACTIVE symbols (held positions + top trending). The full L2 stream
is high-bandwidth — the universe-wide miniTicker stream is fine but
depth20 across hundreds of symbols would saturate; we deliberately
restrict to <= ~12 active symbols at a time.

Snapshot cadence: we throttle to one stored snapshot per ~1s (10 snaps
of history rolled). Spoof filter: any price level whose presence is
NOT continuous across the last 3 snapshots is dropped from the OBI
computation. obi_f = (bid5 - ask5) / (bid5 + ask5), normalized to
[-1, 1]. A 5-min EMA smooths the per-tick value.

Thread-safe: WS callback thread writes under `_lock`; reader threads
(brain tick) use snapshot copies under the same lock.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Optional

from src.engine.log import log

_BINANCE_STREAM_BASE = "wss://fstream.binance.com/stream"

# Tunables
_HISTORY_SIZE = 10           # last 10 snapshots (~1s apart) → 10s window for spoof filter
_SNAP_INTERVAL_S = 1.0       # one stored snapshot per second
_SPOOF_WINDOW = 3            # drop levels not present in 3 consecutive snapshots
_TOP_N_LEVELS = 5            # top-5 bid vs top-5 ask
_EMA_WINDOW_S = 300.0        # 5-min EMA half-life proxy
_MAX_ACTIVE_SYMBOLS = 12     # bandwidth cap


class OrderBookTracker:
    """Track L2 depth + compute filtered OBI per symbol.

    Public reader API (called from brain thread):
        obi_f(symbol) -> float | None      latest filtered OBI in [-1, 1]
        obi_f_ema(symbol) -> float | None  5-min EMA of obi_f
        active_symbols() -> set[str]
        snapshot_count(symbol) -> int

    Mutating API (called from runner / data_streams):
        set_active_symbols(symbols)        diffs sub/unsub on the WS connection
        start() / stop()
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # symbol -> deque[ list[(price, qty)] ] (bids, descending; asks, ascending)
        self._bid_snaps: dict[str, deque[list[tuple[float, float]]]] = {}
        self._ask_snaps: dict[str, deque[list[tuple[float, float]]]] = {}
        # symbol -> last raw book (overwritten on every WS message)
        self._latest_bids: dict[str, list[tuple[float, float]]] = {}
        self._latest_asks: dict[str, list[tuple[float, float]]] = {}
        self._last_snap_ts: dict[str, float] = {}
        # symbol -> latest computed obi_f and its EMA
        self._obi: dict[str, float] = {}
        self._obi_ema: dict[str, float] = {}
        self._last_ema_ts: dict[str, float] = {}

        self._active: set[str] = set()
        self._stop = threading.Event()
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._status = "disconnected"
        self._msg_count = 0

    # ── Public reader API ──────────────────────────────────────────────

    def obi_f(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._obi.get(symbol)

    def obi_f_ema(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._obi_ema.get(symbol)

    def active_symbols(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def snapshot_count(self, symbol: str) -> int:
        with self._lock:
            return len(self._bid_snaps.get(symbol, ()))

    @property
    def status(self) -> str:
        return self._status

    @property
    def message_count(self) -> int:
        return self._msg_count

    # ── Active-symbol management ───────────────────────────────────────

    def set_active_symbols(self, symbols: set[str]) -> None:
        """Diff and rotate active symbol set (capped at _MAX_ACTIVE_SYMBOLS).

        On change we tear down + reconnect — Binance combined-stream sub
        management is fiddly and the rotation cadence (positions/trending
        churn) is on the order of minutes, so a reconnect is cheap.
        """
        wanted = {s.upper() for s in symbols if s}
        if len(wanted) > _MAX_ACTIVE_SYMBOLS:
            wanted = set(list(wanted)[:_MAX_ACTIVE_SYMBOLS])
        with self._lock:
            if wanted == self._active:
                return
            self._active = wanted
            # Drop history for symbols no longer tracked
            for sym in list(self._bid_snaps.keys()):
                if sym not in wanted:
                    self._bid_snaps.pop(sym, None)
                    self._ask_snaps.pop(sym, None)
                    self._latest_bids.pop(sym, None)
                    self._latest_asks.pop(sym, None)
                    self._obi.pop(sym, None)
                    self._obi_ema.pop(sym, None)
        # Force the WS loop to pick up new subs
        self._reconnect()

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ob-tracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.keep_running = False
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._status = "disconnected"

    def _reconnect(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    # ── WS loop ────────────────────────────────────────────────────────

    def _stream_url(self, symbols: set[str]) -> str:
        # Binance Futures wants lowercase pairs with USDT suffix.
        streams = "/".join(f"{s.lower()}usdt@depth20@100ms" for s in symbols)
        return f"{_BINANCE_STREAM_BASE}?streams={streams}"

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            log("warn", "[ob-tracker] websocket-client not installed — OBI-F disabled")
            return

        while not self._stop.is_set():
            with self._lock:
                syms = set(self._active)
            if not syms:
                self._stop.wait(timeout=2.0)
                continue
            try:
                url = self._stream_url(syms)
                ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=lambda *_: None,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log("warn", f"[ob-tracker] WS error: {e}")
            self._status = "disconnected"
            if self._stop.is_set():
                break
            self._stop.wait(timeout=3.0)

    def _on_open(self, ws):
        self._status = "connected"
        log("info", f"[ob-tracker] WS connected for {len(self._active)} symbols")

    def _on_close(self, ws, *args):
        self._status = "disconnected"

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        stream = data.get("stream", "")
        payload = data.get("data") or {}
        if not stream or not payload:
            return
        # stream like "btcusdt@depth20@100ms"
        pair = stream.split("@", 1)[0].upper()
        if not pair.endswith("USDT"):
            return
        symbol = pair[:-4]
        bids = payload.get("b") or payload.get("bids") or []
        asks = payload.get("a") or payload.get("asks") or []
        try:
            bids_p = [(float(p), float(q)) for p, q, *_ in bids if float(q) > 0]
            asks_p = [(float(p), float(q)) for p, q, *_ in asks if float(q) > 0]
        except (ValueError, TypeError):
            return
        self.ingest(symbol, bids_p, asks_p)
        self._msg_count += 1

    # ── Core compute (also used by tests directly) ─────────────────────

    def ingest(
        self,
        symbol: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        ts: Optional[float] = None,
    ) -> None:
        """Record latest book; throttle stored snapshots to ~1/s and recompute OBI.

        This is the only entry point that mutates per-symbol history; the
        spoof filter and OBI computation run inline so a reader sees a
        consistent (filtered_value, ema) pair under the lock.
        """
        now = ts if ts is not None else time.time()
        # Defensive: keep books sorted in canonical order
        bids = sorted(bids, key=lambda x: x[0], reverse=True)
        asks = sorted(asks, key=lambda x: x[0])

        with self._lock:
            self._latest_bids[symbol] = bids
            self._latest_asks[symbol] = asks

            last_snap = self._last_snap_ts.get(symbol, 0.0)
            if now - last_snap < _SNAP_INTERVAL_S:
                return
            self._last_snap_ts[symbol] = now

            bid_hist = self._bid_snaps.setdefault(symbol, deque(maxlen=_HISTORY_SIZE))
            ask_hist = self._ask_snaps.setdefault(symbol, deque(maxlen=_HISTORY_SIZE))
            bid_hist.append(bids[: max(_TOP_N_LEVELS * 4, 20)])
            ask_hist.append(asks[: max(_TOP_N_LEVELS * 4, 20)])

            obi = self._compute_obi_locked(bid_hist, ask_hist)
            if obi is None:
                return
            self._obi[symbol] = obi

            # 5-min EMA. alpha = dt / (window + dt) gives a time-aware EMA
            # that decays on its own clock instead of per-update — gaps in
            # the WS feed don't make the EMA snap to the new value.
            prev = self._obi_ema.get(symbol)
            last_ema_ts = self._last_ema_ts.get(symbol, now)
            dt = max(1e-3, now - last_ema_ts)
            alpha = min(1.0, dt / (_EMA_WINDOW_S + dt))
            self._obi_ema[symbol] = obi if prev is None else (prev + alpha * (obi - prev))
            self._last_ema_ts[symbol] = now

    @staticmethod
    def _compute_obi_locked(
        bid_hist: deque,
        ask_hist: deque,
    ) -> Optional[float]:
        """Compute filtered top-N OBI on the latest snapshot.

        A price level survives the filter iff it appears in the last
        _SPOOF_WINDOW snapshots without a gap. This rejects the classic
        spoof pattern where a level pops in for one tick to fake depth.
        """
        if not bid_hist or not ask_hist:
            return None
        if len(bid_hist) < _SPOOF_WINDOW or len(ask_hist) < _SPOOF_WINDOW:
            # Not enough history to filter spoofs — return None so callers
            # know we don't have a valid signal yet.
            return None

        latest_bids = bid_hist[-1]
        latest_asks = ask_hist[-1]

        recent_bid_snaps = list(bid_hist)[-_SPOOF_WINDOW:]
        recent_ask_snaps = list(ask_hist)[-_SPOOF_WINDOW:]

        def _persistent(price: float, snaps: list[list[tuple[float, float]]]) -> bool:
            for snap in snaps:
                if not any(abs(p - price) < 1e-12 for p, _ in snap):
                    return False
            return True

        bid_size = 0.0
        for price, qty in latest_bids[:_TOP_N_LEVELS]:
            if _persistent(price, recent_bid_snaps):
                bid_size += qty
        ask_size = 0.0
        for price, qty in latest_asks[:_TOP_N_LEVELS]:
            if _persistent(price, recent_ask_snaps):
                ask_size += qty

        total = bid_size + ask_size
        if total <= 0:
            return None
        return (bid_size - ask_size) / total


# Process-wide singleton (mirrors liquidation_tracker pattern).
_tracker: Optional[OrderBookTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> OrderBookTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = OrderBookTracker()
    return _tracker
