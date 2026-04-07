"""Binance Futures funding rate + open interest fetcher."""

import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log


@dataclass
class FundingData:
    symbol: str
    binance_symbol: str
    funding_rate: float
    next_funding_time: float
    open_interest_usd: float
    open_interest_change_24h: float
    sampled_at: float


_oi_history: dict[str, dict] = {}

SYMBOL_MAP: dict[str, str] = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "BNB": "BNBUSDT",
    "ARB": "ARBUSDT", "OP": "OPUSDT", "AVAX": "AVAXUSDT", "MATIC": "MATICUSDT",
    "LINK": "LINKUSDT", "UNI": "UNIUSDT", "AAVE": "AAVEUSDT", "DOGE": "DOGEUSDT",
    "SUI": "SUIUSDT", "APT": "APTUSDT", "SEI": "SEIUSDT", "TIA": "TIAUSDT",
}

_BASE = "https://fapi.binance.com"
_last_fetch_at: float = 0
_cached: list[FundingData] = []
_CACHE_TTL_MS = 300_000
_breaker = CircuitBreaker("funding")


def _compute_oi_change(symbol: str, current_oi: float) -> float:
    now = time.time() * 1000
    prev = _oi_history.get(symbol)
    _oi_history[symbol] = {"oi": current_oi, "ts": now}
    if not prev:
        return 0
    age_hours = (now - prev["ts"]) / 3_600_000
    if age_hours > 26:
        return 0
    return (current_oi - prev["oi"]) / prev["oi"] if prev["oi"] > 0 else 0


def fetch_funding_data(symbols: list[str]) -> list[FundingData]:
    global _last_fetch_at, _cached
    now = time.time() * 1000
    if now - _last_fetch_at < _CACHE_TTL_MS:
        return _cached

    # Staleness warning
    if _cached and _last_fetch_at > 0 and now - _last_fetch_at > 2 * _CACHE_TTL_MS:
        log("warn", f"Funding data is stale (last fetch {(now - _last_fetch_at) / 60_000:.0f}m ago)")

    if not _breaker.can_call():
        log("warn", "Funding circuit breaker OPEN — returning cached data")
        return _cached

    binance_symbols = [SYMBOL_MAP[s] for s in symbols if s in SYMBOL_MAP]
    if not binance_symbols:
        return []

    results: list[FundingData] = []
    try:
        funding_res = requests.get(f"{_BASE}/fapi/v1/premiumIndex", timeout=8)
        if funding_res.status_code != 200:
            log("warn", f"Binance funding fetch failed: {funding_res.status_code}")
            _breaker.record_failure()
            return _cached

        all_funding = funding_res.json()
        funding_map = {f["symbol"]: f for f in all_funding}

        reverse_map = {v: k for k, v in SYMBOL_MAP.items()}

        for binance_sym in binance_symbols:
            sym = reverse_map.get(binance_sym)
            if not sym:
                continue
            funding = funding_map.get(binance_sym)
            if not funding:
                continue

            try:
                oi_res = requests.get(
                    f"{_BASE}/fapi/v1/openInterest?symbol={binance_sym}", timeout=5
                )
                if oi_res.status_code != 200:
                    continue
                oi = oi_res.json()
                oi_usd = float(oi["openInterest"])
                oi_change = _compute_oi_change(binance_sym, oi_usd)

                results.append(FundingData(
                    symbol=sym,
                    binance_symbol=binance_sym,
                    funding_rate=float(funding["lastFundingRate"]),
                    next_funding_time=int(funding["nextFundingTime"]),
                    open_interest_usd=oi_usd,
                    open_interest_change_24h=oi_change,
                    sampled_at=now,
                ))
            except Exception:
                continue

        _breaker.record_success()
    except Exception as err:
        log("warn", f"Binance funding network error: {err}")
        _breaker.record_failure()
        return _cached

    _last_fetch_at = now
    _cached = results
    return results
