"""Derivatives data — futures basis, open interest, funding from Binance.

Monitors futures premium over spot, OI dynamics, and funding rates
to detect overheated markets (extreme longs/shorts).
All Binance endpoints are public (no auth needed).
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log

_CACHE_TTL_MS = 120_000  # 2 minutes
_FAPI_BASE = "https://fapi.binance.com/fapi/v1"
_SPOT_BASE = "https://api.binance.com/api/v3"

_lock = threading.Lock()
_cache: dict[str, tuple["DerivativesData", float]] = {}
_breaker = CircuitBreaker("binance_derivatives", failure_threshold=3, reset_timeout_s=300)

# Map our symbols to Binance perpetual tickers
_BINANCE_PERP_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "UNI": "UNIUSDT",
    "AAVE": "AAVEUSDT",
    "ARB": "ARBUSDT",
    "OP": "OPUSDT",
    "DOGE": "DOGEUSDT",
    "SUI": "SUIUSDT",
    "APT": "APTUSDT",
    "SEI": "SEIUSDT",
    "INJ": "INJUSDT",
    "FET": "FETUSDT",
}


@dataclass
class DerivativesData:
    symbol: str
    futures_basis_pct: float        # (perp_price - spot_price) / spot_price * 100
    open_interest_usd: float        # total OI in USD
    funding_rate: float             # current funding rate
    mark_price: float               # mark price from premium index
    index_price: float              # underlying index price


def fetch_derivatives_data(symbol: str) -> Optional[DerivativesData]:
    """Fetch futures/derivatives data from Binance."""
    perp_ticker = _BINANCE_PERP_MAP.get(symbol.upper())
    if not perp_ticker:
        return None

    now = time.time() * 1000

    with _lock:
        cached = _cache.get(symbol)
        if cached and (now - cached[1]) < _CACHE_TTL_MS:
            return cached[0]

    if not _breaker.can_call():
        with _lock:
            c = _cache.get(symbol)
            return c[0] if c else None

    try:
        # Fetch premium index (includes funding rate, mark price, index price)
        premium_resp = requests.get(
            f"{_FAPI_BASE}/premiumIndex",
            params={"symbol": perp_ticker},
            timeout=10,
        )
        premium_resp.raise_for_status()
        premium = premium_resp.json()

        # Fetch open interest
        oi_resp = requests.get(
            f"{_FAPI_BASE}/openInterest",
            params={"symbol": perp_ticker},
            timeout=10,
        )
        oi_resp.raise_for_status()
        oi_data = oi_resp.json()

        # Fetch spot price
        spot_resp = requests.get(
            f"{_SPOT_BASE}/ticker/price",
            params={"symbol": perp_ticker},
            timeout=10,
        )
        spot_resp.raise_for_status()
        spot_data = spot_resp.json()

        _breaker.record_success()
    except Exception as err:
        _breaker.record_failure()
        log("warn", f"Binance derivatives fetch failed for {symbol}: {err}", symbol=symbol)
        with _lock:
            c = _cache.get(symbol)
            return c[0] if c else None

    try:
        mark_price = float(premium.get("markPrice", 0))
        index_price = float(premium.get("indexPrice", 0))
        funding_rate = float(premium.get("lastFundingRate", 0))
        oi_quantity = float(oi_data.get("openInterest", 0))
        spot_price = float(spot_data.get("price", 0))

        # Compute basis as premium of mark over spot
        if spot_price > 0:
            basis_pct = ((mark_price - spot_price) / spot_price) * 100
        else:
            basis_pct = 0

        # OI in USD
        oi_usd = oi_quantity * mark_price

        data = DerivativesData(
            symbol=symbol,
            futures_basis_pct=basis_pct,
            open_interest_usd=oi_usd,
            funding_rate=funding_rate,
            mark_price=mark_price,
            index_price=index_price,
        )

        with _lock:
            _cache[symbol] = (data, now)

        return data
    except (ValueError, TypeError, KeyError) as err:
        log("warn", f"Binance derivatives parse error for {symbol}: {err}", symbol=symbol)
        return None


def is_overheated(symbol: str) -> bool:
    """Check if the market is overheated (high basis + positive funding + high OI).

    Overheated = too many leveraged longs, correction risk.
    """
    data = fetch_derivatives_data(symbol)
    if data is None:
        return False
    # Basis > 0.5% + positive funding = lots of leveraged longs
    return data.futures_basis_pct > 0.5 and data.funding_rate > 0.0001


def is_funding_extreme(symbol: str) -> Optional[str]:
    """Check if funding rate is extreme. Returns 'long_crowded' or 'short_crowded' or None."""
    data = fetch_derivatives_data(symbol)
    if data is None:
        return None
    if data.funding_rate > 0.001:  # >0.1% per 8h
        return "long_crowded"
    if data.funding_rate < -0.001:
        return "short_crowded"
    return None
