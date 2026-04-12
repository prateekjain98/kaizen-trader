"""Listing Pump Strategy."""

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal


@dataclass
class ListingAnnouncement:
    symbol: str
    exchange: str
    product_id: str
    announced_at: float
    is_new_to_major_exchanges: bool
    trading_starts_at: Optional[float] = None


_seen_listings: set[str] = set()
_listing_timestamps: dict[str, float] = {}
_lock = threading.Lock()
_LISTING_EXPIRY_MS = 6 * 3_600_000  # 6 hours — listing pumps exhaust quickly
_last_cleanup: float = 0

STRATEGY_META = {
    "strategies": [
        {"id": "listing_announcement", "function": "on_listing_announcement",
         "description": "Detects new exchange listings and rides the initial pump",
         "tier": "swing"},
    ],
    "signal_sources": ["price_action"],
}


def _cleanup_expired_listings(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < 3_600_000:
        return
    _last_cleanup = now
    expired = [k for k, ts in _listing_timestamps.items() if now - ts > _LISTING_EXPIRY_MS]
    for k in expired:
        _seen_listings.discard(k)
        _listing_timestamps.pop(k, None)


def on_listing_announcement(listing: ListingAnnouncement, current_price: float) -> Optional[TradeSignal]:
    now = time.time() * 1000
    key = f"{listing.exchange}:{listing.symbol}"

    with _lock:
        _cleanup_expired_listings(now)
        if key in _seen_listings:
            return None
        _seen_listings.add(key)
        _listing_timestamps[key] = now

    age_ms = now - listing.announced_at
    if age_ms > _LISTING_EXPIRY_MS:
        return None

    base_score = {"coinbase": 75, "binance": 72, "kraken": 60, "bybit": 58}.get(listing.exchange, 55)
    # Decay faster: penalize signals older than 15 minutes instead of rewarding late entries
    age_minutes = age_ms / 60_000
    if age_minutes <= 15:
        freshness_bonus = max(0, 15 - int(age_minutes))
    else:
        freshness_bonus = -min(10, int((age_minutes - 15) / 5))
    already_listed_penalty = 0 if listing.is_new_to_major_exchanges else -15
    score = min(95, base_score + freshness_bonus + already_listed_penalty)

    return TradeSignal(
        id=str(uuid.uuid4()), symbol=listing.symbol, product_id=listing.product_id,
        strategy="listing_pump", side="long", tier="swing", score=score,
        confidence="high" if score > 80 else ("medium" if score > 65 else "low"),
        sources=["listing_detector"],
        reasoning=f"{listing.symbol} new {listing.exchange} listing {int(age_ms/60_000)}m ago"
                  + (" (first major exchange)" if listing.is_new_to_major_exchanges else ""),
        entry_price=current_price, stop_price=current_price * 0.85,
        target_price=current_price * 1.20,  # R:R fix: 20% target vs 15% stop = 1.33:1
        suggested_size_usd=120,
        expires_at=now + _LISTING_EXPIRY_MS, created_at=now,
    )
