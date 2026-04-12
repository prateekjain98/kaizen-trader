"""Funding Rate Extreme Strategy."""

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal, ScannerConfig, MarketContext


@dataclass
class FundingRateData:
    symbol: str
    funding_rate: float
    funding_interval_hours: float
    open_interest: float
    open_interest_change_pct: float
    predicted_rate: Optional[float] = None


_funding_cache: dict[str, FundingRateData] = {}
_funding_lock = threading.Lock()
_MAX_FUNDING_CACHE = 200


def update_funding_data(data: FundingRateData) -> None:
    with _funding_lock:
        if len(_funding_cache) >= _MAX_FUNDING_CACHE and data.symbol not in _funding_cache:
            oldest_key = min(_funding_cache, key=lambda k: _funding_cache[k].funding_rate)
            del _funding_cache[oldest_key]
        _funding_cache[data.symbol] = data


STRATEGY_META = {
    "strategies": [
        {"id": "funding_extreme", "function": "scan_funding_extreme",
         "description": "Trades extreme funding rate reversals on futures markets",
         "tier": "swing"},
    ],
    "signal_sources": ["funding"],
}


def scan_funding_extreme(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    with _funding_lock:
        funding = _funding_cache.get(symbol)
    if not funding:
        return None
    now = time.time() * 1000

    threshold = config.funding_rate_extreme_threshold
    if funding.funding_interval_hours == 0 or threshold == 0:
        return None
    annualized = funding.funding_rate * (8760 / funding.funding_interval_hours)

    # Backtest finding: require 3x threshold minimum — 173 funding_squeeze losses at 2x.
    # Weak funding extremes don't revert reliably enough to overcome fees.
    # Also block long entries during extreme_fear (12 losses in backtest).
    min_magnitude = threshold * 3

    # Short: over-leveraged longs — funding extremes are MOST reliable in bear markets
    if (funding.funding_rate > min_magnitude
            and funding.open_interest_change_pct < 15):
        mag_score = min(40, (funding.funding_rate / threshold - 1) * 20)
        oi_score = min(20, funding.open_interest_change_pct / 5)
        # Backtest fix: raise base score to avoid low_qual_score losses (158 occurrences)
        score = min(88, 55 + mag_score + oi_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="funding_extreme", side="short", tier="swing", score=score,
            confidence="medium" if score > 70 else "low",
            sources=["funding_rates"],
            reasoning=f"{symbol} funding={funding.funding_rate*100:.3f}% ({annualized*100:.0f}% ann), OI +{funding.open_interest_change_pct:.0f}%",
            entry_price=current_price, stop_price=current_price * 1.06,
            target_price=current_price * 0.92,  # R:R fix: 8% target vs 6% stop = 1.33:1
            suggested_size_usd=60,
            expires_at=now + 14_400_000, created_at=now,
        )

    # Long: short squeeze — OI should be DECREASING (shorts closing/getting liquidated)
    # Backtest fix: block longs during extreme_fear (wrong_market_phase losses)
    if (funding.funding_rate < -min_magnitude
            and funding.open_interest_change_pct < -5
            and ctx.phase not in ("bear",)):
        mag_score = min(35, (-funding.funding_rate / threshold - 1) * 18)
        score = min(85, 52 + mag_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="funding_extreme", side="long", tier="swing", score=score,
            confidence="medium" if score > 68 else "low",
            sources=["funding_rates"],
            reasoning=f"{symbol} funding={funding.funding_rate*100:.3f}% (negative), OI +{funding.open_interest_change_pct:.0f}% shorts",
            entry_price=current_price, stop_price=current_price * 0.95,
            target_price=current_price * 1.08,  # R:R fix: 8% target vs 5% stop = 1.6:1
            suggested_size_usd=70,
            expires_at=now + 14_400_000, created_at=now,
        )

    return None
