"""Protocol Revenue Strategy — DeFi fundamentals."""

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal

STRATEGY_META = {
    "strategies": [
        {"id": "protocol_revenue", "function": "scan_protocol_revenue",
         "description": "Trades based on protocol revenue anomalies",
         "tier": "swing"},
    ],
    "signal_sources": ["protocol"],
}


@dataclass
class ProtocolMetrics:
    symbol: str
    product_id: str
    protocol: str
    revenue_24h: float
    revenue_7d_avg: float
    tvl: float
    tvl_change_7d: float
    token_price_change_24h: float


def scan_protocol_revenue(metric: ProtocolMetrics, current_price: float) -> Optional[TradeSignal]:
    now = time.time() * 1000
    revenue_multiple = metric.revenue_24h / metric.revenue_7d_avg if metric.revenue_7d_avg > 0 else 0

    # Revenue multiple is the core requirement; TVL and price are secondary filters
    # Don't reject on TVL or price alone — only reject if revenue signal isn't strong
    if revenue_multiple < 2.0:
        return None
    if metric.token_price_change_24h > 0.12 and metric.tvl_change_7d < -0.20:
        return None  # both price pumped AND TVL declining = skip

    rev_score = min(35, (revenue_multiple - 2) * 10)
    tvl_score = min(15, max(0, metric.tvl_change_7d * 50))
    price_discount_score = max(0, 10 - metric.token_price_change_24h * 50)
    score = min(85, 45 + rev_score + tvl_score + price_discount_score)

    return TradeSignal(
        id=str(uuid.uuid4()), symbol=metric.symbol, product_id=metric.product_id,
        strategy="protocol_revenue", side="long", tier="swing", score=score,
        confidence="medium" if score > 72 else "low",
        sources=["protocol_revenue"],
        reasoning=f"{metric.protocol} revenue {revenue_multiple:.1f}x 7d avg (${metric.revenue_24h/1000:.0f}K today) but {metric.symbol} only +{metric.token_price_change_24h*100:.0f}%",
        entry_price=current_price, stop_price=current_price * 0.92,
        target_price=current_price * 1.15,  # R:R fix: 15% target vs 8% stop = 1.87:1
        suggested_size_usd=120,
        expires_at=now + 86_400_000 * 3, created_at=now,
    )
