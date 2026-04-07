"""Stablecoin flow tracking — capital inflow/outflow indicator.

Monitors USDT and USDC market cap changes as a proxy for capital entering
or exiting crypto markets. Growing stablecoin supply = capital inflow = bullish.
Data from CoinGecko free API.
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log

_CACHE_TTL_MS = 1_800_000  # 30 minutes
_API_BASE = "https://api.coingecko.com/api/v3"

_lock = threading.Lock()
_cache: Optional[tuple["StablecoinFlows", float]] = None
_breaker = CircuitBreaker("coingecko_stablecoin", failure_threshold=3, reset_timeout_s=600)

_STABLECOINS = {
    "tether": "USDT",
    "usd-coin": "USDC",
}


@dataclass
class StablecoinFlows:
    total_stablecoin_mcap: float       # combined USDT + USDC market cap
    mcap_change_24h_pct: float         # 24-hour market cap change %
    mcap_change_7d_pct: float          # 7-day market cap change %
    usdt_dominance: float              # USDT share of total stablecoin mcap (0-1)
    usdt_mcap: float
    usdc_mcap: float


def fetch_stablecoin_flows() -> Optional[StablecoinFlows]:
    """Fetch stablecoin market cap data from CoinGecko."""
    global _cache
    now = time.time() * 1000

    with _lock:
        if _cache and (now - _cache[1]) < _CACHE_TTL_MS:
            return _cache[0]

    if not _breaker.can_call():
        with _lock:
            return _cache[0] if _cache else None

    mcap_data = {}

    for coin_id, ticker in _STABLECOINS.items():
        try:
            resp = requests.get(
                f"{_API_BASE}/coins/{coin_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            _breaker.record_success()

            market = data.get("market_data", {})
            mcap_data[ticker] = {
                "mcap": float(market.get("market_cap", {}).get("usd", 0)),
                "change_24h": float(market.get("market_cap_change_percentage_24h", 0)),
            }
        except Exception as err:
            _breaker.record_failure()
            log("warn", f"CoinGecko stablecoin fetch failed for {coin_id}: {err}")
            with _lock:
                return _cache[0] if _cache else None

    usdt = mcap_data.get("USDT", {"mcap": 0, "change_24h": 0})
    usdc = mcap_data.get("USDC", {"mcap": 0, "change_24h": 0})

    total = usdt["mcap"] + usdc["mcap"]
    if total == 0:
        return None

    # Weighted average 24h change
    weighted_24h = (
        (usdt["mcap"] * usdt["change_24h"] + usdc["mcap"] * usdc["change_24h"]) / total
    ) if total > 0 else 0

    # For 7d change, we approximate from 24h (CoinGecko free tier limitation)
    # A proper implementation would use /market_chart endpoint
    approx_7d = weighted_24h * 3  # rough approximation

    flows = StablecoinFlows(
        total_stablecoin_mcap=total,
        mcap_change_24h_pct=weighted_24h,
        mcap_change_7d_pct=approx_7d,
        usdt_dominance=usdt["mcap"] / total if total > 0 else 0,
        usdt_mcap=usdt["mcap"],
        usdc_mcap=usdc["mcap"],
    )

    with _lock:
        _cache = (flows, now)

    return flows


def is_capital_inflowing() -> bool:
    """Quick check: is stablecoin supply growing (capital entering crypto)?"""
    flows = fetch_stablecoin_flows()
    if flows is None:
        return False
    return flows.mcap_change_24h_pct > 0.05  # >0.05% daily growth threshold
