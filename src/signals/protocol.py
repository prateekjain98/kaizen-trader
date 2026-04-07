"""DeFiLlama protocol revenue fetcher."""

import re
import time
from dataclasses import dataclass

import requests

from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log


@dataclass
class ProtocolRevenueData:
    protocol: str
    symbol: str
    revenue_24h: float
    revenue_7d_avg: float
    revenue_multiple: float
    sampled_at: float


PROTOCOL_SYMBOL_MAP: dict[str, str] = {
    "uniswap": "UNI", "aave": "AAVE", "curve": "CRV", "compound": "COMP",
    "lido": "LDO", "makerdao": "MKR", "synthetix": "SNX", "gmx": "GMX",
    "dydx": "DYDX", "ondo": "ONDO", "maple": "MPL",
}

_cached: list[ProtocolRevenueData] = []
_last_fetch_at: float = 0
_CACHE_TTL_MS = 3_600_000
_breaker = CircuitBreaker("protocol")


def _resolve_symbol(protocol: dict) -> str | None:
    name = re.sub(r"[^a-z0-9]", "", protocol.get("name", "").lower())
    if name in PROTOCOL_SYMBOL_MAP:
        return PROTOCOL_SYMBOL_MAP[name]
    sym = protocol.get("symbol")
    if sym:
        return sym.upper()
    return None


def fetch_protocol_revenue() -> list[ProtocolRevenueData]:
    global _cached, _last_fetch_at
    now = time.time() * 1000
    if now - _last_fetch_at < _CACHE_TTL_MS:
        return _cached

    # Staleness warning
    if _cached and _last_fetch_at > 0 and now - _last_fetch_at > 2 * _CACHE_TTL_MS:
        log("warn", f"Protocol revenue data is stale (last fetch {(now - _last_fetch_at) / 60_000:.0f}m ago)")

    if not _breaker.can_call():
        log("warn", "Protocol revenue circuit breaker OPEN — returning cached data")
        return _cached

    try:
        res = requests.get(
            "https://api.llama.fi/overview/fees?excludeTotalDataChartBreakdown=true",
            timeout=15,
        )
        if res.status_code != 200:
            log("warn", f"DeFiLlama fees fetch failed: {res.status_code}")
            _breaker.record_failure()
            return _cached

        data = res.json()
        results: list[ProtocolRevenueData] = []

        for protocol in data.get("protocols", []):
            if protocol.get("disabled"):
                continue
            total_24h = protocol.get("total24h")
            total_7d = protocol.get("total7d")
            if not total_24h or not total_7d:
                continue
            if total_24h < 1000:
                continue

            symbol = _resolve_symbol(protocol)
            if not symbol:
                continue

            revenue_7d_avg = total_7d / 7
            revenue_multiple = total_24h / revenue_7d_avg if revenue_7d_avg > 0 else 0

            results.append(ProtocolRevenueData(
                protocol=protocol.get("displayName") or protocol["name"],
                symbol=symbol,
                revenue_24h=total_24h,
                revenue_7d_avg=revenue_7d_avg,
                revenue_multiple=revenue_multiple,
                sampled_at=now,
            ))

        results.sort(key=lambda x: x.revenue_multiple, reverse=True)
        _last_fetch_at = now
        _cached = results
        _breaker.record_success()
        log("info", f"DeFiLlama: loaded {len(results)} protocol revenue records")
        return results

    except Exception as err:
        log("warn", f"DeFiLlama network error: {err}")
        _breaker.record_failure()
        return _cached
