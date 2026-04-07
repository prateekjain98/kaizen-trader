"""Listing Pump Strategy."""

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
_LISTING_EXPIRY_MS = 48 * 3_600_000


def on_listing_announcement(listing: ListingAnnouncement, current_price: float) -> Optional[TradeSignal]:
    key = f"{listing.exchange}:{listing.symbol}"
    if key in _seen_listings:
        return None
    _seen_listings.add(key)
    now = time.time() * 1000

    age_ms = now - listing.announced_at
    if age_ms > _LISTING_EXPIRY_MS:
        return None

    base_score = {"coinbase": 75, "binance": 72, "kraken": 60, "bybit": 58}.get(listing.exchange, 55)
    freshness_bonus = max(0, 15 - int(age_ms / 120_000))
    already_listed_penalty = 0 if listing.is_new_to_major_exchanges else -15
    score = min(95, base_score + freshness_bonus + already_listed_penalty)

    return TradeSignal(
        id=str(uuid.uuid4()), symbol=listing.symbol, product_id=listing.product_id,
        strategy="listing_pump", side="long", tier="swing", score=score,
        confidence="high" if score > 80 else ("medium" if score > 65 else "low"),
        sources=["listing_detector"],
        reasoning=f"{listing.symbol} new {listing.exchange} listing {int(age_ms/60_000)}m ago"
                  + (" (first major exchange)" if listing.is_new_to_major_exchanges else ""),
        entry_price=current_price, stop_price=current_price * 0.88,
        suggested_size_usd=120,
        expires_at=now + _LISTING_EXPIRY_MS, created_at=now,
    )
