"""Dynamic watchlist forager — discover volatile/trending symbols.

Inspired by Passivbot's forager mode. Periodically scans exchange data
for symbols with volume spikes or strong momentum, adds them to the
watchlist temporarily. Symbols that go quiet are removed.
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.storage.database import log


@dataclass
class ForagerCandidate:
    symbol: str
    product_id: str
    volume_24h: float
    price_change_24h_pct: float
    added_at: float
    score: float = 0.0


_lock = threading.Lock()
_dynamic_symbols: dict[str, ForagerCandidate] = {}
_MAX_DYNAMIC = 10  # max symbols to add dynamically
_MIN_VOLUME_24H = 50_000_000  # $50M minimum 24h volume
_MIN_PRICE_CHANGE_PCT = 0.03  # 3% minimum 24h price change
_EXPIRY_S = 3600  # dynamic symbols expire after 1 hour


def update_candidates(ticker_data: list[dict]) -> list[str]:
    """Process exchange ticker data and identify forager candidates.

    Args:
        ticker_data: List of dicts with keys: symbol, product_id, volume_24h, price_change_24h_pct

    Returns:
        List of newly added product_ids.
    """
    now = time.time()
    added = []

    # Score and rank candidates
    candidates = []
    for t in ticker_data:
        vol = t.get("volume_24h", 0)
        change = abs(t.get("price_change_24h_pct", 0))

        if vol < _MIN_VOLUME_24H:
            continue
        if change < _MIN_PRICE_CHANGE_PCT:
            continue

        # Score: volume weight + momentum weight
        vol_score = min(50, (vol / 100_000_000) * 10)  # 10pts per $100M, cap at 50
        momentum_score = min(50, change * 500)  # 50pts at 10% change, cap at 50
        score = vol_score + momentum_score

        candidates.append(ForagerCandidate(
            symbol=t["symbol"],
            product_id=t["product_id"],
            volume_24h=vol,
            price_change_24h_pct=t.get("price_change_24h_pct", 0),
            added_at=now,
            score=score,
        ))

    # Sort by score descending
    candidates.sort(key=lambda c: c.score, reverse=True)

    with _lock:
        # Prune expired symbols
        expired = [sym for sym, c in _dynamic_symbols.items()
                   if now - c.added_at > _EXPIRY_S]
        for sym in expired:
            del _dynamic_symbols[sym]

        # Add top candidates up to max
        for c in candidates[:_MAX_DYNAMIC]:
            if c.symbol not in _dynamic_symbols:
                _dynamic_symbols[c.symbol] = c
                added.append(c.product_id)
                if len(added) <= 3:  # only log first 3
                    log("info", f"Forager added {c.symbol}: "
                        f"vol=${c.volume_24h/1e6:.0f}M, "
                        f"change={c.price_change_24h_pct*100:.1f}%, "
                        f"score={c.score:.0f}")

    return added


def get_dynamic_product_ids() -> list[str]:
    """Get current dynamic product IDs to subscribe to."""
    now = time.time()
    with _lock:
        return [
            c.product_id for c in _dynamic_symbols.values()
            if now - c.added_at <= _EXPIRY_S
        ]


def get_dynamic_symbols() -> list[str]:
    """Get current dynamic symbol names."""
    now = time.time()
    with _lock:
        return [
            c.symbol for c in _dynamic_symbols.values()
            if now - c.added_at <= _EXPIRY_S
        ]


def get_forager_stats() -> dict:
    """Get forager stats for health endpoint."""
    with _lock:
        return {
            "dynamic_symbols": len(_dynamic_symbols),
            "symbols": list(_dynamic_symbols.keys()),
        }
