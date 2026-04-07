"""Shared test fixtures."""

import os
import time
import pytest

# Use in-memory DB for all tests
os.environ["DB_PATH"] = ":memory:"
os.environ["PAPER_TRADING"] = "true"
os.environ.setdefault("GITHUB_REPO", "prateekjain98/kaizen-trader")

from src.types import TradeSignal, Position, ScannerConfig, MarketContext


def _now_ms() -> float:
    return time.time() * 1000


@pytest.fixture
def config():
    return ScannerConfig()


@pytest.fixture
def bull_ctx():
    return MarketContext(
        phase="bull", btc_dominance=45.0,
        fear_greed_index=65, total_market_cap_change_d1=3.0,
        timestamp=_now_ms(),
    )


@pytest.fixture
def bear_ctx():
    return MarketContext(
        phase="bear", btc_dominance=55.0,
        fear_greed_index=25, total_market_cap_change_d1=-4.0,
        timestamp=_now_ms(),
    )


@pytest.fixture
def neutral_ctx():
    return MarketContext(
        phase="neutral", btc_dominance=48.0,
        fear_greed_index=50, total_market_cap_change_d1=0.5,
        timestamp=_now_ms(),
    )


def make_signal(
    symbol="ETH", side="long", tier="swing", score=65.0,
    strategy="momentum_swing", entry_price=2000.0,
) -> TradeSignal:
    now = _now_ms()
    return TradeSignal(
        id="test-sig-1", symbol=symbol, product_id=f"{symbol}-USD",
        strategy=strategy, side=side, tier=tier, score=score,
        confidence="medium", sources=["price_action"],
        reasoning="test signal", entry_price=entry_price,
        expires_at=now + 300_000, created_at=now,
    )


def make_position(
    symbol="ETH", side="long", tier="swing", strategy="momentum_swing",
    entry_price=2000.0, quantity=0.5, pnl_pct=None, pnl_usd=None,
    exit_reason=None, qual_score=70.0, opened_at=None, closed_at=None,
    high_watermark=None, low_watermark=None, status="open",
) -> Position:
    now = _now_ms()
    return Position(
        id="test-pos-1", symbol=symbol, product_id=f"{symbol}-USD",
        strategy=strategy, side=side, tier=tier,
        entry_price=entry_price, quantity=quantity,
        size_usd=entry_price * quantity,
        opened_at=opened_at or now,
        high_watermark=high_watermark or entry_price * 1.05,
        low_watermark=low_watermark or entry_price * 0.95,
        current_price=entry_price,
        trail_pct=0.07, stop_price=entry_price * 0.93,
        max_hold_ms=43_200_000, qual_score=qual_score,
        signal_id="test-sig-1", status=status,
        exit_price=entry_price * (1 + pnl_pct) if pnl_pct else None,
        closed_at=closed_at, pnl_usd=pnl_usd, pnl_pct=pnl_pct,
        exit_reason=exit_reason,
    )
