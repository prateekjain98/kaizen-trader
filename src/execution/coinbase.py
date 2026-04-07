"""Coinbase Advanced Trade REST executor."""

import hashlib
import hmac
import json
import threading
import time
import uuid

import requests

from src.config import env
from src.storage.database import log
from src.types import Trade

BASE_URL = "https://api.coinbase.com"

# Rate limiter state (guarded by _rate_lock)
_last_request_time: float = 0.0
_rate_lock = threading.Lock()
_MIN_REQUEST_INTERVAL: float = 0.1  # 100ms between requests

# Retry configuration
_MAX_RETRIES = 3
_BACKOFF_SECONDS = [1, 2, 4]


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


def _enforce_rate_limit() -> None:
    """Enforce minimum 100ms between API requests."""
    global _last_request_time
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _is_retryable(exc: Exception) -> bool:
    """Return True if the error is a timeout or 5xx (retryable)."""
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, ExchangeError) and exc.code.startswith("5"):
        return True
    return False


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

    _enforce_rate_limit()

    res = requests.request(method, f"{BASE_URL}{path}", headers=headers,
                           data=body_str if body_str else None, timeout=10)

    try:
        parsed = res.json()
    except (json.JSONDecodeError, ValueError) as e:
        log("error", f"Failed to parse JSON response: {e} (HTTP {res.status_code}): {res.text[:200]}")
        raise ExchangeError("PARSE_ERROR", f"Non-JSON response ({res.status_code}): {res.text[:200]}")

    if not res.ok:
        code = str(res.status_code)
        message = parsed.get("message", f"HTTP {res.status_code}")
        raise ExchangeError(code, message)
    return parsed


def _cb_request_with_retry(method: str, path: str, body: dict | None = None) -> dict:
    """Wrap _cb_request with retry + exponential backoff for transient errors."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return _cb_request(method, path, body)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise  # 4xx / client errors: don't retry
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_SECONDS[attempt]
                log("warn", f"Retryable error (attempt {attempt + 1}/{_MAX_RETRIES}), "
                    f"retrying in {wait}s: {exc}")
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _make_client_order_id(position_id: str, side: str, attempt: int) -> str:
    """Deterministic order ID per position + side + attempt to prevent duplicate orders on retry."""
    raw = f"{position_id}:{side}:{attempt}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, raw))


def place_buy_order(product_id: str, size_usd: float, position_id: str) -> Trade:
    symbol = product_id.replace("-USD", "")

    # Price sanity check
    if size_usd <= 0:
        log("error", f"Invalid buy size_usd={size_usd} for {product_id}", symbol=symbol)
        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="buy",
            symbol=symbol, quantity=0, size_usd=0,
            price=0, status="failed", paper_trading=False,
            placed_at=time.time() * 1000, error="Invalid size_usd <= 0",
        )

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        client_order_id = _make_client_order_id(position_id, "buy", attempt)
        body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {"quote_size": f"{size_usd:.2f}"},
            },
        }
        log("info", f"Placing BUY order: {product_id} ${size_usd} (attempt {attempt + 1})", symbol=symbol,
            data={"product_id": product_id, "size_usd": size_usd, "position_id": position_id,
                  "client_order_id": client_order_id})

        try:
            response = _cb_request("POST", "/api/v3/brokerage/orders", body)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_SECONDS[attempt]
                log("warn", f"Retryable error on BUY (attempt {attempt + 1}/{_MAX_RETRIES}), "
                    f"retrying in {wait}s: {exc}", symbol=symbol)
                time.sleep(wait)
                continue
            raise

        if not response.get("success") or not response.get("order_id"):
            reason = (response.get("failure_reason")
                      or (response.get("error_response") or {}).get("preview_failure_reason")
                      or "unknown")
            if "INSUFFICIENT_FUND" in reason:
                raise InsufficientFundsError(f"Insufficient funds to buy {product_id} for ${size_usd}")
            raise ExchangeError(reason, f"Order failed: {reason}")

        order = response.get("order", {})
        avg_price = float(order.get("average_filled_price", 0))
        expected_qty = size_usd / avg_price if avg_price else 0
        filled_size = float(order.get("filled_size", expected_qty))

        # Partial fill detection
        if avg_price > 0 and expected_qty > 0:
            fill_ratio = filled_size / expected_qty
            if fill_ratio < 0.99:
                log("warn",
                    f"Partial fill on BUY {product_id}: requested ~{expected_qty:.8f}, "
                    f"filled {filled_size:.8f} ({fill_ratio * 100:.1f}%)",
                    symbol=symbol,
                    data={"expected_qty": expected_qty, "filled_size": filled_size,
                          "fill_ratio": fill_ratio})

        actual_size_usd = filled_size * avg_price if avg_price else 0

        log("trade", f"BUY filled: {product_id} ${actual_size_usd:.2f} @ avg {avg_price:.4f}", symbol=symbol,
            data={"order_id": response["order_id"], "avg_price": avg_price, "filled_size": filled_size})

        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="buy",
            symbol=symbol, quantity=filled_size, size_usd=actual_size_usd,
            price=avg_price, order_id=response["order_id"],
            status="filled", paper_trading=False, placed_at=time.time() * 1000,
        )

    # Should not reach here, but just in case
    raise last_exc  # type: ignore[misc]


def place_sell_order(product_id: str, quantity: float, position_id: str) -> Trade:
    symbol = product_id.replace("-USD", "")

    # Price sanity check
    if quantity <= 0:
        log("error", f"Invalid sell quantity={quantity} for {product_id}", symbol=symbol)
        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="sell",
            symbol=symbol, quantity=0, size_usd=0,
            price=0, status="failed", paper_trading=False,
            placed_at=time.time() * 1000, error="Invalid quantity <= 0",
        )

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        client_order_id = _make_client_order_id(position_id, "sell", attempt)
        body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": "SELL",
            "order_configuration": {
                "market_market_ioc": {"base_size": f"{quantity:.8f}"},
            },
        }
        log("info", f"Placing SELL order: {product_id} {quantity} units (attempt {attempt + 1})", symbol=symbol,
            data={"product_id": product_id, "quantity": quantity, "position_id": position_id,
                  "client_order_id": client_order_id})

        try:
            response = _cb_request("POST", "/api/v3/brokerage/orders", body)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise
            if attempt < _MAX_RETRIES - 1:
                wait = _BACKOFF_SECONDS[attempt]
                log("warn", f"Retryable error on SELL (attempt {attempt + 1}/{_MAX_RETRIES}), "
                    f"retrying in {wait}s: {exc}", symbol=symbol)
                time.sleep(wait)
                continue
            raise

        if not response.get("success") or not response.get("order_id"):
            reason = response.get("failure_reason", "unknown")
            raise ExchangeError(reason, f"Sell order failed: {reason}")

        order = response.get("order", {})
        avg_price = float(order.get("average_filled_price", 0))
        filled_size = float(order.get("filled_size", quantity))

        # Partial fill detection
        if quantity > 0:
            fill_ratio = filled_size / quantity
            if fill_ratio < 0.99:
                log("warn",
                    f"Partial fill on SELL {product_id}: requested {quantity:.8f}, "
                    f"filled {filled_size:.8f} ({fill_ratio * 100:.1f}%)",
                    symbol=symbol,
                    data={"requested_qty": quantity, "filled_size": filled_size,
                          "fill_ratio": fill_ratio})

        actual_size_usd = filled_size * avg_price

        log("trade", f"SELL filled: {product_id} {filled_size} units @ avg {avg_price:.4f}", symbol=symbol,
            data={"order_id": response["order_id"], "avg_price": avg_price, "filled_size": filled_size})

        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="sell",
            symbol=symbol, quantity=filled_size, size_usd=actual_size_usd,
            price=avg_price, order_id=response["order_id"],
            status="filled", paper_trading=False, placed_at=time.time() * 1000,
        )

    # Should not reach here, but just in case
    raise last_exc  # type: ignore[misc]


def get_account_balances() -> dict[str, float]:
    res = _cb_request_with_retry("GET", "/api/v3/brokerage/accounts?limit=250")
    return {
        a["currency"]: float(a["available_balance"]["value"])
        for a in res.get("accounts", [])
        if float(a["available_balance"]["value"]) > 0
    }
