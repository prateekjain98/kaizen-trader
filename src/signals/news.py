"""CryptoPanic news sentiment fetcher."""

import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.config import env
from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log

BULLISH_TERMS = [
    "partnership", "integration", "launch", "listing", "upgrade", "milestone",
    "bullish", "adoption", "record", "surge", "rally", "growth", "wins",
    "mainnet", "release", "staking", "airdrop",
]

BEARISH_TERMS = [
    "hack", "exploit", "breach", "fraud", "scam", "rug", "ban", "banned",
    "regulation", "lawsuit", "sec", "crash", "bearish", "suspend", "delisted",
    "delisting", "investigation", "bankruptcy", "insolvent",
]


@dataclass
class NewsSentiment:
    symbol: str
    score: float
    mention_count: int
    top_headlines: list[str]
    velocity_ratio: float
    sampled_at: float


_mention_history: dict[str, list[int]] = {}
_last_fetch_at: float = 0
_cached: list[NewsSentiment] = []
_CACHE_TTL_MS = 300_000
_breaker = CircuitBreaker("news")


def _score_headline(title: str) -> float:
    lower = title.lower()
    score = 0.0
    for term in BULLISH_TERMS:
        if term in lower:
            score += 0.15
    for term in BEARISH_TERMS:
        if term in lower:
            score -= 0.25
    return max(-1.0, min(1.0, score))


def _score_votes(votes: dict) -> float:
    pos = votes.get("positive", 0) + votes.get("liked", 0) + votes.get("important", 0)
    neg = votes.get("negative", 0) + votes.get("disliked", 0) + votes.get("toxic", 0)
    total = pos + neg
    if total < 3:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / total))


def _update_baseline(symbol: str, count: int) -> float:
    if symbol not in _mention_history:
        _mention_history[symbol] = []
    hist = _mention_history[symbol]
    hist.append(count)
    if len(hist) > 7:
        hist.pop(0)
    avg = sum(hist) / len(hist) if hist else 1
    return count / avg if avg > 0 else 1.0


def fetch_news_sentiment(symbols: list[str]) -> list[NewsSentiment]:
    global _last_fetch_at, _cached
    if not env.cryptopanic_token:
        return []
    now = time.time() * 1000
    if now - _last_fetch_at < _CACHE_TTL_MS:
        return _cached

    # Staleness warning
    if _cached and _last_fetch_at > 0 and now - _last_fetch_at > 2 * _CACHE_TTL_MS:
        log("warn", f"News data is stale (last fetch {(now - _last_fetch_at) / 60_000:.0f}m ago)")

    if not _breaker.can_call():
        log("warn", "News circuit breaker OPEN — returning cached data")
        return _cached

    url = "https://cryptopanic.com/api/free/v1/posts/?public=true&kind=news"
    try:
        res = requests.get(url, headers={"Authorization": f"Token {env.cryptopanic_token}"}, timeout=8)
        if res.status_code != 200:
            log("warn", f"CryptoPanic fetch failed: {res.status_code}")
            _breaker.record_failure()
            return _cached
        data = res.json()
        _breaker.record_success()
    except Exception as err:
        log("warn", f"CryptoPanic network error: {err}")
        _breaker.record_failure()
        return _cached

    by_symbol: dict[str, list[dict]] = {}
    for post in data.get("results", []):
        for currency in post.get("currencies", []):
            sym = currency["code"].upper()
            if sym not in symbols:
                continue
            by_symbol.setdefault(sym, []).append(post)

    sentiments: list[NewsSentiment] = []
    for symbol in symbols:
        posts = by_symbol.get(symbol, [])
        if not posts:
            continue
        headline_scores = [_score_headline(p["title"]) for p in posts]
        vote_scores = [_score_votes(p.get("votes", {})) for p in posts]
        all_scores = headline_scores + vote_scores
        avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
        velocity_ratio = _update_baseline(symbol, len(posts))

        sentiments.append(NewsSentiment(
            symbol=symbol,
            score=max(-1.0, min(1.0, avg_score)),
            mention_count=len(posts),
            top_headlines=[p["title"] for p in posts[:3]],
            velocity_ratio=velocity_ratio,
            sampled_at=now,
        ))

    _last_fetch_at = now
    _cached = sentiments
    return sentiments
