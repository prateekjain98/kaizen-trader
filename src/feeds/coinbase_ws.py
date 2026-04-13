"""Coinbase Advanced Trade WebSocket feed using the official SDK."""

import json
import threading
import time
from typing import Callable, Optional

from src.config import env
from src.storage.database import log

BOOK_STALE_S = 60

_book_state: dict[str, dict] = {}
_book_lock = threading.Lock()


def _get_book(product_id: str) -> dict:
    if product_id not in _book_state:
        _book_state[product_id] = {"bids": {}, "asks": {}, "updated_at": time.time()}
    return _book_state[product_id]


def purge_stale_books(max_age_s: float = BOOK_STALE_S) -> int:
    """Remove book entries older than max_age_s. Returns number of purged products."""
    now = time.time()
    purged = 0
    with _book_lock:
        stale_keys = [
            k for k, v in _book_state.items()
            if now - v.get("updated_at", 0) > max_age_s
        ]
        for k in stale_keys:
            del _book_state[k]
            purged += 1
    if purged:
        log("info", f"Purged {purged} stale book entries")
    return purged


class CoinbaseWebSocket:
    def __init__(
        self,
        product_ids: list[str],
        on_tick: Callable[[str, float, float], None],
        on_book: Callable[[str, list, list], None],
    ):
        self.product_ids = product_ids
        self.on_tick = on_tick
        self.on_book = on_book
        self._client = None
        self._status = "disconnected"
        self._thread: Optional[threading.Thread] = None
        self._tick_count = 0
        self._last_tick_time = time.time()
        self._stop_event = threading.Event()
        self._last_vol_24h: dict[str, float] = {}  # track 24h volume to compute tick deltas

    def connect(self) -> None:
        if self._status in ("connected", "connecting"):
            return
        self._status = "connecting"
        self._stop_event.clear()

        try:
            from coinbase.websocket import WSClient
        except ImportError:
            log("error", "coinbase-advanced-py not installed — pip install coinbase-advanced-py")
            return

        api_key = env.coinbase_api_key or ""
        api_secret = env.coinbase_api_secret or ""

        def _make_ws_client():
            """Create a WSClient instance (shared between initial connect and reconnect)."""
            if api_key and api_secret:
                return WSClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    retry=False,
                )
            return WSClient(
                on_message=self._on_message,
                on_close=self._on_close,
                retry=False,
            )

        self._client = _make_ws_client()

        def _run():
            from coinbase.websocket import WSClientConnectionClosedException, WSClientException

            while not self._stop_event.is_set():
                try:
                    self._client.open()
                    self._status = "connected"
                    log("info", f"Coinbase WS connected — subscribing to {len(self.product_ids)} products")

                    self._client.subscribe(
                        product_ids=self.product_ids,
                        channels=["ticker", "heartbeats"],
                    )

                    log("info", "Coinbase WS subscriptions sent")

                    while not self._stop_event.is_set() and self._status == "connected":
                        self._client.sleep_with_exception_check(sleep=5)

                except WSClientConnectionClosedException:
                    log("warn", "Coinbase WS connection closed — reconnecting in 5s")
                except WSClientException as e:
                    log("warn", f"Coinbase WS error: {e} — reconnecting in 5s")
                except Exception as e:
                    log("error", f"Coinbase WS connection failed: {e} — reconnecting in 5s")

                self._status = "disconnected"
                if self._stop_event.is_set():
                    break

                if self._stop_event.wait(timeout=5):
                    break

                # Create a fresh client for reconnection
                try:
                    self._client = _make_ws_client()
                except Exception as e:
                    log("error", f"Failed to create WS client: {e}")
                    if self._stop_event.wait(timeout=10):
                        break

        self._thread = threading.Thread(target=_run, daemon=True, name="coinbase-ws")
        self._thread.start()

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        channel = msg.get("channel")
        events = msg.get("events", [])

        for event in events:
            if channel == "ticker":
                tickers = event.get("tickers", [])
                for t in tickers:
                    product_id = t.get("product_id", "")
                    symbol = product_id.replace("-USD", "")
                    try:
                        price = float(t["price"])
                        # Use per-tick volume if available, fallback to 24h volume
                        # Coinbase ticker provides volume_24_h (cumulative) — not per-tick
                        # Use the difference from last known 24h volume as proxy for tick volume
                        raw_vol_24h = float(t.get("volume_24_h", 0))
                        last_vol = self._last_vol_24h.get(symbol, raw_vol_24h)
                        volume = max(0, raw_vol_24h - last_vol) if last_vol > 0 else 0
                        self._last_vol_24h[symbol] = raw_vol_24h
                        if price > 0:
                            self.on_tick(symbol, price, volume)
                            self._tick_count += 1
                            self._last_tick_time = time.time()
                            if self._tick_count == 1:
                                log("info", f"First tick received: {symbol} @ ${price:,.2f}")
                    except Exception as e:
                        if self._tick_count < 5:
                            log("error", f"Tick handler error for {symbol}: {type(e).__name__}: {e}")
                        pass

            elif channel == "l2_data":
                product_id = event.get("product_id", "")
                updates = event.get("updates", [])
                if not updates:
                    continue
                with _book_lock:
                    book = _get_book(product_id)
                    for update in updates:
                        try:
                            side = update.get("side", "")
                            price_str = update.get("price_level", "")
                            size = float(update.get("new_quantity", 0))
                            book_side = book["bids"] if side == "bid" else book["asks"]
                            if size == 0:
                                book_side.pop(price_str, None)
                            else:
                                book_side[price_str] = size
                        except (ValueError, TypeError):
                            pass
                    book["updated_at"] = time.time()
                    bids_snapshot = list(book["bids"].items())
                    asks_snapshot = list(book["asks"].items())

                symbol = product_id.replace("-USD", "")
                bids = sorted(
                    [{"price": float(p), "size": s} for p, s in bids_snapshot],
                    key=lambda x: x["price"], reverse=True,
                )[:20]
                asks = sorted(
                    [{"price": float(p), "size": s} for p, s in asks_snapshot],
                    key=lambda x: x["price"],
                )[:20]
                self.on_book(symbol, bids, asks)

    def _on_close(self) -> None:
        was_connected = self._status == "connected"
        self._status = "disconnected"
        if was_connected:
            log("warn", "WebSocket disconnected — reconnecting")

    def check_health(self, max_silence_s: float = 30.0) -> bool:
        """Check if WS is receiving ticks. Force-reconnect if stale."""
        if self._status != "connected":
            return False
        silence = time.time() - self._last_tick_time
        if silence > max_silence_s:
            log("warn", f"WS tick silence for {silence:.0f}s — force reconnecting")
            self.force_reconnect()
            return False
        return True

    def force_reconnect(self) -> None:
        """Kill the current connection and reconnect."""
        self._status = "disconnected"
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        # The _run loop will detect the disconnection and reconnect automatically
        self._tick_count = 0
        self._last_tick_time = time.time()

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._status = "disconnected"

    def is_connected(self) -> bool:
        return self._status == "connected"

    def update_products(self, product_ids: list[str]) -> None:
        old = set(self.product_ids)
        new = set(product_ids)
        self.product_ids = product_ids

        if not self._client or self._status != "connected":
            return

        unsub = list(old - new)
        sub = list(new - old)
        if unsub:
            try:
                self._client.unsubscribe(product_ids=unsub, channels=["ticker"])
            except Exception:
                pass
        if sub:
            try:
                self._client.subscribe(product_ids=sub, channels=["ticker"])
            except Exception:
                pass
