"""Tests for the performance metrics engine — pure math functions."""

import math
import pytest
from unittest.mock import patch

from src.evaluation.metrics import (
    _mean, _std_dev, _max_drawdown, _max_consecutive_losses,
    _kelly_fraction, compute_metrics, format_metrics,
)
from tests.conftest import make_position


# ── _mean ──────────────────────────────────────────────────────────────────

class TestMean:
    def test_empty(self):
        assert _mean([]) == 0

    def test_single(self):
        assert _mean([5.0]) == 5.0

    def test_multiple(self):
        assert _mean([1, 2, 3, 4, 5]) == 3.0

    def test_negative_values(self):
        assert _mean([-10, 10]) == 0.0


# ── _std_dev ───────────────────────────────────────────────────────────────

class TestStdDev:
    def test_empty(self):
        assert _std_dev([]) == 0

    def test_single_value(self):
        assert _std_dev([42.0]) == 0

    def test_two_values(self):
        result = _std_dev([10, 20])
        expected = math.sqrt(((10 - 15)**2 + (20 - 15)**2) / 1)
        assert abs(result - expected) < 1e-10

    def test_with_precomputed_avg(self):
        result = _std_dev([2, 4, 6], avg=4.0)
        expected = math.sqrt(((2 - 4)**2 + (4 - 4)**2 + (6 - 4)**2) / 2)
        assert abs(result - expected) < 1e-10

    def test_all_same_values(self):
        assert _std_dev([5, 5, 5, 5]) == 0.0


# ── _max_drawdown ──────────────────────────────────────────────────────────

class TestMaxDrawdown:
    def test_empty(self):
        assert _max_drawdown([]) == 0

    def test_all_positive(self):
        assert _max_drawdown([10, 20, 30]) == 0

    def test_simple_drawdown(self):
        # equity: 100, 50 -> peak=100, dd = (100-50)/100 = 0.5
        dd = _max_drawdown([100, -50])
        assert abs(dd - 0.5) < 1e-10

    def test_recovery(self):
        # equity: 100, 50, 120 -> dd = (100-50)/100 = 0.5, then recovery
        dd = _max_drawdown([100, -50, 70])
        assert abs(dd - 0.5) < 1e-10

    def test_no_peak_means_zero(self):
        # all negative — equity never goes above 0, peak stays at 0
        dd = _max_drawdown([-10, -20, -30])
        assert dd == 0

    def test_multiple_drawdowns_picks_max(self):
        # equity: 100, 60, 80, 40 -> dd1 = 40/100 = 0.4, dd2 = 40/100 = 0.4
        dd = _max_drawdown([100, -40, 20, -40])
        # peak=100, equity=40 at end -> dd = 60/100 = 0.6
        assert abs(dd - 0.6) < 1e-10


# ── _max_consecutive_losses ───────────────────────────────────────────────

class TestMaxConsecutiveLosses:
    def test_empty(self):
        assert _max_consecutive_losses([]) == 0

    def test_all_wins(self):
        assert _max_consecutive_losses([10, 20, 30]) == 0

    def test_all_losses(self):
        assert _max_consecutive_losses([-1, -2, -3]) == 3

    def test_mixed(self):
        assert _max_consecutive_losses([10, -1, -2, 5, -3, -4, -5, 10]) == 3

    def test_single_loss(self):
        assert _max_consecutive_losses([10, -1, 10]) == 1

    def test_zero_is_not_a_loss(self):
        assert _max_consecutive_losses([0, 0, -1]) == 1


# ── _kelly_fraction ────────────────────────────────────────────────────────

class TestKellyFraction:
    def test_zero_avg_loss(self):
        assert _kelly_fraction(0.6, 0.05, 0) == 0

    def test_positive_edge(self):
        # b = 0.05/0.03 = 1.667, p=0.6, q=0.4
        # kelly = (1.667*0.6 - 0.4) / 1.667 = (1.0 - 0.4) / 1.667 = 0.36
        b = 0.05 / 0.03
        expected = (b * 0.6 - 0.4) / b
        result = _kelly_fraction(0.6, 0.05, 0.03)
        assert abs(result - expected) < 1e-10

    def test_no_edge(self):
        # 50/50 win rate, equal win/loss -> kelly = (1*0.5 - 0.5)/1 = 0
        assert _kelly_fraction(0.5, 0.03, 0.03) == 0

    def test_negative_edge_returns_zero(self):
        # Losing strategy: low win rate, small wins, big losses
        result = _kelly_fraction(0.3, 0.01, 0.05)
        assert result == 0

    def test_perfect_win_rate(self):
        # p=1, q=0 -> kelly = (b*1 - 0)/b = 1.0
        result = _kelly_fraction(1.0, 0.05, 0.03)
        assert abs(result - 1.0) < 1e-10


# ── compute_metrics (integration with mock DB) ────────────────────────────

class TestComputeMetrics:
    def _make_closed_positions(self, pnl_data):
        """Create mock closed positions from (strategy, pnl_pct, pnl_usd, hold_hours) tuples."""
        positions = []
        base_time = 1_700_000_000_000
        for i, (strategy, pnl_pct, pnl_usd, hold_h) in enumerate(pnl_data):
            p = make_position(
                strategy=strategy,
                pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                status="closed", exit_reason="trailing_stop",
                opened_at=base_time + i * 100_000,
                closed_at=base_time + i * 100_000 + hold_h * 3_600_000,
            )
            p.id = f"pos-{i}"
            positions.append(p)
        return positions

    @patch("src.evaluation.metrics.get_closed_trades")
    def test_empty_trades(self, mock_db):
        mock_db.return_value = []
        m = compute_metrics()
        assert m.total_trades == 0
        assert m.win_rate == 0
        assert m.sharpe_ratio is None
        assert m.sortino_ratio is None
        assert m.calmar_ratio is None

    @patch("src.evaluation.metrics.get_closed_trades")
    def test_all_winners(self, mock_db):
        mock_db.return_value = self._make_closed_positions([
            ("momentum_swing", 0.05, 50, 6),
            ("momentum_swing", 0.03, 30, 4),
            ("momentum_swing", 0.07, 70, 8),
        ])
        m = compute_metrics()
        assert m.total_trades == 3
        assert m.win_rate == 1.0
        assert m.profit_factor == float("inf")
        assert m.total_pnl_usd == 150
        assert m.max_drawdown_pct == 0

    @patch("src.evaluation.metrics.get_closed_trades")
    def test_all_losers(self, mock_db):
        mock_db.return_value = self._make_closed_positions([
            ("mean_reversion", -0.02, -20, 3),
            ("mean_reversion", -0.04, -40, 5),
        ])
        m = compute_metrics()
        assert m.win_rate == 0.0
        assert m.profit_factor == 0
        assert m.total_pnl_usd == -60

    @patch("src.evaluation.metrics.get_closed_trades")
    def test_mixed_trades_with_strategy_breakdown(self, mock_db):
        mock_db.return_value = self._make_closed_positions([
            ("momentum_swing", 0.05, 50, 6),
            ("momentum_swing", -0.02, -20, 3),
            ("mean_reversion", 0.03, 30, 4),
            ("mean_reversion", -0.01, -10, 2),
        ])
        m = compute_metrics()
        assert m.total_trades == 4
        assert m.win_rate == 0.5
        assert len(m.by_strategy) == 2

        # Strategies sorted by total_pnl_usd descending
        assert m.by_strategy[0].strategy == "momentum_swing"
        assert m.by_strategy[0].total_pnl_usd == 30
        assert m.by_strategy[1].strategy == "mean_reversion"
        assert m.by_strategy[1].total_pnl_usd == 20

    @patch("src.evaluation.metrics.get_closed_trades")
    def test_sharpe_requires_30_trades(self, mock_db):
        # 29 trades: no Sharpe
        trades = self._make_closed_positions(
            [("momentum_swing", 0.01, 10, 2)] * 29
        )
        mock_db.return_value = trades
        m = compute_metrics()
        assert m.sharpe_ratio is None

        # 30 trades: Sharpe should be computed
        trades = self._make_closed_positions(
            [("momentum_swing", 0.01, 10, 2)] * 15
            + [("momentum_swing", -0.005, -5, 1)] * 15
        )
        mock_db.return_value = trades
        m = compute_metrics()
        assert m.sharpe_ratio is not None

    @patch("src.evaluation.metrics.get_closed_trades")
    def test_hold_hours(self, mock_db):
        mock_db.return_value = self._make_closed_positions([
            ("momentum_swing", 0.05, 50, 10),
            ("momentum_swing", 0.03, 30, 6),
        ])
        m = compute_metrics()
        assert abs(m.avg_hold_hours - 8.0) < 0.01


class TestFormatMetrics:
    def test_format_basic(self):
        from src.evaluation.metrics import PortfolioMetrics
        m = PortfolioMetrics(
            total_trades=10, win_rate=0.6, profit_factor=1.5,
            total_pnl_usd=100, sharpe_ratio=1.2, sortino_ratio=1.5,
            calmar_ratio=2.0, max_drawdown_pct=0.1, avg_hold_hours=5.0,
        )
        text = format_metrics(m)
        assert "10" in text
        assert "60.0%" in text
        assert "1.50" in text  # profit factor
        assert "$100.00" in text
        assert "1.20" in text  # sharpe

    def test_format_with_none_ratios(self):
        from src.evaluation.metrics import PortfolioMetrics
        m = PortfolioMetrics(
            total_trades=5, win_rate=0.5, profit_factor=1.0,
            total_pnl_usd=0, sharpe_ratio=None, sortino_ratio=None,
            calmar_ratio=None, max_drawdown_pct=0, avg_hold_hours=3.0,
        )
        text = format_metrics(m)
        assert "insufficient data" in text
