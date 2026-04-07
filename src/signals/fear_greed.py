"""Fear & Greed Index fetcher (Alternative.me)."""

import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.storage.database import log
from src.types import MarketContext


@dataclass
class FearGreedReading:
    index: int
    label: str
    delta1d: int
    fetched_at: float


_cached: Optional[FearGreedReading] = None
_last_fetch_at: float = 0
_CACHE_TTL_MS = 30 * 60_000


def fetch_fear_greed() -> Optional[FearGreedReading]:
    global _cached, _last_fetch_at
    now = time.time() * 1000
    if _cached and now - _last_fetch_at < _CACHE_TTL_MS:
        return _cached

    try:
        res = requests.get("https://api.alternative.me/fng/?limit=2", timeout=5)
        if res.status_code != 200:
            log("warn", f"Fear & Greed fetch failed: {res.status_code}")
            return _cached
        data = res.json()
        items = data.get("data", [])
        if not items:
            return _cached

        today = int(items[0]["value"])
        yesterday = int(items[1]["value"]) if len(items) > 1 else today

        _cached = FearGreedReading(
            index=today,
            label=items[0]["value_classification"],
            delta1d=today - yesterday,
            fetched_at=now,
        )
        _last_fetch_at = now
        return _cached
    except Exception as err:
        log("warn", f"Fear & Greed network error: {err}")
        return _cached


def fear_greed_to_market_phase(fgi: int) -> str:
    if fgi <= 20:
        return "extreme_fear"
    if fgi >= 80:
        return "extreme_greed"
    if fgi <= 40:
        return "bear"
    if fgi >= 60:
        return "bull"
    return "neutral"


def build_market_context(fgi: FearGreedReading, btc_dominance: float) -> MarketContext:
    return MarketContext(
        phase=fear_greed_to_market_phase(fgi.index),
        btc_dominance=btc_dominance,
        fear_greed_index=fgi.index,
        total_market_cap_change_d1=0,
        timestamp=time.time() * 1000,
    )
