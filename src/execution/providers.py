"""Concrete ExecutionProvider implementations wrapping existing executors."""

import base64
import hashlib
import hmac
import math
import time
import uuid
from datetime import datetime, timezone

import requests

from src.config import env
from src.storage.database import log
from src.types import Trade
from src.utils.binance_symbols import BINANCE_SYMBOL_MAP, to_binance_ticker


def _failed_trade(position_id: str, symbol: str, side: str, error: str) -> Trade:
    """Create a standardized failed Trade object."""
    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side=side,
        symbol=symbol, quantity=0, size_usd=0, price=0,
        status="failed", paper_trading=False,
        placed_at=time.time() * 1000, error=error,
    )


class CoinbaseProvider:
    """Wraps src.execution.coinbase for the ExecutionProvider protocol."""

    @property
    def name(self) -> str:
        return "coinbase"

    def buy(self, symbol: str, product_id: str, size_usd: float,
            position_id: str, market_price: float) -> Trade:
        from src.execution.coinbase import place_buy_order
        return place_buy_order(product_id, size_usd, position_id)

    def sell(self, symbol: str, product_id: str, quantity: float,
             position_id: str, market_price: float) -> Trade:
        from src.execution.coinbase import place_sell_order
        return place_sell_order(product_id, quantity, position_id)

    def get_balances(self) -> dict[str, float]:
        from src.execution.coinbase import get_account_balances
        return get_account_balances()


class PaperProvider:
    """Wraps src.execution.paper for the ExecutionProvider protocol."""

    @property
    def name(self) -> str:
        return "paper"

    def buy(self, symbol: str, product_id: str, size_usd: float,
            position_id: str, market_price: float) -> Trade:
        from src.execution.paper import paper_buy
        return paper_buy(symbol, product_id, size_usd, position_id, market_price)

    def sell(self, symbol: str, product_id: str, quantity: float,
             position_id: str, market_price: float) -> Trade:
        from src.execution.paper import paper_sell
        return paper_sell(symbol, product_id, quantity, position_id, market_price)

    def get_balances(self) -> dict[str, float]:
        from src.execution.paper import get_paper_balance, get_paper_holdings
        balances = get_paper_holdings()
        balances["USD"] = get_paper_balance()
        return balances


class BinanceProvider:
    """Binance Futures execution provider.

    Uses Binance USDM Futures API for perpetual contracts.
    Requires BINANCE_API_KEY and BINANCE_API_SECRET in env.
    """

    FAPI_BASE = "https://fapi.binance.com"

    def __init__(self) -> None:
        self._leverage_set: set[str] = set()
        self._step_sizes: dict[str, float] = {}
        self._exchange_info_loaded: bool = False
        self._load_exchange_info()

    def _load_exchange_info(self) -> None:
        """Fetch exchange info once and cache LOT_SIZE step sizes for all USDT perps."""
        if self._exchange_info_loaded:
            return
        try:
            resp = requests.get(
                f"{self.FAPI_BASE}/fapi/v1/exchangeInfo", timeout=15,
            )
            resp.raise_for_status()
            for sym_info in resp.json().get("symbols", []):
                symbol = sym_info.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                for f in sym_info.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        self._step_sizes[symbol] = float(f["stepSize"])
                        break
            self._exchange_info_loaded = True
            log("info", f"Binance exchange info cached: {len(self._step_sizes)} USDT pairs")
        except Exception as exc:
            log("warn", f"Failed to load Binance exchange info: {exc}")

    def _round_step(self, binance_symbol: str, qty: float) -> float:
        """Round quantity down to the symbol's LOT_SIZE step size.

        Float arithmetic can leave trailing precision drift even after flooring
        to step (e.g. math.floor(46.1/0.1)*0.1 = 46.10000000000001), which
        Binance rejects with -1111 "Precision is over the maximum". Round to
        the decimal count implied by step to scrub it.
        """
        step = self._step_sizes.get(binance_symbol)
        if not step or step <= 0:
            return qty
        rounded = math.floor(qty / step) * step
        # Decimals from step: 0.001 -> 3, 1.0 -> 0, 0.5 -> 1.
        if step >= 1:
            decimals = 0
        else:
            decimals = max(0, -int(math.floor(math.log10(step))))
        return round(rounded, decimals)

    @property
    def name(self) -> str:
        return "binance"

    def _get_binance_symbol(self, symbol: str) -> str:
        return to_binance_ticker(symbol) or f"{symbol.upper()}USDT"

    def _ensure_leverage(self, binance_symbol: str) -> None:
        """Set leverage to 1x for a symbol (once per session)."""
        if binance_symbol in self._leverage_set:
            return
        if not env.binance_api_key or not env.binance_api_secret:
            return
        timestamp = int(time.time() * 1000)
        params = f"symbol={binance_symbol}&leverage=1&timestamp={timestamp}"
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()
        try:
            time.sleep(0.1)  # rate-limit guard
            resp = requests.post(
                f"{self.FAPI_BASE}/fapi/v1/leverage",
                headers={"X-MBX-APIKEY": env.binance_api_key},
                data=f"{params}&signature={signature}",
                timeout=10,
            )
            resp.raise_for_status()
            self._leverage_set.add(binance_symbol)
            log("info", f"Binance leverage set to 1x for {binance_symbol}")
        except requests.exceptions.HTTPError as exc:
            body = exc.response.text if exc.response is not None else ""
            log("warn", f"Failed to set leverage for {binance_symbol}: {exc} — {body}")
        except Exception as exc:
            log("warn", f"Failed to set leverage for {binance_symbol}: {exc}")

    def _sign_and_post(self, params: str) -> dict:
        """Sign params with HMAC and POST to Binance Futures order endpoint."""
        if not env.binance_api_secret or not env.binance_api_key:
            raise ValueError("Binance API keys not configured")
        time.sleep(0.1)  # rate-limit guard
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()
        resp = requests.post(
            f"{self.FAPI_BASE}/fapi/v1/order",
            headers={"X-MBX-APIKEY": env.binance_api_key},
            data=f"{params}&signature={signature}",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _query_order_by_cl_ord_id(self, binance_symbol: str, cl_ord_id: str) -> dict:
        """GET a single order's state from Binance using the client order ID
        (origClientOrderId). Used for orphan recovery after network timeouts."""
        if not env.binance_api_secret or not env.binance_api_key:
            return {}
        time.sleep(0.1)
        timestamp = int(time.time() * 1000)
        params = f"symbol={binance_symbol}&origClientOrderId={cl_ord_id}&timestamp={timestamp}"
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()
        resp = requests.get(
            f"{self.FAPI_BASE}/fapi/v1/order",
            headers={"X-MBX-APIKEY": env.binance_api_key},
            params={"symbol": binance_symbol, "origClientOrderId": cl_ord_id,
                    "timestamp": timestamp, "signature": signature},
            timeout=10,
        )
        # 404 with -2013 means the order was never accepted — return empty.
        if resp.status_code == 400 and "-2013" in resp.text:
            return {}
        resp.raise_for_status()
        return resp.json()

    def _query_order(self, binance_symbol: str, order_id: int) -> dict:
        """GET a single order's current state from Binance Futures."""
        if not env.binance_api_secret or not env.binance_api_key:
            return {}
        time.sleep(0.1)  # rate-limit guard
        timestamp = int(time.time() * 1000)
        params = f"symbol={binance_symbol}&orderId={order_id}&timestamp={timestamp}"
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()
        resp = requests.get(
            f"{self.FAPI_BASE}/fapi/v1/order",
            headers={"X-MBX-APIKEY": env.binance_api_key},
            params={"symbol": binance_symbol, "orderId": order_id,
                    "timestamp": timestamp, "signature": signature},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _poll_fill(self, binance_symbol: str, order_id: int,
                   max_attempts: int = 8, delay: float = 0.4) -> dict | None:
        """Poll order status until FILLED or max attempts reached."""
        for i in range(max_attempts):
            time.sleep(delay)
            try:
                data = self._query_order(binance_symbol, order_id)
                status = data.get("status", "")
                if status == "FILLED":
                    return data
                if status in ("CANCELED", "REJECTED", "EXPIRED"):
                    log("warn", f"Order {order_id} ended with status {status}")
                    return data
            except Exception as exc:
                log("warn", f"Poll attempt {i+1}/{max_attempts} failed for order {order_id}: {exc}")
        return None

    def _place_order(self, symbol: str, position_id: str, side: str,
                     quantity: float, market_price: float,
                     reduce_only: bool = False) -> Trade:
        """Shared order execution for both buy and sell.

        reduce_only: when True (i.e. closing a position), Binance Futures will
        guarantee the order only reduces an existing position rather than
        opening a new opposite-side position. Critical to prevent flipping net
        short after a partial server-side stop fill.
        """
        binance_symbol = self._get_binance_symbol(symbol)
        self._ensure_leverage(binance_symbol)

        if not env.binance_api_key or not env.binance_api_secret:
            log("error", f"Binance API keys not configured for {symbol}", symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), "Binance API keys not configured")

        # Round quantity to symbol's LOT_SIZE step size
        quantity = self._round_step(binance_symbol, quantity)

        if quantity <= 0:
            return _failed_trade(position_id, symbol, side.lower(), f"Invalid quantity after rounding: {quantity}")

        timestamp = int(time.time() * 1000)
        # newClientOrderId acts as idempotency key — replays of the same params
        # within a session are safely deduped by Binance (returns -2010/4015).
        params = (
            f"symbol={binance_symbol}&side={side}&type=MARKET"
            f"&quantity={quantity}&timestamp={timestamp}"
            f"&newClientOrderId={position_id[:36]}"
        )
        if reduce_only:
            params += "&reduceOnly=true"

        try:
            data = self._sign_and_post(params)
            order_id = data.get("orderId")
            avg_price = float(data.get("avgPrice", 0))
            filled_qty = float(data.get("executedQty", 0))
            status = data.get("status", "")

            # Poll for fill if order not immediately filled
            if status != "FILLED" and order_id:
                log("info", f"Order {order_id} status={status}, polling for fill...",
                    symbol=symbol)
                poll_data = self._poll_fill(binance_symbol, order_id)
                if poll_data:
                    avg_price = float(poll_data.get("avgPrice", avg_price))
                    filled_qty = float(poll_data.get("executedQty", filled_qty))
                    status = poll_data.get("status", status)

            # Fallback to market_price if avgPrice is still 0
            if avg_price <= 0:
                avg_price = market_price
            if filled_qty <= 0:
                filled_qty = quantity

            if status not in ("FILLED", ""):
                log("warn", f"Order {order_id} final status: {status}", symbol=symbol)

            log("trade", f"Binance {side} filled: {binance_symbol} {filled_qty} @ {avg_price}",
                symbol=symbol, data={"order_id": order_id, "avg_price": avg_price})

            return Trade(
                id=str(uuid.uuid4()), position_id=position_id, side=side.lower(),
                symbol=symbol, quantity=filled_qty,
                size_usd=filled_qty * avg_price, price=avg_price,
                order_id=str(order_id or ""),
                status="filled", paper_trading=False,
                placed_at=time.time() * 1000,
            )
        except requests.exceptions.HTTPError as exc:
            body = exc.response.text if exc.response is not None else "no response"
            safe_msg = f"Binance {side} order failed: {exc.response.status_code if exc.response else '?'} {body}"
            log("error", safe_msg, symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), safe_msg)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # Critical: the request may have reached Binance and filled. Query
            # by clientOrderId (= position_id[:36], the idempotency key) before
            # declaring failure — otherwise we orphan a live position.
            cl_ord_id = position_id[:36]
            log("warn", f"Binance {side} {symbol}: {type(exc).__name__} — querying order by clOrdId for orphan check")
            try:
                recovered = self._query_order_by_cl_ord_id(binance_symbol, cl_ord_id)
                if recovered and recovered.get("status") in ("FILLED", "PARTIALLY_FILLED"):
                    avg_price = float(recovered.get("avgPrice") or market_price)
                    filled_qty = float(recovered.get("executedQty") or quantity)
                    log("trade", f"Recovered orphaned fill: {symbol} {filled_qty} @ {avg_price}",
                        symbol=symbol)
                    return Trade(
                        id=str(uuid.uuid4()), position_id=position_id, side=side.lower(),
                        symbol=symbol, quantity=filled_qty, size_usd=filled_qty * avg_price,
                        price=avg_price, order_id=str(recovered.get("orderId", "")),
                        status="filled", paper_trading=False,
                        placed_at=time.time() * 1000,
                    )
            except Exception as recover_exc:
                log("error", f"Orphan-recovery query failed for {symbol}: {recover_exc}")
            safe_msg = f"Binance {side} order failed ({type(exc).__name__}: {exc})"
            log("error", safe_msg, symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), safe_msg)
        except Exception as exc:
            safe_msg = f"Binance {side} order failed ({type(exc).__name__}: {exc})"
            log("error", safe_msg, symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), safe_msg)

    def buy(self, symbol: str, product_id: str, size_usd: float,
            position_id: str, market_price: float) -> Trade:
        if market_price <= 0 or size_usd <= 0:
            return _failed_trade(position_id, symbol, "buy", "Invalid price or size")
        quantity = size_usd / market_price
        return self._place_order(symbol, position_id, "BUY", quantity, market_price)

    def sell(self, symbol: str, product_id: str, quantity: float,
             position_id: str, market_price: float,
             reduce_only: bool = True) -> Trade:
        # Default reduce_only=True for sells: this provider only opens longs,
        # so a sell is always a close. Callers opening a real short can pass False.
        if quantity <= 0:
            return _failed_trade(position_id, symbol, "sell", "Invalid quantity")
        return self._place_order(symbol, position_id, "SELL", quantity, market_price,
                                 reduce_only=reduce_only)

    def get_balances(self) -> dict[str, float]:
        if not env.binance_api_key or not env.binance_api_secret:
            return {}

        timestamp = int(time.time() * 1000)
        params = f"timestamp={timestamp}"
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()

        try:
            time.sleep(0.1)  # rate-limit guard
            resp = requests.get(
                f"{self.FAPI_BASE}/fapi/v2/balance",
                headers={"X-MBX-APIKEY": env.binance_api_key},
                params={"timestamp": timestamp, "signature": signature},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                item["asset"]: float(item["availableBalance"])
                for item in data
                if float(item.get("availableBalance", 0)) > 0
            }
        except Exception as exc:
            log("warn", f"Binance balance fetch failed: {exc}")
            return {}


class OKXProvider:
    """OKX Futures (SWAP) execution provider.

    Uses OKX V5 API for perpetual contracts.
    Requires OKX_API_KEY, OKX_API_SECRET, and OKX_PASSPHRASE in env.
    BASE_URL is env-configurable (OKX_BASE_URL) — licensed-jurisdiction accounts
    (e.g. SG entity) live on https://my.okx.com instead of www.okx.com.
    """

    def __init__(self) -> None:
        self.BASE_URL = env.okx_base_url
        self._leverage_set: set[str] = set()
        self._instruments: dict[str, dict] = {}
        self._exchange_info_loaded: bool = False
        # Position mode: "net_mode" or "long_short_mode" (hedge). Detected on
        # first authenticated call; affects whether we send reduceOnly or posSide.
        self._pos_mode: str | None = None
        self._load_exchange_info()

    # ── Exchange info ──────────────────────────────────────────────────

    def _load_exchange_info(self) -> None:
        """Fetch SWAP instrument info once and cache contract values and lot sizes."""
        if self._exchange_info_loaded:
            return
        try:
            resp = requests.get(
                f"{self.BASE_URL}/api/v5/public/instruments",
                params={"instType": "SWAP"},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != "0":
                log("warn", f"OKX instruments request returned code {body.get('code')}: {body.get('msg')}")
                return
            for inst in body.get("data", []):
                inst_id = inst.get("instId", "")
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                self._instruments[inst_id] = {
                    "ctVal": float(inst.get("ctVal", 1)),
                    "lotSz": float(inst.get("lotSz", 1)),
                    "tickSz": float(inst.get("tickSz", "0.01")),
                    "minSz": float(inst.get("minSz", 1)),
                }
            self._exchange_info_loaded = True
            log("info", f"OKX exchange info cached: {len(self._instruments)} USDT-SWAP pairs")
        except Exception as exc:
            log("warn", f"Failed to load OKX exchange info: {exc}")

    # ── Symbol conversion ──────────────────────────────────────────────

    @staticmethod
    def _to_okx_inst_id(symbol: str) -> str:
        """Convert a generic symbol like 'BTC' or 'BTCUSDT' to OKX instId 'BTC-USDT-SWAP'."""
        s = symbol.upper().replace("-", "")
        if s.endswith("USDT"):
            base = s[:-4]
        elif s.endswith("USD"):
            base = s[:-3]
        else:
            base = s
        return f"{base}-USDT-SWAP"

    # ── Auth / signing ─────────────────────────────────────────────────

    @staticmethod
    def _timestamp() -> str:
        """ISO 8601 timestamp with millisecond precision for OKX auth."""
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    @staticmethod
    def _sign(timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """HMAC-SHA256 signature, base64-encoded."""
        if not env.okx_api_secret:
            raise ValueError("OKX API secret not configured")
        prehash = timestamp + method + request_path + body
        mac = hmac.new(
            env.okx_api_secret.encode(), prehash.encode(), hashlib.sha256,
        ).digest()
        return base64.b64encode(mac).decode()

    def _headers(self, timestamp: str, method: str, request_path: str, body: str = "") -> dict:
        """Build OKX authentication headers."""
        return {
            "OK-ACCESS-KEY": env.okx_api_key or "",
            "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": env.okx_passphrase or "",
            "Content-Type": "application/json",
        }

    # ── Quantity helpers ───────────────────────────────────────────────

    def _round_contracts(self, inst_id: str, contracts: float) -> float:
        """Round contract count down to the instrument's lot size.
        Strip trailing float-precision artifacts (e.g. 2.0000000000000004) by
        rounding to the lotSz's implied decimal count — same fix as
        BinanceProvider._round_step. Without this OKX rejects on micro-lot
        instruments (lotSz < 1) with precision errors."""
        info = self._instruments.get(inst_id)
        if info and info["lotSz"] > 0:
            lot = info["lotSz"]
            raw = math.floor(contracts / lot) * lot
            decimals = max(0, -int(math.floor(math.log10(lot)))) if lot < 1 else 0
            return round(raw, decimals)
        return math.floor(contracts)

    def _usd_to_contracts(self, inst_id: str, size_usd: float, market_price: float) -> float:
        """Convert a USD notional amount to number of contracts.

        For OKX SWAP, 1 contract = ctVal units of base currency.
        So: contracts = size_usd / (market_price * ctVal)
        """
        info = self._instruments.get(inst_id)
        ct_val = info["ctVal"] if info else 1.0
        raw = size_usd / (market_price * ct_val)
        return self._round_contracts(inst_id, raw)

    def _contracts_to_base_qty(self, inst_id: str, contracts: float) -> float:
        """Convert contracts back to base currency quantity for Trade reporting."""
        info = self._instruments.get(inst_id)
        ct_val = info["ctVal"] if info else 1.0
        return contracts * ct_val

    # ── Leverage ───────────────────────────────────────────────────────

    def _ensure_leverage(self, inst_id: str) -> None:
        """Set leverage to 1x for a symbol (once per session)."""
        if inst_id in self._leverage_set:
            return
        if not env.okx_api_key or not env.okx_api_secret:
            return
        import json
        body = json.dumps({"instId": inst_id, "lever": "1", "mgnMode": "cross"})
        request_path = "/api/v5/account/set-leverage"
        timestamp = self._timestamp()
        try:
            time.sleep(0.1)  # rate-limit guard
            resp = requests.post(
                f"{self.BASE_URL}{request_path}",
                headers=self._headers(timestamp, "POST", request_path, body),
                data=body,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == "0":
                self._leverage_set.add(inst_id)
                log("info", f"OKX leverage set to 1x for {inst_id}")
            else:
                log("warn", f"OKX set leverage returned code {result.get('code')}: {result.get('msg')}")
        except requests.exceptions.HTTPError as exc:
            body_text = exc.response.text if exc.response is not None else ""
            log("warn", f"Failed to set OKX leverage for {inst_id}: {exc} — {body_text}")
        except Exception as exc:
            log("warn", f"Failed to set OKX leverage for {inst_id}: {exc}")

    # ── Position mode detection (cached) ───────────────────────────────

    def _ensure_pos_mode(self) -> str:
        """Fetch and cache the account's position mode.

        Returns "net_mode" or "long_short_mode". Determines whether closes use
        reduceOnly (net) or posSide (hedge). Falls back to net_mode on error.
        """
        if self._pos_mode is not None:
            return self._pos_mode
        if not env.okx_api_key or not env.okx_api_secret:
            self._pos_mode = "net_mode"
            return self._pos_mode
        path = "/api/v5/account/config"
        try:
            time.sleep(0.1)
            t = self._timestamp()
            resp = requests.get(
                f"{self.BASE_URL}{path}",
                headers=self._headers(t, "GET", path),
                timeout=10,
            )
            resp.raise_for_status()
            r = resp.json()
            if r.get("code") == "0" and r.get("data"):
                self._pos_mode = r["data"][0].get("posMode") or "net_mode"
                log("info", f"OKX posMode detected: {self._pos_mode}")
            else:
                self._pos_mode = "net_mode"
                log("warn", f"OKX posMode fetch returned code {r.get('code')}: {r.get('msg')} — defaulting to net_mode")
        except Exception as exc:
            self._pos_mode = "net_mode"
            log("warn", f"OKX posMode fetch failed ({exc}) — defaulting to net_mode")
        return self._pos_mode

    # ── Order query / polling ──────────────────────────────────────────

    def _query_order(self, inst_id: str, order_id: str) -> dict:
        """GET a single order's current state from OKX."""
        if not env.okx_api_key or not env.okx_api_secret:
            return {}
        time.sleep(0.1)  # rate-limit guard
        request_path = f"/api/v5/trade/order?instId={inst_id}&ordId={order_id}"
        timestamp = self._timestamp()
        resp = requests.get(
            f"{self.BASE_URL}{request_path}",
            headers=self._headers(timestamp, "GET", request_path),
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == "0" and result.get("data"):
            return result["data"][0]
        return {}

    def _poll_fill(self, inst_id: str, order_id: str,
                   max_attempts: int = 8, delay: float = 0.4) -> dict | None:
        """Poll order status until filled or max attempts reached."""
        for i in range(max_attempts):
            time.sleep(delay)
            try:
                data = self._query_order(inst_id, order_id)
                state = data.get("state", "")
                if state == "filled":
                    return data
                if state in ("canceled", "rejected"):
                    log("warn", f"OKX order {order_id} ended with state {state}")
                    return data
            except Exception as exc:
                log("warn", f"OKX poll attempt {i+1}/{max_attempts} failed for order {order_id}: {exc}")
        return None

    # ── Core order placement ───────────────────────────────────────────

    def _place_order(self, symbol: str, position_id: str, side: str,
                     quantity: float, market_price: float,
                     reduce_only: bool = False,
                     attach_sl_px: float | None = None,
                     attach_tp_px: float | None = None) -> Trade:
        """Place a market order on OKX and return a Trade.

        Accepts quantity in base currency (same signature as BinanceProvider),
        converts to OKX contracts internally.

        reduce_only: when True (closing a position), guarantees the order only
        reduces an existing position. In net mode this sets reduceOnly=true.
        In hedge mode (long_short_mode), the correct posSide is set instead
        (OKX rejects reduceOnly with posSide together).

        attach_sl_px / attach_tp_px: when both are provided on an OPENING order,
        attach server-side SL+TP triggers (OCO) via attachAlgoOrds. OKX fires
        these atomically with the entry — they survive engine crashes / VM
        reboots. Both order prices are set to -1 (market on trigger). Trigger
        type is "last" (last-traded price). Pass None on closes.
        """
        import json

        inst_id = self._to_okx_inst_id(symbol)
        self._ensure_leverage(inst_id)
        pos_mode = self._ensure_pos_mode()

        if not env.okx_api_key or not env.okx_api_secret:
            log("error", f"OKX API keys not configured for {symbol}", symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), "OKX API keys not configured")

        # Convert base-currency quantity to contracts
        info = self._instruments.get(inst_id)
        ct_val = info["ctVal"] if info else 1.0
        contracts = self._round_contracts(inst_id, quantity / ct_val)
        if contracts <= 0:
            return _failed_trade(position_id, symbol, side.lower(),
                                 f"Invalid contract count after rounding: {contracts}")
        sz = str(contracts)

        # clOrdId acts as idempotency key — replays of the same params return
        # error 51000 ("duplicate clOrdId"), which is safe to treat as already-filled.
        # OKX clOrdId max length is 32 chars, alphanumeric only.
        cl_ord_id = "".join(c for c in position_id if c.isalnum())[:32]

        order_body: dict = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side.lower(),
            "ordType": "market",
            "sz": sz,
            "clOrdId": cl_ord_id,
        }

        if pos_mode == "long_short_mode":
            # Hedge mode: posSide tells OKX which side this order targets. For a
            # close, posSide must match the side being reduced (long for sell-to-close,
            # short for buy-to-close). For an open, it matches the new direction.
            if reduce_only:
                order_body["posSide"] = "long" if side.lower() == "sell" else "short"
            else:
                order_body["posSide"] = "long" if side.lower() == "buy" else "short"
        else:
            # Net mode: reduceOnly prevents flipping to opposite side on partial fills.
            if reduce_only:
                order_body["reduceOnly"] = True

        # Attach OCO SL/TP if this is an opening order with both prices supplied.
        # OKX places these atomically with the entry — server-side guarantees
        # SL/TP survive engine crashes. Skipped on closes (reduce_only=True).
        if not reduce_only and attach_sl_px is not None and attach_tp_px is not None \
                and attach_sl_px > 0 and attach_tp_px > 0:
            order_body["attachAlgoOrds"] = [{
                "attachAlgoClOrdId": f"sl{cl_ord_id}"[:32],
                "tpTriggerPx": f"{attach_tp_px:.8f}",
                "tpOrdPx": "-1",  # market on trigger
                "tpTriggerPxType": "last",
                "slTriggerPx": f"{attach_sl_px:.8f}",
                "slOrdPx": "-1",
                "slTriggerPxType": "last",
            }]

        body = json.dumps(order_body)
        request_path = "/api/v5/trade/order"
        timestamp = self._timestamp()

        try:
            time.sleep(0.1)  # rate-limit guard
            resp = requests.post(
                f"{self.BASE_URL}{request_path}",
                headers=self._headers(timestamp, "POST", request_path, body),
                data=body,
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("code") != "0":
                first = result.get("data", [{}])[0]
                err_code = first.get("sCode", result.get("code", ""))
                msg = first.get("sMsg", result.get("msg", "unknown error"))
                # Idempotency: 51000 = duplicate clOrdId. Means a previous attempt
                # already placed this order — query it instead of failing.
                if err_code == "51000":
                    log("warn", f"OKX duplicate clOrdId {cl_ord_id} — querying existing order", symbol=symbol)
                    existing = self._query_order_by_cl_ord_id(inst_id, cl_ord_id)
                    if existing.get("ordId"):
                        order_id = existing["ordId"]
                        # Fall through to fill polling below
                    else:
                        return _failed_trade(position_id, symbol, side.lower(),
                                             f"OKX clOrdId conflict but no order found: {msg}")
                else:
                    safe_msg = f"OKX {side} order rejected ({err_code}): {msg}"
                    log("error", safe_msg, symbol=symbol)
                    return _failed_trade(position_id, symbol, side.lower(), safe_msg)
            else:
                order_id = result["data"][0].get("ordId", "")

            # Query fill details
            fill_data = self._query_order(inst_id, order_id) if order_id else {}
            state = fill_data.get("state", "")
            avg_price = float(fill_data.get("avgPx", 0)) if fill_data else 0
            filled_sz = float(fill_data.get("fillSz", 0)) if fill_data else 0

            # Poll if not immediately filled
            if state != "filled" and order_id:
                log("info", f"OKX order {order_id} state={state}, polling for fill...", symbol=symbol)
                poll_data = self._poll_fill(inst_id, order_id)
                if poll_data:
                    avg_price = float(poll_data.get("avgPx", avg_price))
                    filled_sz = float(poll_data.get("fillSz", filled_sz))
                    state = poll_data.get("state", state)

            # Fallback to market_price / requested size
            if avg_price <= 0:
                avg_price = market_price
            if filled_sz <= 0:
                filled_sz = float(sz)

            # Convert filled contracts to base currency quantity for reporting
            filled_base_qty = self._contracts_to_base_qty(inst_id, filled_sz)

            if state not in ("filled", ""):
                log("warn", f"OKX order {order_id} final state: {state}", symbol=symbol)

            log("trade", f"OKX {side} filled: {inst_id} {filled_sz} contracts @ {avg_price}",
                symbol=symbol, data={"order_id": order_id, "avg_price": avg_price})

            return Trade(
                id=str(uuid.uuid4()), position_id=position_id, side=side.lower(),
                symbol=symbol, quantity=filled_base_qty,
                size_usd=filled_base_qty * avg_price, price=avg_price,
                order_id=str(order_id),
                status="filled", paper_trading=False,
                placed_at=time.time() * 1000,
            )
        except requests.exceptions.HTTPError as exc:
            body_text = exc.response.text if exc.response is not None else "no response"
            safe_msg = f"OKX {side} order failed: {exc.response.status_code if exc.response else '?'} {body_text}"
            log("error", safe_msg, symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), safe_msg)
        except Exception as exc:
            safe_msg = f"OKX {side} order failed ({type(exc).__name__}: {exc})"
            log("error", safe_msg, symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), safe_msg)

    def _query_order_by_cl_ord_id(self, inst_id: str, cl_ord_id: str) -> dict:
        """Look up an order by client order ID (used for idempotency recovery)."""
        if not env.okx_api_key or not env.okx_api_secret:
            return {}
        time.sleep(0.1)
        path = f"/api/v5/trade/order?instId={inst_id}&clOrdId={cl_ord_id}"
        try:
            resp = requests.get(
                f"{self.BASE_URL}{path}",
                headers=self._headers(self._timestamp(), "GET", path),
                timeout=10,
            )
            resp.raise_for_status()
            r = resp.json()
            if r.get("code") == "0" and r.get("data"):
                return r["data"][0]
        except Exception as exc:
            log("warn", f"OKX clOrdId lookup failed: {exc}")
        return {}

    # ── Public interface ───────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "okx"

    def buy(self, symbol: str, product_id: str, size_usd: float,
            position_id: str, market_price: float,
            attach_sl_px: float | None = None,
            attach_tp_px: float | None = None) -> Trade:
        if market_price <= 0 or size_usd <= 0:
            return _failed_trade(position_id, symbol, "buy", "Invalid price or size")
        quantity = size_usd / market_price
        return self._place_order(symbol, position_id, "buy", quantity, market_price,
                                 reduce_only=False,
                                 attach_sl_px=attach_sl_px, attach_tp_px=attach_tp_px)

    def sell(self, symbol: str, product_id: str, quantity: float,
             position_id: str, market_price: float,
             reduce_only: bool = True) -> Trade:
        # Default reduce_only=True for sells: this provider only opens longs,
        # so a sell is always a close. Callers opening a real short can pass False.
        if quantity <= 0:
            return _failed_trade(position_id, symbol, "sell", "Invalid quantity")
        return self._place_order(symbol, position_id, "sell", quantity, market_price,
                                 reduce_only=reduce_only)

    def get_balances(self) -> dict[str, float]:
        if not env.okx_api_key or not env.okx_api_secret:
            return {}

        request_path = "/api/v5/account/balance"
        timestamp = self._timestamp()

        try:
            time.sleep(0.1)  # rate-limit guard
            resp = requests.get(
                f"{self.BASE_URL}{request_path}",
                headers=self._headers(timestamp, "GET", request_path),
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != "0":
                log("warn", f"OKX balance request returned code {result.get('code')}: {result.get('msg')}")
                return {}
            balances: dict[str, float] = {}
            for account in result.get("data", []):
                for detail in account.get("details", []):
                    ccy = detail.get("ccy", "")
                    avail = float(detail.get("availBal", 0))
                    if avail > 0:
                        balances[ccy] = avail
            return balances
        except Exception as exc:
            log("warn", f"OKX balance fetch failed: {exc}")
            return {}
