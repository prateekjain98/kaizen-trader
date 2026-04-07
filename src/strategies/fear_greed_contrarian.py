"""Fear & Greed Contrarian Strategy."""

import time
import uuid
from typing import Optional

from src.types import TradeSignal, MarketContext

_ELIGIBLE_SYMBOLS = {"BTC", "ETH"}
_prev_fgi = 50
_activated = False


def scan_fear_greed_contrarian(
    symbol: str, product_id: str, current_price: float,
    ctx: MarketContext,
) -> Optional[TradeSignal]:
    global _prev_fgi, _activated
    if symbol not in _ELIGIBLE_SYMBOLS:
        return None
    now = time.time() * 1000

    fgi = ctx.fear_greed_index
    delta = fgi - _prev_fgi
    _prev_fgi = fgi

    if _activated and 20 < fgi < 80:
        _activated = False

    # Extreme Fear: contrarian long
    if fgi <= 15 and not _activated:
        extremeness = min(30, (15 - fgi) * 2)
        momentum_bonus = 10 if delta < -5 else 0
        score = min(82, 52 + extremeness + momentum_bonus)
        _activated = True
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="fear_greed_contrarian", side="long", tier="swing", score=score,
            confidence="medium" if score > 72 else "low",
            sources=["fear_greed"],
            reasoning=f"Fear & Greed at {fgi} (extreme fear) — contrarian long; 76% of extremes precede positive 30d returns",
            entry_price=current_price, stop_price=current_price * 0.88,
            suggested_size_usd=150,
            expires_at=now + 7 * 86_400_000, created_at=now,
        )

    # Extreme Greed: contrarian short
    if fgi >= 85 and not _activated and ctx.phase != "bull":
        extremeness = min(25, (fgi - 85) * 1.5)
        score = min(75, 45 + extremeness)
        _activated = True
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="fear_greed_contrarian", side="short", tier="swing", score=score,
            confidence="low", sources=["fear_greed"],
            reasoning=f"Fear & Greed at {fgi} (extreme greed) — contrarian short",
            entry_price=current_price, stop_price=current_price * 1.07,
            suggested_size_usd=80,
            expires_at=now + 5 * 86_400_000, created_at=now,
        )

    return None
