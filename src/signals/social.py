"""LunarCrush social signal fetcher."""

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import requests

from src.config import env
from src.signals._circuit_breaker import CircuitBreaker
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
    # Extended sentiment breakdown
    positive_pct: float = 0.0     # % positive mentions
    negative_pct: float = 0.0     # % negative mentions
    neutral_pct: float = 0.0      # % neutral mentions
    social_volume_24h_change: float = 0.0  # % change in social volume over 24h
    alt_rank_change_24h: int = 0  # rank change (negative = improving)


_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9]{1,20}$")
_volume_history: dict[str, deque[float]] = {}
_last_fetch_at: float = 0
_cached: list[SocialSentiment] = []
_CACHE_TTL_MS = 600_000  # 10 min — LunarCrush free tier rate-limits aggressively
_breaker = CircuitBreaker("social")

# Rate limit tracking for /topic/ endpoint
_rate_lock = threading.Lock()
_topic_requests_this_minute: int = 0
_topic_minute_start: float = 0
_MAX_TOPIC_PER_MINUTE = 3

# Cache for topic endpoint
_topic_cache: dict[str, SocialSentiment] = {}
_topic_cache_at: dict[str, float] = {}
_TOPIC_CACHE_TTL_MS = 180_000


def _can_call_topic() -> bool:
    """Check if we're within the topic endpoint rate limit budget."""
    global _topic_requests_this_minute, _topic_minute_start
    with _rate_lock:
        now = time.time()
        if now - _topic_minute_start >= 60:
            _topic_requests_this_minute = 0
            _topic_minute_start = now
        return _topic_requests_this_minute < _MAX_TOPIC_PER_MINUTE


def _record_topic_call() -> None:
    """Record a topic endpoint call for rate limiting."""
    global _topic_requests_this_minute, _topic_minute_start
    with _rate_lock:
        now = time.time()
        if now - _topic_minute_start >= 60:
            _topic_requests_this_minute = 0
            _topic_minute_start = now
        _topic_requests_this_minute += 1


def _compute_velocity(symbol: str, current_volume: float) -> float:
    if symbol not in _volume_history:
        _volume_history[symbol] = deque(maxlen=24)
    hist = _volume_history[symbol]
    avg = sum(hist) / len(hist) if hist else current_volume
    hist.append(current_volume)
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

    # Staleness warning
    if _cached and _last_fetch_at > 0 and now - _last_fetch_at > 2 * _CACHE_TTL_MS:
        log("warn", f"Social data is stale (last fetch {(now - _last_fetch_at) / 60_000:.0f}m ago)")

    if not _breaker.can_call():
        log("warn", "Social circuit breaker OPEN — returning cached data")
        return _cached

    # Validate symbols before constructing URLs
    symbols = [s for s in symbols if _SYMBOL_PATTERN.fullmatch(s)]
    results: list[SocialSentiment] = []
    chunks = [symbols[i:i+10] for i in range(0, len(symbols), 10)]
    any_success = False

    for chunk in chunks:
        url = f"https://lunarcrush.com/api4/public/coins/list/v2?symbols={','.join(chunk)}"
        try:
            res = requests.get(url, headers={"Authorization": f"Bearer {env.lunarcrush_api_key}"}, timeout=8)
            if res.status_code != 200:
                log("warn", f"LunarCrush fetch failed: {res.status_code}")
                continue
            data = res.json()
            any_success = True
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
                    positive_pct=asset.get("sentiment_positive_pct", 0.0),
                    negative_pct=asset.get("sentiment_negative_pct", 0.0),
                    neutral_pct=asset.get("sentiment_neutral_pct", 0.0),
                    social_volume_24h_change=asset.get("social_volume_24h_change", 0.0),
                    alt_rank_change_24h=int(asset.get("alt_rank_change_24h", 0)),
                ))
        except Exception as err:
            log("warn", f"LunarCrush network error: {err}")

    if any_success:
        _breaker.record_success()
    elif chunks:
        _breaker.record_failure()

    _last_fetch_at = now
    _cached = results
    return results


def fetch_topic_sentiment(symbol: str) -> Optional[SocialSentiment]:
    """Fetch real-time social data via /topic/:topic endpoint.

    LunarCrush's AI-optimized endpoint with richer sentiment breakdown.
    Budget: 3 req/min for top-3 active symbols.
    """
    if not env.lunarcrush_api_key:
        return None
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        return None

    now = time.time() * 1000

    # Check topic cache
    if symbol in _topic_cache and symbol in _topic_cache_at:
        if now - _topic_cache_at[symbol] < _TOPIC_CACHE_TTL_MS:
            return _topic_cache[symbol]

    if not _can_call_topic():
        log("warn", f"Topic endpoint rate limit reached — returning cached data for {symbol}")
        return _topic_cache.get(symbol)

    if not _breaker.can_call():
        log("warn", "Social circuit breaker OPEN — returning cached topic data")
        return _topic_cache.get(symbol)

    # LunarCrush topic uses the coin name, but we pass symbol for simplicity
    topic = symbol.lower()
    url = f"https://lunarcrush.com/api4/public/topic/{topic}"

    try:
        _record_topic_call()
        res = requests.get(url, headers={"Authorization": f"Bearer {env.lunarcrush_api_key}"}, timeout=8)
        if res.status_code != 200:
            log("warn", f"LunarCrush topic fetch failed: {res.status_code}")
            _breaker.record_failure()
            return _topic_cache.get(symbol)

        data = res.json().get("data", {})
        _breaker.record_success()

        velocity = _compute_velocity(symbol, data.get("social_volume", 0))
        sentiment_obj = SocialSentiment(
            symbol=symbol.upper(),
            galaxy_score=data.get("galaxy_score", 50),
            alt_rank=data.get("alt_rank", 999),
            social_volume=data.get("social_volume", 0),
            velocity_multiple=velocity,
            sentiment=_galaxy_to_sentiment(data.get("galaxy_score", 50)) + (0.2 if velocity > 3 else 0),
            sampled_at=now,
            positive_pct=data.get("sentiment_positive_pct", 0.0),
            negative_pct=data.get("sentiment_negative_pct", 0.0),
            neutral_pct=data.get("sentiment_neutral_pct", 0.0),
            social_volume_24h_change=data.get("social_volume_24h_change", 0.0),
            alt_rank_change_24h=int(data.get("alt_rank_change_24h", 0)),
        )

        _topic_cache[symbol] = sentiment_obj
        _topic_cache_at[symbol] = now
        return sentiment_obj

    except Exception as err:
        log("warn", f"LunarCrush topic network error: {err}")
        _breaker.record_failure()
        return _topic_cache.get(symbol)


def fetch_social_time_series(symbol: str, interval: str = "1d",
                              data_points: int = 7) -> list[dict]:
    """Fetch historical social data via /public/topic/:topic/time-series/v2.

    Returns hourly/daily social metrics for trend detection.
    Budget: 2 req/min.
    """
    if not env.lunarcrush_api_key:
        return []
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        return []
    if interval not in ("1h", "1d", "1w"):
        return []

    if not _breaker.can_call():
        log("warn", "Social circuit breaker OPEN — skipping time series fetch")
        return []

    topic = symbol.lower()
    url = (
        f"https://lunarcrush.com/api4/public/topic/{topic}/time-series/v2"
        f"?interval={interval}&data_points={data_points}"
    )

    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {env.lunarcrush_api_key}"}, timeout=10)
        if res.status_code != 200:
            log("warn", f"LunarCrush time-series fetch failed: {res.status_code}")
            _breaker.record_failure()
            return []

        data = res.json().get("data", [])
        _breaker.record_success()
        return data

    except Exception as err:
        log("warn", f"LunarCrush time-series network error: {err}")
        _breaker.record_failure()
        return []
