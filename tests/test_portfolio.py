"""Tests for portfolio risk manager — protection chain, Sharpe, drawdown."""

import math
import pytest
from unittest.mock import patch

import src.risk.portfolio as portfolio_mod
from src.risk.portfolio import (
    DailyStats, can_open_position, register_open, register_close,
    update_position_price, get_open_positions, get_daily_stats,
    is_circuit_breaker_open, compute_sharpe, compute_max_drawdown,
    init_protections,
)
from src.risk.protections import DEFAULT_PROTECTIONS
from tests.conftest import make_position


@pytest.fixture(autouse=True)
def reset_portfolio_state():
    """Reset module-level state before each test."""
    portfolio_mod._open_positions.clear()
    portfolio_mod._daily_returns.clear()
    portfolio_mod._daily_stats = DailyStats(date=portfolio_mod._today_utc())
    # Re-initialize protection chain with defaults for clean state
    portfolio_mod._protection_chain = None
    yield


class TestCanOpenPosition:
    @patch("src.risk.portfolio.log")
    def test_can_open_when_clear(self, mock_log):
        assert can_open_position() is True

    @patch("src.risk.portfolio.log")
    def test_blocked_by_position_cap(self, mock_log):
        for i in range(5):  # default max = 5
            p = make_position()
            p.id = f"pos-{i}"
            register_open(p)
        assert can_open_position() is False

    @patch("src.risk.portfolio.log")
    def test_blocked_by_daily_loss(self, mock_log):
        # Trigger via register_close so the chain sees the loss
        p1 = make_position()
        p1.id = "p1"
        register_open(p1)
        register_close(p1, pnl_usd=-200)
        p2 = make_position()
        p2.id = "p2"
        register_open(p2)
        register_close(p2, pnl_usd=-150)
        # Total = -350 > -300 threshold
        assert can_open_position() is False


class TestRegisterOpenClose:
    @patch("src.risk.portfolio.log")
    def test_register_open(self, mock_log):
        p = make_position()
        register_open(p)
        assert len(get_open_positions()) == 1

    @patch("src.risk.portfolio.log")
    def test_register_close_removes_position(self, mock_log):
        p = make_position()
        register_open(p)
        register_close(p, pnl_usd=50)
        assert len(get_open_positions()) == 0

    @patch("src.risk.portfolio.log")
    def test_register_close_updates_daily_stats(self, mock_log):
        p = make_position()
        register_open(p)
        register_close(p, pnl_usd=-50)
        stats = get_daily_stats()
        assert stats.realized_pnl == -50
        assert stats.trade_count == 1


class TestCircuitBreaker:
    @patch("src.risk.portfolio.log")
    def test_triggers_on_large_loss(self, mock_log):
        # Default max_daily_loss_usd = 300
        p1 = make_position()
        p1.id = "p1"
        register_open(p1)
        register_close(p1, pnl_usd=-200)
        assert is_circuit_breaker_open() is False

        p2 = make_position()
        p2.id = "p2"
        register_open(p2)
        register_close(p2, pnl_usd=-150)
        # Total = -350 > -300 threshold
        assert is_circuit_breaker_open() is True

    @patch("src.risk.portfolio.log")
    def test_not_triggered_by_wins(self, mock_log):
        p = make_position()
        register_open(p)
        register_close(p, pnl_usd=500)
        assert is_circuit_breaker_open() is False


class TestProtectionChainIntegration:
    @patch("src.risk.portfolio.log")
    def test_stoploss_guard_blocks_after_consecutive_stops(self, mock_log):
        """StoplossGuard (from DEFAULT_PROTECTIONS) blocks after 3 consecutive stop-losses."""
        for i in range(3):
            p = make_position(exit_reason="trailing_stop")
            p.id = f"sl-{i}"
            register_open(p)
            register_close(p, pnl_usd=-20)
        assert can_open_position() is False

    @patch("src.risk.portfolio.log")
    def test_cooldown_blocks_after_consecutive_losses(self, mock_log):
        """CooldownPeriod (from DEFAULT_PROTECTIONS) blocks after 4 consecutive losses."""
        for i in range(4):
            p = make_position(exit_reason="time_limit")
            p.id = f"cd-{i}"
            register_open(p)
            register_close(p, pnl_usd=-10)
        assert can_open_position() is False

    @patch("src.risk.portfolio.log")
    def test_custom_protections(self, mock_log):
        """Initialize with custom config — only max_open_positions=2."""
        init_protections([
            {"rule_type": "max_open_positions", "params": {"max_open_positions": 2}},
        ])
        p1 = make_position()
        p1.id = "c1"
        register_open(p1)
        assert can_open_position() is True
        p2 = make_position()
        p2.id = "c2"
        register_open(p2)
        assert can_open_position() is False


class TestUpdatePositionPrice:
    def test_updates_price(self):
        p = make_position()
        register_open(p)
        update_position_price(p.id, 2500)
        positions = get_open_positions()
        assert positions[0].current_price == 2500

    def test_unknown_id_noop(self):
        update_position_price("nonexistent", 9999)  # should not raise


class TestComputeSharpe:
    def test_insufficient_data(self):
        assert compute_sharpe() is None

    def test_insufficient_29_days(self):
        portfolio_mod._daily_returns.extend([10.0] * 29)
        assert compute_sharpe() is None

    def test_30_days_computes(self):
        portfolio_mod._daily_returns.extend([10.0] * 30)
        result = compute_sharpe()
        # All same returns -> std_dev = 0 -> None
        assert result is None  # zero variance

    def test_positive_sharpe(self):
        # Mix of positive returns with some variance
        returns = [10, 12, 8, 15, 5, 11, 9, 14, 7, 13] * 3  # 30 returns
        portfolio_mod._daily_returns.extend(returns)
        result = compute_sharpe()
        assert result is not None
        assert result > 0  # positive average returns

    def test_negative_sharpe(self):
        returns = [-10, -12, -8, -15, -5, -11, -9, -14, -7, -13] * 3
        portfolio_mod._daily_returns.extend(returns)
        result = compute_sharpe()
        assert result is not None
        assert result < 0


class TestComputeMaxDrawdown:
    def test_empty_returns(self):
        assert compute_max_drawdown() == 0

    def test_all_positive(self):
        portfolio_mod._daily_returns.extend([10, 20, 30])
        assert compute_max_drawdown() == 0

    def test_simple_drawdown(self):
        portfolio_mod._daily_returns.extend([100, -50])
        dd = compute_max_drawdown()
        assert abs(dd - 0.5) < 1e-10

    def test_recovery_then_new_drawdown(self):
        portfolio_mod._daily_returns.extend([100, -30, 50, -80])
        dd = compute_max_drawdown()
        # equity: 100, 70, 120, 40 -> peak=120, dd=(120-40)/120=0.667
        assert abs(dd - 2 / 3) < 1e-10
