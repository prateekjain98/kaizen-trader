"""Concrete ExecutionProvider implementations wrapping existing executors."""

import hashlib
import hmac
import time
import uuid

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
    _leverage_set: set[str] = set()  # Track symbols with leverage already configured

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

    def _place_order(self, symbol: str, position_id: str, side: str,
                     quantity: float, market_price: float) -> Trade:
        """Shared order execution for both buy and sell."""
        binance_symbol = self._get_binance_symbol(symbol)
        self._ensure_leverage(binance_symbol)

        if not env.binance_api_key or not env.binance_api_secret:
            log("error", f"Binance API keys not configured for {symbol}", symbol=symbol)
            return _failed_trade(position_id, symbol, side.lower(), "Binance API keys not configured")

        if quantity <= 0:
            return _failed_trade(position_id, symbol, side.lower(), f"Invalid quantity: {quantity}")

        timestamp = int(time.time() * 1000)
        params = (
            f"symbol={binance_symbol}&side={side}&type=MARKET"
            f"&quantity={quantity:.8f}&timestamp={timestamp}"
            f"&newClientOrderId={position_id[:36]}"
        )

        try:
            data = self._sign_and_post(params)
            avg_price = float(data.get("avgPrice", market_price))
            filled_qty = float(data.get("executedQty", quantity))

            log("trade", f"Binance {side} filled: {binance_symbol} {filled_qty} @ {avg_price}",
                symbol=symbol, data={"order_id": data.get("orderId"), "avg_price": avg_price})

            return Trade(
                id=str(uuid.uuid4()), position_id=position_id, side=side.lower(),
                symbol=symbol, quantity=filled_qty,
                size_usd=filled_qty * avg_price, price=avg_price,
                order_id=str(data.get("orderId", "")),
                status="filled", paper_trading=False,
                placed_at=time.time() * 1000,
            )
        except requests.exceptions.HTTPError as exc:
            body = exc.response.text if exc.response is not None else "no response"
            safe_msg = f"Binance {side} order failed: {exc.response.status_code if exc.response else '?'} {body}"
            log("error", safe_msg, symbol=symbol)
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
             position_id: str, market_price: float) -> Trade:
        if quantity <= 0:
            return _failed_trade(position_id, symbol, "sell", "Invalid quantity")
        return self._place_order(symbol, position_id, "SELL", quantity, market_price)

    def get_balances(self) -> dict[str, float]:
        if not env.binance_api_key or not env.binance_api_secret:
            return {}

        timestamp = int(time.time() * 1000)
        params = f"timestamp={timestamp}"
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()

        try:
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
