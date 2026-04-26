"""OKX WebSocket feed — real-time tickers (public) and orders/positions/balance (private).

Two channels:
- Public  (no auth):  per-instId `tickers` — used to track real-time price for
  symbols we have OPEN positions in. Reduces stop-check latency from 30s
  (watchdog REST) to <1s.
- Private (auth):    `account` (USDT balance changes), `positions`, `orders` —
  pushes fill/close events the moment they happen instead of REST polling.

Endpoint: env-configurable via `OKX_WS_BASE`. Defaults to `wss://ws.okx.com:8443`
for global accounts; set `wss://wseea.okx.com:8443` for licensed-jurisdiction
accounts (the same brand mapping as `OKX_BASE_URL` for REST).

Each "side" (public/private) is a separate WS connection in its own thread.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Callable, Optional

from src.config import env
from src.engine.log import log

# Default to global; OKX_WS_BASE env var overrides for licensed brands.
# Path conventions:
#   public:  /ws/v5/public
#   private: /ws/v5/private
import os
_OKX_WS_BASE = os.environ.get(
    "OKX_WS_BASE",
    "wss://ws.okx.com:8443" if env.okx_base_url == "https://www.okx.com" else "wss://wseea.okx.com:8443"
).rstrip("/")


# ─── Helper ─────────────────────────────────────────────────────────────────

def _login_payload() -> dict:
    """OKX private WS login.

    Signature: base64(HMAC-SHA256(SECRET, TS + 'GET' + '/users/self/verify'))
    where TS is unix seconds (NOT milliseconds, NOT ISO).
    """
    ts = str(int(time.time()))
    prehash = ts + "GET" + "/users/self/verify"
    sig = base64.b64encode(
        hmac.new((env.okx_api_secret or "").encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "op": "login",
        "args": [{
            "apiKey": env.okx_api_key or "",
            "passphrase": env.okx_passphrase or "",
            "timestamp": ts,
            "sign": sig,
        }],
    }


# ─── Public WS (tickers) ────────────────────────────────────────────────────

class OKXPublicWS:
    """OKX public WS — per-instId ticker subscriptions for OPEN-position symbols.

    Subscriptions are dynamic: call subscribe([symbols]) / unsubscribe([symbols])
    whenever positions open/close. Idempotent — duplicate subscribes are no-ops.

    Symbols are passed in generic form ('BTC', 'ETH') and converted to OKX
    instIds ('BTC-USDT-SWAP') internally.
    """

    def __init__(self, on_ticker: Callable[[str, float, float, float], None]):
        # on_ticker(symbol, last_price, bid, ask)
        self.on_ticker = on_ticker
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status = "disconnected"
        self._connected_at: float = 0.0
        self._last_msg_ts: float = time.time()
        self._tick_count = 0
        self._lock = threading.Lock()
        # Symbols we want subscribed. The reconnect loop re-subscribes from this set.
        self._wanted: set[str] = set()
        # Symbols actually subscribed on the live socket (cleared on disconnect)
        self._active: set[str] = set()

    @staticmethod
    def _to_inst_id(symbol: str) -> str:
        s = symbol.upper().replace("-", "")
        if s.endswith("USDT"):
            base = s[:-4]
        elif s.endswith("USD"):
            base = s[:-3]
        else:
            base = s
        return f"{base}-USDT-SWAP"

    def connect(self) -> None:
        if self._status in ("connected", "connecting"):
            return
        self._status = "connecting"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="okx-public-ws")
        self._thread.start()

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            log("error", "websocket-client not installed for OKX WS")
            return
        url = f"{_OKX_WS_BASE}/ws/v5/public"
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                # OKX recommends ping every 30s; library handles WS-protocol ping_interval
                ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as exc:
                log("warn", f"OKX public WS error: {exc}")
            self._status = "disconnected"
            with self._lock:
                self._active.clear()
            if self._stop.is_set():
                break
            log("info", "OKX public WS reconnecting in 5s...")
            self._stop.wait(timeout=5)

    def _on_open(self, ws) -> None:
        self._status = "connected"
        self._connected_at = time.time()
        self._last_msg_ts = time.time()
        log("info", f"OKX public WS connected ({_OKX_WS_BASE})")
        # Re-subscribe to whatever was wanted before disconnect
        with self._lock:
            wanted = list(self._wanted)
        if wanted:
            self._send_subscribe(wanted)

    def _on_message(self, ws, message: str) -> None:
        self._last_msg_ts = time.time()
        try:
            d = json.loads(message)
        except (ValueError, TypeError):
            return
        if d.get("event") == "subscribe":
            inst = d.get("arg", {}).get("instId", "")
            if inst:
                with self._lock:
                    self._active.add(inst)
            return
        if d.get("event") == "error":
            log("warn", f"OKX public WS error msg: {d.get('msg')} (code {d.get('code')})")
            return
        # tickers data: arg has channel/instId, data is list[dict]
        arg = d.get("arg", {})
        if arg.get("channel") != "tickers":
            return
        for row in d.get("data", []):
            try:
                inst_id = row.get("instId", "")
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                symbol = inst_id.split("-")[0]
                last = float(row.get("last", 0) or 0)
                bid = float(row.get("bidPx", 0) or 0)
                ask = float(row.get("askPx", 0) or 0)
                if last > 0:
                    self.on_ticker(symbol, last, bid, ask)
                    self._tick_count += 1
            except (ValueError, TypeError):
                continue

    def _on_error(self, ws, error) -> None:
        log("warn", f"OKX public WS error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        self._status = "disconnected"

    def _send_subscribe(self, symbols: list[str]) -> None:
        if not self._ws or self._status != "connected":
            return
        args = [{"channel": "tickers", "instId": self._to_inst_id(s)} for s in symbols]
        try:
            self._ws.send(json.dumps({"op": "subscribe", "args": args}))
        except Exception as exc:
            log("warn", f"OKX public WS subscribe failed: {exc}")

    def _send_unsubscribe(self, symbols: list[str]) -> None:
        if not self._ws or self._status != "connected":
            return
        args = [{"channel": "tickers", "instId": self._to_inst_id(s)} for s in symbols]
        try:
            self._ws.send(json.dumps({"op": "unsubscribe", "args": args}))
        except Exception as exc:
            log("warn", f"OKX public WS unsubscribe failed: {exc}")

    def subscribe(self, symbols: list[str]) -> None:
        """Add symbols to the wanted set; subscribe new ones if connected."""
        new: list[str] = []
        with self._lock:
            for s in symbols:
                if s not in self._wanted:
                    self._wanted.add(s)
                    new.append(s)
        if new and self._status == "connected":
            self._send_subscribe(new)

    def unsubscribe(self, symbols: list[str]) -> None:
        gone: list[str] = []
        with self._lock:
            for s in symbols:
                if s in self._wanted:
                    self._wanted.discard(s)
                    inst = self._to_inst_id(s)
                    if inst in self._active:
                        gone.append(s)
                        self._active.discard(inst)
        if gone and self._status == "connected":
            self._send_unsubscribe(gone)

    def disconnect(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._status = "disconnected"

    def is_connected(self) -> bool:
        return self._status == "connected"

    def check_health(self, max_silence_s: float = 60.0) -> bool:
        """If we have wanted subs but no msgs in N sec, force-reconnect."""
        if self._status != "connected":
            return False
        with self._lock:
            wanted_count = len(self._wanted)
        if wanted_count == 0:
            return True  # no subs = nothing to receive, healthy idle
        silence = time.time() - self._last_msg_ts
        if silence > max_silence_s:
            log("warn", f"OKX public WS silence {silence:.0f}s with {wanted_count} subs — reconnecting")
            self.disconnect()
            self.connect()
            return False
        return True


# ─── Private WS (orders / positions / account) ──────────────────────────────

class OKXPrivateWS:
    """OKX private WS — push notifications for orders, positions, balance.

    Three subscriptions after login:
      - account: USDT balance changes
      - positions instType=SWAP: open position state
      - orders instType=SWAP: order lifecycle (live/partially_filled/filled/canceled)

    Callbacks:
      on_order(order_dict)       — every order state change. Used to confirm fills
                                   instantly instead of REST polling _query_order.
      on_position(position_dict) — every position update (avgPx, pos, upl).
      on_balance(usdt_avail)     — USDT availBal change.
    """

    def __init__(
        self,
        on_order: Callable[[dict], None],
        on_position: Callable[[dict], None],
        on_balance: Callable[[float], None],
    ):
        self.on_order = on_order
        self.on_position = on_position
        self.on_balance = on_balance
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status = "disconnected"
        self._logged_in = False
        self._last_msg_ts: float = time.time()
        self._connected_at: float = 0.0

    def connect(self) -> None:
        if self._status in ("connected", "connecting"):
            return
        if not env.okx_api_key or not env.okx_api_secret or not env.okx_passphrase:
            log("warn", "OKX private WS: API credentials missing, skipping connect")
            return
        self._status = "connecting"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="okx-private-ws")
        self._thread.start()

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            log("error", "websocket-client not installed for OKX private WS")
            return
        url = f"{_OKX_WS_BASE}/ws/v5/private"
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as exc:
                log("warn", f"OKX private WS error: {exc}")
            self._status = "disconnected"
            self._logged_in = False
            if self._stop.is_set():
                break
            log("info", "OKX private WS reconnecting in 5s...")
            self._stop.wait(timeout=5)

    def _on_open(self, ws) -> None:
        self._status = "connected"
        self._connected_at = time.time()
        self._last_msg_ts = time.time()
        try:
            ws.send(json.dumps(_login_payload()))
        except Exception as exc:
            log("warn", f"OKX private WS login send failed: {exc}")

    def _on_message(self, ws, message: str) -> None:
        self._last_msg_ts = time.time()
        try:
            d = json.loads(message)
        except (ValueError, TypeError):
            return
        ev = d.get("event")
        if ev == "login":
            if d.get("code") == "0":
                self._logged_in = True
                log("info", f"OKX private WS logged in ({_OKX_WS_BASE})")
                self._subscribe_after_login()
            else:
                log("error", f"OKX private WS login failed: {d.get('msg')} (code {d.get('code')})")
            return
        if ev == "error":
            log("warn", f"OKX private WS error msg: {d.get('msg')} (code {d.get('code')})")
            return
        if ev == "subscribe":
            log("info", f"OKX private WS subscribed: {d.get('arg', {}).get('channel')}")
            return
        # Data messages
        arg = d.get("arg", {})
        channel = arg.get("channel", "")
        if channel == "orders":
            for row in d.get("data", []):
                try:
                    self.on_order(row)
                except Exception as exc:
                    log("warn", f"on_order callback error: {exc}")
        elif channel == "positions":
            for row in d.get("data", []):
                try:
                    self.on_position(row)
                except Exception as exc:
                    log("warn", f"on_position callback error: {exc}")
        elif channel == "account":
            for row in d.get("data", []):
                try:
                    for det in row.get("details", []):
                        if det.get("ccy") == "USDT":
                            self.on_balance(float(det.get("availBal", 0) or 0))
                            break
                except Exception as exc:
                    log("warn", f"on_balance callback error: {exc}")

    def _on_error(self, ws, error) -> None:
        log("warn", f"OKX private WS error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        self._status = "disconnected"
        self._logged_in = False

    def _subscribe_after_login(self) -> None:
        if not self._ws:
            return
        try:
            self._ws.send(json.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "account"},
                    {"channel": "positions", "instType": "SWAP"},
                    {"channel": "orders", "instType": "SWAP"},
                ],
            }))
        except Exception as exc:
            log("warn", f"OKX private WS subscribe failed: {exc}")

    def disconnect(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._status = "disconnected"
        self._logged_in = False

    def is_connected(self) -> bool:
        return self._status == "connected" and self._logged_in

    def check_health(self, max_silence_s: float = 90.0) -> bool:
        """Private WS gets less data — only fires on order/position/balance change.
        90s silence is normal during quiet periods, so the threshold is generous.
        """
        if self._status != "connected":
            return False
        if not self._logged_in:
            return False
        silence = time.time() - self._last_msg_ts
        if silence > max_silence_s:
            log("warn", f"OKX private WS silence {silence:.0f}s — reconnecting")
            self.disconnect()
            self.connect()
            return False
        return True
