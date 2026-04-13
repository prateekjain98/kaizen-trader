"""Token unlock schedule fetcher — supply shock risk filter.

Upcoming large unlocks (>2% of supply within 7 days) are bearish signals.
Data from TokenUnlocks.app public API.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log

_CACHE_TTL_MS = 3_600_000  # 1 hour
_API_BASE = "https://tokenomist.ai/api/v2"
_UNLOCK_RISK_DAYS = 7
_UNLOCK_RISK_PCT = 2.0  # >2% of supply = risk
# Base-layer tokens never have VC unlock events — skip to avoid 404 spam
_SKIP_SYMBOLS = {"BTC", "ETH", "DOGE", "LTC"}

_lock = threading.Lock()
_cache: dict[str, tuple[list["TokenUnlock"], float]] = {}
_breaker = CircuitBreaker("token_unlocks", failure_threshold=3, reset_timeout_s=600)


@dataclass
class TokenUnlock:
    symbol: str
    unlock_date: str           # ISO date string
    unlock_amount_usd: float
    unlock_pct_supply: float   # percentage of total supply being unlocked
    days_until_unlock: int


def fetch_token_unlocks(symbol: str) -> list[TokenUnlock]:
    """Fetch upcoming token unlock events for a symbol."""
    if symbol.upper() in _SKIP_SYMBOLS:
        return []
    if not re.fullmatch(r"[A-Za-z0-9]{1,20}", symbol):
        return []
    now = time.time() * 1000

    with _lock:
        cached = _cache.get(symbol)
        if cached and (now - cached[1]) < _CACHE_TTL_MS:
            return cached[0]

    if not _breaker.can_call():
        with _lock:
            return _cache.get(symbol, ([], 0))[0]

    try:
        resp = requests.get(
            f"{_API_BASE}/token/{symbol.lower()}/events",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        _breaker.record_success()
    except Exception as err:
        _breaker.record_failure()
        log("warn", f"Token unlocks fetch failed for {symbol}: {err}", symbol=symbol)
        with _lock:
            return _cache.get(symbol, ([], 0))[0]

    unlocks = []
    now_s = time.time()
    events = data if isinstance(data, list) else data.get("events", data.get("data", []))

    for event in events:
        try:
            unlock_ts = event.get("unlockDate") or event.get("date") or event.get("timestamp")
            if not unlock_ts:
                continue
            # Handle both epoch seconds and ISO strings
            if isinstance(unlock_ts, (int, float)):
                event_time = unlock_ts
            else:
                from datetime import datetime
                event_time = datetime.fromisoformat(unlock_ts.replace("Z", "+00:00")).timestamp()

            days_until = max(0, int((event_time - now_s) / 86400))
            if days_until > 14:
                continue  # only care about next 14 days

            pct_supply = float(event.get("percentOfSupply", event.get("pctSupply", 0)))
            amount_usd = float(event.get("valueUsd", event.get("unlockValueUsd", 0)))

            unlocks.append(TokenUnlock(
                symbol=symbol,
                unlock_date=str(unlock_ts),
                unlock_amount_usd=amount_usd,
                unlock_pct_supply=pct_supply,
                days_until_unlock=days_until,
            ))
        except (ValueError, TypeError, KeyError):
            continue

    unlocks.sort(key=lambda u: u.days_until_unlock)

    with _lock:
        _cache[symbol] = (unlocks, now)

    return unlocks


def get_upcoming_unlocks(symbols: list[str]) -> dict[str, list[TokenUnlock]]:
    """Get upcoming unlocks for multiple symbols."""
    result = {}
    for sym in symbols:
        unlocks = fetch_token_unlocks(sym)
        if unlocks:
            result[sym] = unlocks
    return result


def is_unlock_risk(symbol: str) -> bool:
    """Check if there's a significant unlock (>2% supply) within 7 days."""
    unlocks = fetch_token_unlocks(symbol)
    for u in unlocks:
        if u.days_until_unlock <= _UNLOCK_RISK_DAYS and u.unlock_pct_supply >= _UNLOCK_RISK_PCT:
            return True
    return False
