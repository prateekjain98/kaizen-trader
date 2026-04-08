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

from src.utils.binance_symbols import BINANCE_SYMBOL_MAP as _BINANCE_PERP_MAP


@dataclass
class LeverageBracket:
    """Long/short ratio for a specific leverage bracket."""
    bracket: str          # e.g. "10x", "20x", "50x"
    long_ratio: float     # fraction of longs (0-1)
    short_ratio: float    # fraction of shorts (0-1)
    long_short_ratio: float  # long_account / short_account


@dataclass
class LeverageProfile:
    """Aggregated leverage bracket data for a symbol."""
    symbol: str
    brackets: list[LeverageBracket]
    high_leverage_long_pct: float    # % of longs at >=20x
    high_leverage_short_pct: float   # % of shorts at >=20x
    top_trader_long_ratio: float     # top trader long/short ratio
    top_trader_short_ratio: float


@dataclass
class DerivativesData:
    symbol: str
    futures_basis_pct: float        # (perp_price - spot_price) / spot_price * 100
    open_interest_usd: float        # total OI in USD
    funding_rate: float             # current funding rate
    mark_price: float               # mark price from premium index
    index_price: float              # underlying index price
    leverage_profile: Optional["LeverageProfile"] = None


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

        if spot_price > 0:
            basis_pct = ((mark_price - spot_price) / spot_price) * 100
        else:
            basis_pct = 0

        oi_usd = oi_quantity * mark_price

        # Leverage profile is fetched separately (not on the hot path)
        # to avoid adding 2 more HTTP calls to per-signal scoring.
        # It is populated by fetch_leverage_profile() called from the
        # background signal refresh loop.
        raw_lev = _leverage_cache.get_raw(symbol)
        lev_profile = raw_lev[0] if raw_lev else None

        data = DerivativesData(
            symbol=symbol,
            futures_basis_pct=basis_pct,
            open_interest_usd=oi_usd,
            funding_rate=funding_rate,
            mark_price=mark_price,
            index_price=index_price,
            leverage_profile=lev_profile,
        )

        with _lock:
            _cache[symbol] = (data, now)

        return data
    except (ValueError, TypeError, KeyError) as err:
        log("warn", f"Binance derivatives parse error for {symbol}: {err}", symbol=symbol)
        return None


def _fetch_leverage_profile(symbol: str) -> Optional[LeverageProfile]:
    """Fetch long/short ratio by leverage bracket from Binance.

    Uses two endpoints:
    - /fapi/v1/topLongShortAccountRatio — top traders' positioning
    - /futures/data/globalLongShortAccountRatio — overall long/short ratio

    High leverage concentration on one side = liquidation cascade risk.
    """
    perp_ticker = _BINANCE_PERP_MAP.get(symbol.upper())
    if not perp_ticker:
        return None

    if not _breaker.can_call():
        return None

    try:
        # Top trader long/short ratio (most informative — "smart money")
        top_resp = requests.get(
            f"{_FAPI_BASE}/topLongShortAccountRatio",
            params={"symbol": perp_ticker, "period": "1h", "limit": 1},
            timeout=10,
        )
        top_resp.raise_for_status()
        top_data = top_resp.json()

        # Global long/short ratio
        global_resp = requests.get(
            f"{_FAPI_BASE}/globalLongShortAccountRatio",
            params={"symbol": perp_ticker, "period": "1h", "limit": 1},
            timeout=10,
        )
        global_resp.raise_for_status()
        global_data = global_resp.json()

        _breaker.record_success()
    except Exception as err:
        _breaker.record_failure()
        log("warn", f"Binance leverage profile fetch failed for {symbol}: {err}", symbol=symbol)
        return None

    try:
        top_entry = top_data[0] if top_data else {}
        global_entry = global_data[0] if global_data else {}

        top_long_ratio = float(top_entry.get("longAccount", 0.5))
        top_short_ratio = float(top_entry.get("shortAccount", 0.5))
        global_long_ratio = float(global_entry.get("longAccount", 0.5))
        global_short_ratio = float(global_entry.get("shortAccount", 0.5))

        # Derive leverage concentration heuristic:
        # If top traders are heavily one-sided vs global, the other side is retail leverage
        # High leverage longs = global_long > top_long (retail is more long than smart money)
        high_lev_long_pct = max(0.0, (global_long_ratio - top_long_ratio) * 100)
        high_lev_short_pct = max(0.0, (global_short_ratio - top_short_ratio) * 100)

        # Build bracket approximation from ratios
        brackets = [
            LeverageBracket(
                bracket="global",
                long_ratio=global_long_ratio,
                short_ratio=global_short_ratio,
                long_short_ratio=global_long_ratio / global_short_ratio if global_short_ratio > 0 else 1.0,
            ),
            LeverageBracket(
                bracket="top_traders",
                long_ratio=top_long_ratio,
                short_ratio=top_short_ratio,
                long_short_ratio=top_long_ratio / top_short_ratio if top_short_ratio > 0 else 1.0,
            ),
        ]

        return LeverageProfile(
            symbol=symbol,
            brackets=brackets,
            high_leverage_long_pct=high_lev_long_pct,
            high_leverage_short_pct=high_lev_short_pct,
            top_trader_long_ratio=top_long_ratio,
            top_trader_short_ratio=top_short_ratio,
        )
    except (ValueError, TypeError, IndexError, KeyError) as err:
        log("warn", f"Binance leverage profile parse error for {symbol}: {err}", symbol=symbol)
        return None


from src.utils.cache import TTLCache as _TTLCache

_leverage_cache: _TTLCache[str, LeverageProfile] = _TTLCache(ttl_s=300)


def fetch_leverage_profile(symbol: str) -> Optional[LeverageProfile]:
    """Fetch leverage profile with caching."""
    cached = _leverage_cache.get(symbol)
    if cached is not None:
        return cached

    profile = _fetch_leverage_profile(symbol)
    if profile:
        _leverage_cache.set(symbol, profile)

    return profile


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
