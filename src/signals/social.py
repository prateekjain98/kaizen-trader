"""LunarCrush social signal fetcher."""

import time
from dataclasses import dataclass

import requests

from src.config import env
from src.storage.database import log


@dataclass
class SocialSentiment:
    symbol: str
    galaxy_score: float
    alt_rank: int
    social_volume: float
    velocity_multiple: float
    sentiment: float
    sampled_at: float


_volume_history: dict[str, list[float]] = {}
_last_fetch_at: float = 0
_cached: list[SocialSentiment] = []
_CACHE_TTL_MS = 180_000


def _compute_velocity(symbol: str, current_volume: float) -> float:
    if symbol not in _volume_history:
        _volume_history[symbol] = []
    hist = _volume_history[symbol]
    avg = sum(hist) / len(hist) if hist else current_volume
    hist.append(current_volume)
    if len(hist) > 24:
        hist.pop(0)
    return current_volume / avg if avg > 0 else 1.0


def _galaxy_to_sentiment(score: float) -> float:
    if score >= 70:
        return 0.7
    if score >= 55:
        return 0.3
    if score >= 35:
        return 0.0
    if score >= 20:
        return -0.3
    return -0.7


def fetch_social_sentiment(symbols: list[str]) -> list[SocialSentiment]:
    global _last_fetch_at, _cached
    if not env.lunarcrush_api_key:
        return []
    now = time.time() * 1000
    if now - _last_fetch_at < _CACHE_TTL_MS:
        return _cached

    results: list[SocialSentiment] = []
    chunks = [symbols[i:i+10] for i in range(0, len(symbols), 10)]

    for chunk in chunks:
        url = f"https://lunarcrush.com/api4/public/coins/list/v2?symbols={','.join(chunk)}&key={env.lunarcrush_api_key}"
        try:
            res = requests.get(url, timeout=8)
            if res.status_code != 200:
                log("warn", f"LunarCrush fetch failed: {res.status_code}")
                continue
            data = res.json()
            for asset in data.get("data", []):
                sym = asset["symbol"].upper()
                if sym not in chunk:
                    continue
                velocity = _compute_velocity(sym, asset.get("social_volume", 0))
                results.append(SocialSentiment(
                    symbol=sym,
                    galaxy_score=asset.get("galaxy_score", 50),
                    alt_rank=asset.get("alt_rank", 999),
                    social_volume=asset.get("social_volume", 0),
                    velocity_multiple=velocity,
                    sentiment=_galaxy_to_sentiment(asset.get("galaxy_score", 50)) + (0.2 if velocity > 3 else 0),
                    sampled_at=now,
                ))
        except Exception as err:
            log("warn", f"LunarCrush network error: {err}")

    _last_fetch_at = now
    _cached = results
    return results
