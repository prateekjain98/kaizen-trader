"""Coinbase Advanced Trade WebSocket feed."""

import json
import threading
import time
from typing import Callable, Optional

import websocket

from src.storage.database import log

WS_URL = "wss://advanced-trade-ws.coinbase.com"
MAX_BACKOFF_MS = 30_000
MAX_RECONNECT_ATTEMPTS = 10
PING_INTERVAL_S = 20
PING_TIMEOUT_S = 10
SUBSCRIPTION_TIMEOUT_S = 10
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
        self._ws: Optional[websocket.WebSocketApp] = None
        self._status = "disconnected"
        self._reconnect_attempts = 0
        self._reconnect_timer: Optional[threading.Timer] = None
        self._reconnect_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._subscription_confirmed = False

    def connect(self) -> None:
        if self._status in ("connected", "connecting"):
            return
        self._status = "connecting"
        log("info", f"Coinbase WS connecting (attempt {self._reconnect_attempts + 1})...")

        ws = websocket.WebSocketApp(
            WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        self._thread = threading.Thread(
            target=ws.run_forever,
            kwargs={"ping_interval": PING_INTERVAL_S, "ping_timeout": PING_TIMEOUT_S},
            daemon=True,
        )
        self._thread.start()

    def _on_open(self, ws) -> None:
        self._status = "connected"
        self._reconnect_attempts = 0
        log("info", f"Coinbase WS connected — subscribing to {len(self.product_ids)} products")
        self._subscription_confirmed = False
        self._subscribe()
        self._start_subscription_timeout()

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            log("error", f"Coinbase WS JSON parse error: {e}", data={"raw": raw[:500]})
            return
        try:
            self._handle_message(msg)
        except Exception as e:
            log("error", f"Coinbase WS message handling error: {e}", data={"msg_type": msg.get("type")})

    def _on_error(self, ws, err) -> None:
        log("warn", f"Coinbase WS error: {err}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._status = "disconnected"
        log("warn", "Coinbase WS disconnected — scheduling reconnect")
        self._schedule_reconnect()

    def _subscribe(self) -> None:
        if not self._ws:
            return
        self._ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": self.product_ids,
            "channels": ["ticker", "level2"],
        }))

    def _start_subscription_timeout(self) -> None:
        """Log a warning if subscription confirmation is not received in time."""
        def _check():
            if not self._subscription_confirmed:
                log("warn", f"Coinbase WS subscription confirmation not received within {SUBSCRIPTION_TIMEOUT_S}s")
        timer = threading.Timer(SUBSCRIPTION_TIMEOUT_S, _check)
        timer.daemon = True
        timer.start()

    def _handle_message(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "ticker":
            symbol = msg.get("product_id", "").replace("-USD", "")
            try:
                price = float(msg["price"])
                volume = float(msg["volume_24h"])
                if price > 0:
                    self.on_tick(symbol, price, volume)
            except (KeyError, ValueError) as e:
                log("warn", f"Coinbase WS ticker parse error: {e}", symbol=symbol,
                    data={"raw_keys": list(msg.keys())})

        elif msg_type == "l2update":
            product_id = msg.get("product_id", "")
            with _book_lock:
                book = _get_book(product_id)
                for change in msg.get("changes", []):
                    try:
                        side, price_str, size_str = change
                        size = float(size_str)
                        book_side = book["bids"] if side == "buy" else book["asks"]
                        if size == 0:
                            book_side.pop(price_str, None)
                        else:
                            book_side[price_str] = size
                    except (ValueError, TypeError) as e:
                        log("warn", f"Coinbase WS l2update parse error: {e}",
                            data={"change": str(change)[:200]})
                book["updated_at"] = time.time()
                # Snapshot inside lock to prevent race with purge_stale_books()
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

        elif msg_type == "subscriptions":
            self._subscription_confirmed = True
            log("info", "Coinbase WS subscriptions confirmed")

    def _schedule_reconnect(self) -> None:
        with self._reconnect_lock:
            if self._reconnect_timer:
                return
            if self._reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                log("error", f"Coinbase WS giving up after {MAX_RECONNECT_ATTEMPTS} reconnect attempts")
                return
            backoff_ms = min(MAX_BACKOFF_MS, 1000 * (2 ** self._reconnect_attempts))
            self._reconnect_attempts += 1
            log("info", f"Coinbase WS reconnecting in {backoff_ms}ms (attempt {self._reconnect_attempts})")

            def do_reconnect():
                with self._reconnect_lock:
                    self._reconnect_timer = None
                self.connect()

            self._reconnect_timer = threading.Timer(backoff_ms / 1000, do_reconnect)
            self._reconnect_timer.daemon = True
            self._reconnect_timer.start()

    def disconnect(self) -> None:
        with self._reconnect_lock:
            if self._reconnect_timer:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
        if self._ws:
            self._ws.close()
        self._status = "disconnected"

    def is_connected(self) -> bool:
        return self._status == "connected"

    def update_products(self, product_ids: list[str]) -> None:
        self.product_ids = product_ids
        if self._status == "connected":
            self._subscribe()
