"""Correlation Break Strategy — BTC/altcoin divergence."""

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal, ScannerConfig, MarketContext


@dataclass
class PricePoint:
    btc_pct: float
    alt_pct: float
    ts: float


_correlation_history: dict[str, list[PricePoint]] = {}


def update_correlation_point(symbol: str, btc_pct: float, alt_pct: float) -> None:
    hist = _correlation_history.setdefault(symbol, [])
    hist.append(PricePoint(btc_pct=btc_pct, alt_pct=alt_pct, ts=time.time() * 1000))
    cutoff = time.time() * 1000 - 172_800_000
    _correlation_history[symbol] = [p for p in hist if p.ts >= cutoff]


def _compute_expected_alt_move(symbol: str, btc_pct: float) -> Optional[float]:
    hist = _correlation_history.get(symbol)
    if not hist or len(hist) < 24:
        return None
    n = len(hist)
    sum_x = sum(p.btc_pct for p in hist)
    sum_y = sum(p.alt_pct for p in hist)
    sum_xy = sum(p.btc_pct * p.alt_pct for p in hist)
    sum_xx = sum(p.btc_pct ** 2 for p in hist)

    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-10:
        return None
    beta = (n * sum_xy - sum_x * sum_y) / denom
    alpha = (sum_y - beta * sum_x) / n
    return alpha + beta * btc_pct


def scan_correlation_break(
    symbol: str, product_id: str, current_price: float,
    btc_1h_pct: float, alt_1h_pct: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    if ctx.phase in ("extreme_greed", "extreme_fear"):
        return None
    now = time.time() * 1000

    update_correlation_point(symbol, btc_1h_pct, alt_1h_pct)
    expected = _compute_expected_alt_move(symbol, btc_1h_pct)
    if expected is None:
        return None

    divergence = alt_1h_pct - expected

    # Underperformance long
    if divergence < -0.03 and abs(divergence) > 0.02:
        div_score = min(30, abs(divergence) * 500)
        score = min(80, 42 + div_score)
        if score >= config.min_qual_score_swing - 5:
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
                strategy="correlation_break", side="long", tier="swing", score=score,
                confidence="low", sources=["correlation"],
                reasoning=f"{symbol} underperforming BTC by {divergence*100:.1f}% (expected {expected*100:.1f}%, got {alt_1h_pct*100:.1f}%)",
                entry_price=current_price, stop_price=current_price * 0.97,
                suggested_size_usd=70,
                expires_at=now + 7_200_000, created_at=now,
            )

    # Overperformance short
    if divergence > 0.04 and ctx.phase != "bull":
        div_score = min(28, divergence * 450)
        score = min(78, 38 + div_score)
        if score >= config.min_qual_score_swing - 5:
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
                strategy="correlation_break", side="short", tier="swing", score=score,
                confidence="low", sources=["correlation"],
                reasoning=f"{symbol} overperforming BTC correlation by {divergence*100:.1f}%",
                entry_price=current_price, stop_price=current_price * 1.03,
                suggested_size_usd=60,
                expires_at=now + 7_200_000, created_at=now,
            )

    return None
