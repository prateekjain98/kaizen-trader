"""Funding Rate Extreme Strategy."""

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


def update_funding_data(data: FundingRateData) -> None:
    _funding_cache[data.symbol] = data


def scan_funding_extreme(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    funding = _funding_cache.get(symbol)
    if not funding:
        return None
    now = time.time() * 1000

    threshold = config.funding_rate_extreme_threshold
    if funding.funding_interval_hours == 0 or threshold == 0:
        return None
    annualized = funding.funding_rate * (8760 / funding.funding_interval_hours)

    # Short: over-leveraged longs
    if (funding.funding_rate > threshold
            and funding.open_interest_change_pct > 10
            and ctx.phase != "extreme_greed"):
        mag_score = min(40, (funding.funding_rate / threshold - 1) * 20)
        oi_score = min(20, funding.open_interest_change_pct / 5)
        score = min(88, 45 + mag_score + oi_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="funding_extreme", side="short", tier="swing", score=score,
            confidence="medium" if score > 70 else "low",
            sources=["funding_rates"],
            reasoning=f"{symbol} funding={funding.funding_rate*100:.3f}% ({annualized*100:.0f}% ann), OI +{funding.open_interest_change_pct:.0f}%",
            entry_price=current_price, stop_price=current_price * 1.04,
            suggested_size_usd=60,
            expires_at=now + 14_400_000, created_at=now,
        )

    # Long: short squeeze
    if (funding.funding_rate < -threshold
            and funding.open_interest_change_pct > 5):
        mag_score = min(35, (-funding.funding_rate / threshold - 1) * 18)
        score = min(85, 42 + mag_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="funding_extreme", side="long", tier="swing", score=score,
            confidence="medium" if score > 68 else "low",
            sources=["funding_rates"],
            reasoning=f"{symbol} funding={funding.funding_rate*100:.3f}% (negative), OI +{funding.open_interest_change_pct:.0f}% shorts",
            entry_price=current_price, stop_price=current_price * 0.97,
            suggested_size_usd=70,
            expires_at=now + 14_400_000, created_at=now,
        )

    return None
