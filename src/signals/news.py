"""News sentiment fetcher — CryptoPanic (with key) or CoinTelegraph RSS (free fallback)."""

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

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


_lock = threading.Lock()
_mention_history: dict[str, deque[int]] = {}
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
        _mention_history[symbol] = deque(maxlen=7)
    hist = _mention_history[symbol]
    hist.append(count)
    avg = sum(hist) / len(hist) if hist else 1
    return count / avg if avg > 0 else 1.0


def _fetch_cointelegraph_rss(symbols: list[str]) -> list[NewsSentiment]:
    """Free fallback: CoinTelegraph RSS. No auth required."""
    try:
        req = Request("https://cointelegraph.com/rss",
                       headers={"User-Agent": "kaizen-trader/2.0"})
        with urlopen(req, timeout=10) as resp:
            xml = resp.read().decode()
    except Exception as err:
        log("warn", f"CoinTelegraph RSS fetch failed: {err}")
        return []

    titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml)
    if not titles:
        # Try non-CDATA titles
        titles = re.findall(r"<title>([^<]+)</title>", xml)

    now = time.time() * 1000
    # Match headlines to symbols by keyword
    symbol_aliases = {s: {s.lower()} for s in symbols}
    _EXTRA = {"BTC": {"bitcoin"}, "ETH": {"ethereum", "ether"}, "SOL": {"solana"},
              "BNB": {"binance"}, "XRP": {"ripple"}, "DOGE": {"dogecoin"},
              "ADA": {"cardano"}, "AVAX": {"avalanche"}, "LINK": {"chainlink"},
              "DOT": {"polkadot"}, "NEAR": {"near protocol"}, "LTC": {"litecoin"},
              "BCH": {"bitcoin cash"}, "TON": {"toncoin"}, "ATOM": {"cosmos"},
              "ALGO": {"algorand"}, "HBAR": {"hedera"}, "STX": {"stacks"},
              "FIL": {"filecoin"}, "APT": {"aptos"}, "SEI": {"sei"},
              "INJ": {"injective"}, "TIA": {"celestia"}, "POL": {"polygon"},
              "AAVE": {"aave"}, "UNI": {"uniswap"}, "LDO": {"lido"},
              "ONDO": {"ondo"}, "ENA": {"ethena"}, "SNX": {"synthetix"},
              "CRV": {"curve"}, "ENS": {"ethereum name"}, "RENDER": {"render"},
              "FET": {"fetch"}, "IMX": {"immutable"}, "ARB": {"arbitrum"},
              "OP": {"optimism"}, "TAO": {"bittensor"}, "WLD": {"worldcoin"},
              "PEPE": {"pepe"}, "FLOKI": {"floki"}, "SUI": {"sui"},
              "HYPE": {"hyperliquid"}, "WIF": {"dogwifhat"},
              "BONK": {"bonk"}, "PENGU": {"pudgy penguin"}}
    for s in symbols:
        if s in _EXTRA:
            symbol_aliases[s] |= _EXTRA[s]

    by_symbol: dict[str, list[str]] = {}
    for title in titles[:20]:
        lower = title.lower()
        for sym, aliases in symbol_aliases.items():
            if any(a in lower for a in aliases):
                by_symbol.setdefault(sym, []).append(title)

    sentiments: list[NewsSentiment] = []
    for sym, matched_titles in by_symbol.items():
        scores = [_score_headline(t) for t in matched_titles]
        avg = sum(scores) / len(scores) if scores else 0
        velocity = _update_baseline(sym, len(matched_titles))
        sentiments.append(NewsSentiment(
            symbol=sym,
            score=max(-1.0, min(1.0, avg)),
            mention_count=len(matched_titles),
            top_headlines=matched_titles[:3],
            velocity_ratio=velocity,
            sampled_at=now,
        ))

    if sentiments:
        log("info", f"CoinTelegraph RSS: {len(sentiments)} symbols with news mentions")
    return sentiments


def fetch_news_sentiment(symbols: list[str]) -> list[NewsSentiment]:
    global _last_fetch_at, _cached
    now = time.time() * 1000
    with _lock:
        if now - _last_fetch_at < _CACHE_TTL_MS:
            return _cached

    # Staleness warning
    if _cached and _last_fetch_at > 0 and now - _last_fetch_at > 2 * _CACHE_TTL_MS:
        log("warn", f"News data is stale (last fetch {(now - _last_fetch_at) / 60_000:.0f}m ago)")

    # ── CryptoPanic (if token configured) ──────────────────────────────
    if env.cryptopanic_token and _breaker.can_call():
        url = "https://cryptopanic.com/api/free/v1/posts/?public=true&kind=news"
        try:
            res = requests.get(url, headers={"Authorization": f"Token {env.cryptopanic_token}"}, timeout=8)
            if res.status_code == 200:
                data = res.json()
                _breaker.record_success()
                sentiments = _parse_cryptopanic(data, symbols, now)
                with _lock:
                    _last_fetch_at = now
                    _cached = sentiments
                return sentiments
            else:
                log("warn", f"CryptoPanic fetch failed: {res.status_code}")
                _breaker.record_failure()
        except Exception as err:
            log("warn", f"CryptoPanic network error: {err}")
            _breaker.record_failure()

    # ── CoinTelegraph RSS fallback (free, no auth) ─────────────────────
    sentiments = _fetch_cointelegraph_rss(symbols)
    with _lock:
        _last_fetch_at = now
        _cached = sentiments
    return sentiments


def _parse_cryptopanic(data: dict, symbols: list[str], now: float) -> list[NewsSentiment]:
    """Parse CryptoPanic API response into NewsSentiment list."""
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
    return sentiments
