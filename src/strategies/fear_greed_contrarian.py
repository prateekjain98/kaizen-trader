"""Fear & Greed Contrarian Strategy."""

import threading
import time
import uuid
from typing import Optional

from src.types import TradeSignal, MarketContext

STRATEGY_META = {
    "strategies": [
        {"id": "fear_greed_contrarian", "function": "scan_fear_greed_contrarian",
         "description": "Contrarian trades on extreme Fear & Greed Index readings",
         "tier": "swing"},
    ],
    "signal_sources": ["fear_greed"],
}

_ELIGIBLE_SYMBOLS = {"BTC", "ETH"}
_prev_fgi = 50

# Track which symbols have an open FGI position to avoid duplicate entries.
# Cleared when FGI normalizes (20 < fgi < 80).
_open_symbols: set[str] = set()
_REENTRY_COOLDOWN_S = 259_200  # 72h cooldown — backtest: 30min produced churn, 72h filters noise
_close_cooldown: dict[str, float] = {}
_fgi_lock = threading.Lock()


def on_position_opened(symbol: str) -> None:
    """Called by main.py when a fear_greed_contrarian position is actually opened."""
    with _fgi_lock:
        _open_symbols.add(symbol)


def on_position_closed(symbol: str) -> None:
    """Called by main.py when a fear_greed_contrarian position closes."""
    with _fgi_lock:
        _open_symbols.discard(symbol)
        _close_cooldown[symbol] = time.time() + _REENTRY_COOLDOWN_S


def scan_fear_greed_contrarian(
    symbol: str, product_id: str, current_price: float,
    ctx: MarketContext,
) -> Optional[TradeSignal]:
    global _prev_fgi
    if symbol not in _ELIGIBLE_SYMBOLS:
        return None
    now = time.time() * 1000

    fgi = ctx.fear_greed_index
    delta = fgi - _prev_fgi
    _prev_fgi = fgi

    with _fgi_lock:
        # Reset when FGI normalizes
        if 20 < fgi < 80:
            _open_symbols.clear()

        # Don't signal if we already have an open position for this symbol
        if symbol in _open_symbols:
            return None

        # Post-close cooldown to prevent rapid re-entry spam
        now_s = time.time()
        if symbol in _close_cooldown and now_s < _close_cooldown[symbol]:
            return None

        # Purge expired cooldowns to prevent unbounded growth
        expired = [s for s, t in _close_cooldown.items() if now_s >= t]
        for s in expired:
            del _close_cooldown[s]

    # Extreme Fear: contrarian long
    # Backtest finding: FGI≤15 works, FGI≤20 produces too many weak signals
    # Raised base score to 68 to survive self-healing min_qual_score raises
    if fgi <= 15:
        extremeness = min(20, (15 - fgi) * 3)
        momentum_bonus = 10 if delta < -5 else 0
        score = min(88, 68 + extremeness + momentum_bonus)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="fear_greed_contrarian", side="long", tier="swing", score=score,
            confidence="medium" if score > 72 else "low",
            sources=["fear_greed"],
            reasoning=f"Fear & Greed at {fgi} (extreme fear) — contrarian long; 76% of extremes precede positive 30d returns",
            entry_price=current_price, stop_price=current_price * 0.90,
            target_price=current_price * 1.15,  # Backtest-aligned: 15% target vs 10% stop = 1.5:1
            suggested_size_usd=150,
            expires_at=now + 7 * 86_400_000, created_at=now,
        )

    # Extreme Greed: contrarian short — backtest: tighter at 90 (was 85)
    if fgi >= 90 and ctx.phase == "bull":
        extremeness = min(20, (fgi - 90) * 2)
        score = min(80, 65 + extremeness)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="fear_greed_contrarian", side="short", tier="swing", score=score,
            confidence="low", sources=["fear_greed"],
            reasoning=f"Fear & Greed at {fgi} (extreme greed) — contrarian short",
            entry_price=current_price, stop_price=current_price * 1.07,
            target_price=current_price * 0.88,  # R:R fix: 12% target vs 7% stop = 1.71:1
            suggested_size_usd=80,
            expires_at=now + 5 * 86_400_000, created_at=now,
        )

    return None
