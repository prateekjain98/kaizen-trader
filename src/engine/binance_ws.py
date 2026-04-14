"""Binance WebSocket feed — real-time prices, volume, and funding for ALL futures symbols.

Uses the combined stream endpoint to subscribe to multiple symbols in one connection.
No auth required for public market data streams.

Streams:
    - !miniTicker@arr — all symbol prices + volume in one message (every 1s)
    - !markPrice@arr — all mark prices + funding rates (every 3s)
"""

import json
import threading
import time
from typing import Callable, Optional

from src.engine.log import log

_BINANCE_WS_BASE = "wss://fstream.binance.com/ws"
_BINANCE_STREAM_BASE = "wss://fstream.binance.com/stream"


class BinanceWebSocket:
    """Binance Futures WebSocket — real-time market data for all symbols.

    Subscribes to:
        - !miniTicker@arr: all tickers every ~1s (price, volume, 24h change)
        - !markPrice@arr: mark prices + funding rates every ~3s

    Callbacks:
        on_tick(symbol, price, volume_24h, change_pct_24h)
        on_funding(symbol, funding_rate, mark_price)
    """

    def __init__(
        self,
        on_tick: Callable[[str, float, float, float], None],
        on_funding: Callable[[str, float, float], None],
    ):
        self.on_tick = on_tick
        self.on_funding = on_funding
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status = "disconnected"
        self._tick_count = 0
        self._last_tick_time = time.time()

    def connect(self):
        """Start WebSocket connection in background thread."""
        if self._status in ("connected", "connecting"):
            return
        self._status = "connecting"
        self._stop.clear()

        self._thread = threading.Thread(target=self._run, daemon=True, name="binance-ws")
        self._thread.start()

    def _run(self):
        """Main WebSocket loop with auto-reconnect."""
        try:
            import websocket
        except ImportError:
            log("error", "websocket-client not installed — pip install websocket-client")
            # Fallback: use urllib-based polling
            self._run_polling_fallback()
            return

        while not self._stop.is_set():
            try:
                # Subscribe to all mini tickers + all mark prices
                url = f"{_BINANCE_STREAM_BASE}?streams=!miniTicker@arr/!markPrice@arr"

                ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                    on_open=self._on_ws_open,
                )
                self._ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)

            except Exception as e:
                log("warn", f"Binance WS error: {e}")

            self._status = "disconnected"
            if self._stop.is_set():
                break
            log("info", "Binance WS reconnecting in 5s...")
            self._stop.wait(timeout=5)

    def _on_ws_open(self, ws):
        self._status = "connected"
        log("info", "Binance Futures WS connected — receiving all tickers + funding")

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            stream = data.get("stream", "")
            payload = data.get("data", [])

            if stream == "!miniTicker@arr" and isinstance(payload, list):
                for t in payload:
                    symbol_raw = t.get("s", "")
                    if not symbol_raw.endswith("USDT"):
                        continue
                    symbol = symbol_raw.replace("USDT", "")
                    # Skip 1000-prefixed tokens
                    if symbol.startswith("1000"):
                        symbol = symbol[4:]

                    try:
                        price = float(t.get("c", 0))  # close price
                        volume = float(t.get("q", 0))  # quote volume 24h
                        change_pct = float(t.get("P", 0))  # 24h change %
                        if price > 0:
                            self.on_tick(symbol, price, volume, change_pct)
                            self._tick_count += 1
                            self._last_tick_time = time.time()
                    except (ValueError, TypeError):
                        pass

            elif stream == "!markPrice@arr" and isinstance(payload, list):
                for m in payload:
                    symbol_raw = m.get("s", "")
                    if not symbol_raw.endswith("USDT"):
                        continue
                    symbol = symbol_raw.replace("USDT", "")
                    if symbol.startswith("1000"):
                        symbol = symbol[4:]

                    try:
                        funding = float(m.get("r", 0))  # funding rate
                        mark_price = float(m.get("p", 0))  # mark price
                        if abs(funding) > 0.0001:  # only notable rates
                            self.on_funding(symbol, funding, mark_price)
                    except (ValueError, TypeError):
                        pass

        except (json.JSONDecodeError, TypeError):
            pass

    def _on_ws_error(self, ws, error):
        log("warn", f"Binance WS error: {error}")

    def _on_ws_close(self, ws, close_code, close_msg):
        self._status = "disconnected"

    def _run_polling_fallback(self):
        """Fallback: poll REST API if websocket-client not installed."""
        log("warn", "Binance WS using REST polling fallback (install websocket-client for real-time)")
        from urllib.request import urlopen, Request

        while not self._stop.is_set():
            try:
                # Mini tickers
                req = Request(
                    "https://fapi.binance.com/fapi/v1/ticker/24hr",
                    headers={"User-Agent": "kaizen-trader/2.0"}
                )
                data = json.loads(urlopen(req, timeout=10).read().decode())
                for t in data:
                    sym = t.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    symbol = sym.replace("USDT", "")
                    if symbol.startswith("1000"):
                        symbol = symbol[4:]
                    try:
                        price = float(t.get("lastPrice", 0))
                        volume = float(t.get("quoteVolume", 0))
                        change = float(t.get("priceChangePercent", 0))
                        if price > 0:
                            self.on_tick(symbol, price, volume, change)
                            self._tick_count += 1
                            self._last_tick_time = time.time()
                    except (ValueError, TypeError):
                        pass

                # Mark prices + funding
                req2 = Request(
                    "https://fapi.binance.com/fapi/v1/premiumIndex",
                    headers={"User-Agent": "kaizen-trader/2.0"}
                )
                data2 = json.loads(urlopen(req2, timeout=10).read().decode())
                for m in data2:
                    sym = m.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    symbol = sym.replace("USDT", "")
                    if symbol.startswith("1000"):
                        symbol = symbol[4:]
                    try:
                        funding = float(m.get("lastFundingRate", 0))
                        mark = float(m.get("markPrice", 0))
                        if abs(funding) > 0.0001:
                            self.on_funding(symbol, funding, mark)
                    except (ValueError, TypeError):
                        pass

            except Exception as e:
                log("warn", f"Binance REST poll error: {e}")

            self._stop.wait(timeout=3)  # poll every 3s

    def disconnect(self):
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._status = "disconnected"

    def is_connected(self) -> bool:
        return self._status == "connected"

    def check_health(self, max_silence_s: float = 30.0) -> bool:
        """Check if WS is receiving ticks. Force-reconnect if stale."""
        if self._status != "connected":
            return False
        silence = time.time() - self._last_tick_time
        if silence > max_silence_s:
            log("warn", f"Binance WS tick silence for {silence:.0f}s — force reconnecting")
            self.disconnect()
            self.connect()
            return False
        return True

    @property
    def tick_count(self) -> int:
        return self._tick_count
