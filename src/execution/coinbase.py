"""Coinbase Advanced Trade REST executor."""

import hashlib
import hmac
import json
import time
import uuid

import requests

from src.config import env
from src.storage.database import log
from src.types import Trade

BASE_URL = "https://api.coinbase.com"


class InsufficientFundsError(Exception):
    pass


class ExchangeError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _sign(timestamp: int, method: str, path: str, body: str) -> str:
    message = f"{timestamp}{method.upper()}{path}{body}"
    return hmac.new(
        (env.coinbase_api_secret or "").encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def _cb_request(method: str, path: str, body: dict | None = None) -> dict:
    if not env.coinbase_api_key or not env.coinbase_api_secret:
        raise ExchangeError("MISSING_CREDENTIALS", "Coinbase API key/secret not configured")

    timestamp = int(time.time())
    body_str = json.dumps(body) if body else ""
    signature = _sign(timestamp, method, path, body_str)

    headers = {
        "CB-ACCESS-KEY": env.coinbase_api_key,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": str(timestamp),
        "Content-Type": "application/json",
    }

    res = requests.request(method, f"{BASE_URL}{path}", headers=headers,
                           data=body_str if body_str else None, timeout=10)

    try:
        parsed = res.json()
    except Exception:
        raise ExchangeError("PARSE_ERROR", f"Non-JSON response ({res.status_code}): {res.text[:200]}")

    if not res.ok:
        raise ExchangeError(parsed.get("error", "HTTP_ERROR"),
                            parsed.get("message", f"HTTP {res.status_code}"))
    return parsed


def place_buy_order(product_id: str, size_usd: float, position_id: str) -> Trade:
    body = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {"quote_size": f"{size_usd:.2f}"},
        },
    }
    symbol = product_id.replace("-USD", "")
    log("info", f"Placing BUY order: {product_id} ${size_usd}", symbol=symbol,
        data={"product_id": product_id, "size_usd": size_usd, "position_id": position_id})

    response = _cb_request("POST", "/api/v3/brokerage/orders", body)

    if not response.get("success") or not response.get("order_id"):
        reason = (response.get("failure_reason")
                  or (response.get("error_response") or {}).get("preview_failure_reason")
                  or "unknown")
        if "INSUFFICIENT_FUND" in reason:
            raise InsufficientFundsError(f"Insufficient funds to buy {product_id} for ${size_usd}")
        raise ExchangeError(reason, f"Order failed: {reason}")

    order = response.get("order", {})
    avg_price = float(order.get("average_filled_price", 0))
    filled_size = float(order.get("filled_size", size_usd / avg_price if avg_price else 0))

    log("trade", f"BUY filled: {product_id} ${size_usd} @ avg {avg_price:.4f}", symbol=symbol,
        data={"order_id": response["order_id"], "avg_price": avg_price, "filled_size": filled_size})

    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side="buy",
        symbol=symbol, quantity=filled_size, size_usd=size_usd,
        price=avg_price, order_id=response["order_id"],
        status="filled", paper_trading=False, placed_at=time.time() * 1000,
    )


def place_sell_order(product_id: str, quantity: float, position_id: str) -> Trade:
    body = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {
            "market_market_ioc": {"base_size": f"{quantity:.8f}"},
        },
    }
    symbol = product_id.replace("-USD", "")
    log("info", f"Placing SELL order: {product_id} {quantity} units", symbol=symbol,
        data={"product_id": product_id, "quantity": quantity, "position_id": position_id})

    response = _cb_request("POST", "/api/v3/brokerage/orders", body)

    if not response.get("success") or not response.get("order_id"):
        reason = response.get("failure_reason", "unknown")
        raise ExchangeError(reason, f"Sell order failed: {reason}")

    order = response.get("order", {})
    avg_price = float(order.get("average_filled_price", 0))

    log("trade", f"SELL filled: {product_id} {quantity} units @ avg {avg_price:.4f}", symbol=symbol,
        data={"order_id": response["order_id"], "avg_price": avg_price})

    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side="sell",
        symbol=symbol, quantity=quantity, size_usd=quantity * avg_price,
        price=avg_price, order_id=response["order_id"],
        status="filled", paper_trading=False, placed_at=time.time() * 1000,
    )


def get_account_balances() -> dict[str, float]:
    res = _cb_request("GET", "/api/v3/brokerage/accounts?limit=250")
    return {
        a["currency"]: float(a["available_balance"]["value"])
        for a in res.get("accounts", [])
        if float(a["available_balance"]["value"]) > 0
    }
