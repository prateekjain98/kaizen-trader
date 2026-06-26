"""Binance SPOT execution provider (api/v3) — the LONG leg of the delta-neutral
funding carry.

The engine is otherwise futures-only (`BinanceProvider` hits fapi). Harvesting
positive funding safely means holding an equal-notional LONG on spot to cancel
the SHORT perp's price exposure, so the funding payment is the only P&L driver.
This module adds the spot order path; the pairing/hedging logic lives in
`neutral_carry.py`.

Spot differs from futures in three ways that matter here:
  * host is api.binance.com/api/v3, not fapi.binance.com
  * there is no leverage and no reduceOnly — a SELL is bounded by held balance
  * MARKET BUYs can be sized by quoteOrderQty (USD) instead of base quantity

DEFAULT-OFF capability: nothing here runs unless the caller (gated by
ENABLE_FUNDING_CARRY_NEUTRAL) constructs and invokes it.
"""
from __future__ import annotations

import hmac
import math
import time
import uuid

import requests

from src.config import env
from src.storage.database import log
from src.types import Trade
from src.utils.binance_symbols import to_binance_ticker


def _failed_spot_trade(position_id: str, symbol: str, side: str, error: str) -> Trade:
    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side=side.lower(),
        symbol=symbol, quantity=0, size_usd=0, price=0,
        status="failed", paper_trading=False,
        placed_at=time.time() * 1000, error=error,
    )


class BinanceSpotProvider:
    """Binance SPOT market-order execution. HMAC-signed; mirrors the futures
    provider's signing/rounding so behaviour is consistent across legs."""

    SPOT_BASE = "https://api.binance.com"

    def __init__(self) -> None:
        self._step_sizes: dict[str, float] = {}
        self._min_qty: dict[str, float] = {}
        self._min_notional: dict[str, float] = {}
        self._exchange_info_loaded: bool = False
        self._load_exchange_info()

    @property
    def name(self) -> str:
        return "binance_spot"

    def _get_binance_symbol(self, symbol: str) -> str:
        return to_binance_ticker(symbol) or f"{symbol.upper()}USDT"

    def _load_exchange_info(self) -> None:
        """Cache spot LOT_SIZE / MIN_NOTIONAL once for all USDT pairs."""
        if self._exchange_info_loaded:
            return
        try:
            resp = requests.get(f"{self.SPOT_BASE}/api/v3/exchangeInfo", timeout=15)
            resp.raise_for_status()
            for sym_info in resp.json().get("symbols", []):
                symbol = sym_info.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                for f in sym_info.get("filters", []):
                    ftype = f.get("filterType")
                    if ftype == "LOT_SIZE":
                        self._step_sizes[symbol] = float(f["stepSize"])
                        self._min_qty[symbol] = float(f.get("minQty", 0))
                    # spot uses NOTIONAL (newer) or MIN_NOTIONAL (legacy)
                    elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                        self._min_notional[symbol] = float(
                            f.get("minNotional", f.get("notional", 0)))
            self._exchange_info_loaded = True
            log("info", f"Binance SPOT exchange info cached: {len(self._step_sizes)} USDT pairs")
        except Exception as exc:  # noqa: BLE001
            log("warn", f"Failed to load Binance SPOT exchange info: {exc}")

    def _round_step(self, binance_symbol: str, qty: float) -> float:
        """Floor quantity to the symbol's LOT_SIZE step, scrubbing float drift.
        Same logic as the futures provider (see its docstring for the 148.5/0.1
        epsilon rationale)."""
        step = self._step_sizes.get(binance_symbol)
        if not step or step <= 0:
            return qty
        rounded = math.floor(qty / step + 1e-9) * step
        decimals = 0 if step >= 1 else max(0, -int(math.floor(math.log10(step))))
        return round(rounded, decimals)

    def _sign_and_post(self, params: str) -> dict:
        """Sign params with HMAC and POST to the SPOT order endpoint."""
        if not env.binance_api_secret or not env.binance_api_key:
            raise ValueError("Binance API keys not configured")
        time.sleep(0.1)  # rate-limit guard
        if "recvWindow=" not in params:
            params = f"{params}&recvWindow=10000"
        signature = hmac.new(
            env.binance_api_secret.encode(), params.encode(), "sha256"
        ).hexdigest()
        resp = requests.post(
            f"{self.SPOT_BASE}/api/v3/order",
            headers={"X-MBX-APIKEY": env.binance_api_key},
            data=f"{params}&signature={signature}",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def place_spot_market(self, symbol: str, position_id: str, side: str,
                          quantity: float, market_price: float) -> Trade:
        """Place a SPOT MARKET order. `side` is "BUY" or "SELL".

        Validates LOT_SIZE / MIN_NOTIONAL client-side first so we never burn an
        API call (or a rate-limit hit) on an order Binance will reject. Returns a
        standardized Trade; status "filled" on success, "failed" otherwise.
        """
        side = side.upper()
        binance_symbol = self._get_binance_symbol(symbol)

        if not env.binance_api_key or not env.binance_api_secret:
            log("error", f"Binance SPOT keys not configured for {symbol}", symbol=symbol)
            return _failed_spot_trade(position_id, symbol, side, "Binance API keys not configured")

        quantity = self._round_step(binance_symbol, quantity)
        if quantity <= 0:
            return _failed_spot_trade(position_id, symbol, side,
                                      f"Invalid quantity after rounding: {quantity}")

        min_qty = self._min_qty.get(binance_symbol, 0)
        min_notional = self._min_notional.get(binance_symbol, 0)
        if min_qty and quantity < min_qty:
            return _failed_spot_trade(position_id, symbol, side,
                f"qty {quantity} below minQty {min_qty} for {binance_symbol}")
        if min_notional and (quantity * market_price) < min_notional:
            return _failed_spot_trade(position_id, symbol, side,
                f"notional ${quantity * market_price:.2f} below MIN_NOTIONAL ${min_notional} for {binance_symbol}")

        timestamp = int(time.time() * 1000)
        params = (
            f"symbol={binance_symbol}&side={side}&type=MARKET"
            f"&quantity={quantity}&timestamp={timestamp}"
            f"&newClientOrderId={position_id[:36]}"
        )
        try:
            data = self._sign_and_post(params)
        except Exception as exc:  # noqa: BLE001
            log("warn", f"SPOT {side} order failed {symbol}: {exc}", symbol=symbol)
            return _failed_spot_trade(position_id, symbol, side, str(exc))

        order_id = data.get("orderId")
        filled_qty = float(data.get("executedQty", 0) or 0)
        quote_qty = float(data.get("cummulativeQuoteQty", 0) or 0)
        avg_price = (quote_qty / filled_qty) if filled_qty else market_price
        commission = sum(float(f.get("commission", 0) or 0) for f in data.get("fills", []))

        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side=side.lower(),
            symbol=symbol, quantity=filled_qty or quantity,
            size_usd=quote_qty or (quantity * avg_price), price=avg_price,
            status="filled", paper_trading=False,
            placed_at=time.time() * 1000, commission=commission,
            order_id=str(order_id) if order_id is not None else None,
        )

    def get_spot_balances(self) -> dict[str, float]:
        """Return non-zero free spot balances keyed by asset."""
        if not env.binance_api_key or not env.binance_api_secret:
            return {}
        timestamp = int(time.time() * 1000)
        params = f"timestamp={timestamp}&recvWindow=10000"
        sig = hmac.new(env.binance_api_secret.encode(), params.encode(), "sha256").hexdigest()
        try:
            resp = requests.get(
                f"{self.SPOT_BASE}/api/v3/account",
                headers={"X-MBX-APIKEY": env.binance_api_key},
                params={"timestamp": timestamp, "recvWindow": 10000, "signature": sig},
                timeout=10,
            )
            resp.raise_for_status()
            out: dict[str, float] = {}
            for b in resp.json().get("balances", []):
                free = float(b.get("free", 0) or 0)
                if free > 0:
                    out[b["asset"]] = free
            return out
        except Exception as exc:  # noqa: BLE001
            log("warn", f"Failed to fetch SPOT balances: {exc}")
            return {}
