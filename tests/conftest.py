"""Shared test fixtures."""

import os
import time
import pytest

# Set test environment
os.environ["PAPER_TRADING"] = "true"
os.environ.setdefault("CONVEX_URL", "https://test.convex.cloud")
os.environ.setdefault("GITHUB_REPO", "prateekjain98/kaizen-trader")

from unittest.mock import MagicMock
from src.types import TradeSignal, Position, ScannerConfig, MarketContext


@pytest.fixture(autouse=True)
def _mock_convex_storage():
    """Provide a mock ConvexStorage for all tests so database calls don't hit real Convex."""
    import src.storage.database as db
    from src.storage.convex_client import ConvexStorage
    mock_storage = MagicMock(spec=ConvexStorage)
    mock_storage.get_open_positions.return_value = []
    mock_storage.get_closed_trades.return_value = []
    mock_storage.get_recent_logs.return_value = []
    mock_storage.get_recent_diagnoses.return_value = []
    mock_storage.get_trade_journal.return_value = []
    old = db._storage
    db._storage = mock_storage
    yield mock_storage
    db._storage = old


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
    current_price=None, size_usd=None, id=None,
) -> Position:
    now = _now_ms()
    return Position(
        id=id or "test-pos-1", symbol=symbol, product_id=f"{symbol}-USD",
        strategy=strategy, side=side, tier=tier,
        entry_price=entry_price, quantity=quantity,
        size_usd=size_usd if size_usd is not None else entry_price * quantity,
        opened_at=opened_at or now,
        high_watermark=high_watermark or entry_price * 1.05,
        low_watermark=low_watermark or entry_price * 0.95,
        current_price=current_price if current_price is not None else entry_price,
        trail_pct=0.07, stop_price=entry_price * 0.93,
        max_hold_ms=43_200_000, qual_score=qual_score,
        signal_id="test-sig-1", status=status,
        exit_price=entry_price * (1 + pnl_pct) if pnl_pct else None,
        closed_at=closed_at, pnl_usd=pnl_usd, pnl_pct=pnl_pct,
        exit_reason=exit_reason,
    )
