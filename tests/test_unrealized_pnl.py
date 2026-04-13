"""Tests for unrealized P&L in drawdown checks."""
import pytest
from unittest.mock import patch, MagicMock
from src.risk import portfolio
from tests.conftest import make_position


class TestUnrealizedPnL:
    def setup_method(self):
        """Reset portfolio state before each test."""
        portfolio._protection_chain = None
        with portfolio._lock:
            portfolio._open_positions.clear()
            portfolio._daily_stats = portfolio.DailyStats(date=portfolio._today_utc())

    def test_compute_unrealized_pnl_no_positions(self):
        result = portfolio.compute_unrealized_pnl()
        assert result == 0.0

    def test_compute_unrealized_pnl_long_profit(self):
        pos = make_position(
            side="long", entry_price=100.0, quantity=1.0,
            size_usd=100.0, current_price=110.0,
        )
        with portfolio._lock:
            portfolio._open_positions[pos.id] = pos
        result = portfolio.compute_unrealized_pnl()
        assert result == pytest.approx(10.0, abs=0.01)

    def test_compute_unrealized_pnl_long_loss(self):
        pos = make_position(
            side="long", entry_price=100.0, quantity=1.0,
            size_usd=100.0, current_price=90.0,
        )
        with portfolio._lock:
            portfolio._open_positions[pos.id] = pos
        result = portfolio.compute_unrealized_pnl()
        assert result == pytest.approx(-10.0, abs=0.01)

    def test_compute_unrealized_pnl_short_profit(self):
        pos = make_position(
            side="short", entry_price=100.0, quantity=1.0,
            size_usd=100.0, current_price=90.0,
        )
        with portfolio._lock:
            portfolio._open_positions[pos.id] = pos
        result = portfolio.compute_unrealized_pnl()
        assert result == pytest.approx(10.0, abs=0.01)

    def test_compute_unrealized_pnl_multiple_positions(self):
        pos1 = make_position(
            id="pos-1",
            side="long", entry_price=100.0, quantity=1.0,
            size_usd=100.0, current_price=110.0,
        )
        pos2 = make_position(
            id="pos-2",
            side="long", entry_price=200.0, quantity=0.5,
            size_usd=100.0, current_price=180.0,
        )
        with portfolio._lock:
            portfolio._open_positions[pos1.id] = pos1
            portfolio._open_positions[pos2.id] = pos2
        result = portfolio.compute_unrealized_pnl()
        # pos1: (110-100)*1 = +10, pos2: (180-200)*0.5 = -10, total = 0
        assert result == pytest.approx(0.0, abs=0.01)

    def test_can_open_blocked_by_unrealized_loss(self):
        """Large unrealized losses should block new positions."""
        pos = make_position(
            side="long", entry_price=100.0, quantity=10.0,
            size_usd=1000.0, current_price=69.0,
        )
        with portfolio._lock:
            portfolio._open_positions[pos.id] = pos
        # Unrealized PnL = (69-100)*10 = -310, which exceeds default max_daily_loss of $300
        allowed = portfolio.can_open_position()
        assert allowed is False
