"""Tests for Kelly Criterion position sizing."""

import pytest
from dataclasses import dataclass
from unittest.mock import patch

from src.risk.position_sizer import (
    kelly_size, log_kelly_rationale, StrategyStats,
    apply_correlation_discount, apply_drawdown_scaling, update_peak,
)


def _mock_stats(win_rate=0.6, avg_win_pct=0.05, avg_loss_pct=0.03, sample_size=50):
    return StrategyStats(
        win_rate=win_rate, avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct, sample_size=sample_size,
    )


class TestKellySize:
    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_insufficient_history_uses_fixed_fraction(self, mock_stats):
        mock_stats.return_value = _mock_stats(sample_size=5)
        size = kelly_size("momentum_swing", 10_000, 70)
        # fraction = 0.01, qual_mult = 0.5 + 70/100 = 1.2
        # raw = 0.01 * 10000 * 1.2 = 120
        # clamped to [10, 100(env default)]
        assert size == 100  # clamped to max_position_usd

    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_positive_kelly(self, mock_stats):
        mock_stats.return_value = _mock_stats(
            win_rate=0.6, avg_win_pct=0.05, avg_loss_pct=0.03, sample_size=50
        )
        size = kelly_size("momentum_swing", 10_000, 70)
        # b = 0.05/0.03 = 1.667, p=0.6, q=0.4
        # raw_kelly = (1.667*0.6 - 0.4) / 1.667 = 0.36
        # fraction = 0.36 * 0.25 = 0.09
        # qual_mult = 0.5 + 0.7 = 1.2
        # raw_usd = 0.09 * 10000 * 1.2 = 1080
        # clamped to [10, 100] = 100
        assert size == 100  # clamped to env.max_position_usd (100)

    @patch("src.risk.position_sizer._compute_strategy_stats")
    @patch("src.risk.position_sizer.env")
    def test_large_max_position(self, mock_env, mock_stats):
        mock_env.max_position_usd = 5000
        mock_stats.return_value = _mock_stats(
            win_rate=0.6, avg_win_pct=0.05, avg_loss_pct=0.03, sample_size=50
        )
        size = kelly_size("momentum_swing", 10_000, 70)
        # raw_usd = 0.09 * 10000 * 1.0 = 900 (qual_multiplier capped at 1.0)
        assert abs(size - 900) < 1

    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_losing_strategy_returns_zero(self, mock_stats):
        mock_stats.return_value = _mock_stats(
            win_rate=0.3, avg_win_pct=0.01, avg_loss_pct=0.05, sample_size=50
        )
        size = kelly_size("bad_strategy", 10_000, 70)
        # b = 0.01/0.05 = 0.2, p=0.3, q=0.7
        # raw_kelly = (0.2*0.3 - 0.7)/0.2 = (0.06-0.7)/0.2 = -3.2
        # raw_kelly <= 0 -> return 0
        assert size == 0

    @patch("src.risk.position_sizer._compute_strategy_stats")
    @patch("src.risk.position_sizer.env")
    def test_min_size_enforcement(self, mock_env, mock_stats):
        mock_env.max_position_usd = 5000
        mock_stats.return_value = _mock_stats(
            win_rate=0.51, avg_win_pct=0.01, avg_loss_pct=0.01, sample_size=50
        )
        # b=1, p=0.51, q=0.49 -> kelly = (0.51-0.49)/1 = 0.02
        # fraction = 0.02 * 0.25 = 0.005
        # qual_mult with score=10: 0.5 + 0.1 = 0.6
        # raw = 0.005 * 100 * 0.6 = 0.3 -> clamped to min=10
        size = kelly_size("thin_edge", 100, 10)
        assert size == 10  # min size

    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_qual_score_0_gives_half_multiplier(self, mock_stats):
        mock_stats.return_value = _mock_stats(sample_size=5)
        size_low = kelly_size("test", 10_000, 0)
        # fraction=0.01, qual_mult=0.5, raw=50 -> clamped to [10,100]
        assert size_low == 50

    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_qual_score_100_gives_1_5x_multiplier(self, mock_stats):
        mock_stats.return_value = _mock_stats(sample_size=5)
        size_high = kelly_size("test", 10_000, 100)
        # fraction=0.01, qual_mult=1.5, raw=150 -> clamped to [10,100]
        assert size_high == 100

    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_zero_avg_loss_fallback(self, mock_stats):
        mock_stats.return_value = _mock_stats(
            win_rate=1.0, avg_win_pct=0.05, avg_loss_pct=0, sample_size=50
        )
        # b = 0.05/0 -> b=1 (fallback), raw_kelly = (1*1 - 0)/1 = 1
        # fraction = 1 * 0.25 = 0.25
        size = kelly_size("all_wins", 10_000, 70)
        assert size > 0


class TestLogKellyRationale:
    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_insufficient_history_message(self, mock_stats):
        mock_stats.return_value = _mock_stats(sample_size=5)
        msg = log_kelly_rationale("test_strategy")
        assert "insufficient history" in msg
        assert "5/30" in msg

    @patch("src.risk.position_sizer._compute_strategy_stats")
    def test_full_rationale(self, mock_stats):
        mock_stats.return_value = _mock_stats(
            win_rate=0.6, avg_win_pct=0.05, avg_loss_pct=0.03, sample_size=50
        )
        msg = log_kelly_rationale("momentum_swing")
        assert "win_rate=60%" in msg
        assert "avg_win=5.0%" in msg
        assert "quarter_kelly=" in msg


@dataclass
class _FakePosition:
    symbol: str
    side: str


class TestCorrelationDiscount:
    def test_no_correlated_positions_no_discount(self):
        result = apply_correlation_discount(1000, "SOL-USD", "long", [])
        assert result == 1000

    def test_unknown_symbol_no_discount(self):
        result = apply_correlation_discount(1000, "OBSCURE-USD", "long", [
            _FakePosition("SOL-USD", "long"),
        ])
        assert result == 1000

    def test_one_correlated_position_30pct_reduction(self):
        result = apply_correlation_discount(1000, "SOL-USD", "long", [
            _FakePosition("AVAX-USD", "long"),  # same alt_l1 group
        ])
        assert abs(result - 700) < 1  # 30% reduction

    def test_two_correlated_positions(self):
        result = apply_correlation_discount(1000, "SOL-USD", "long", [
            _FakePosition("AVAX-USD", "long"),
            _FakePosition("NEAR-USD", "long"),
        ])
        assert abs(result - 400) < 1  # 60% reduction

    def test_three_correlated_hits_floor(self):
        result = apply_correlation_discount(1000, "SOL-USD", "long", [
            _FakePosition("AVAX-USD", "long"),
            _FakePosition("NEAR-USD", "long"),
            _FakePosition("SUI-USD", "long"),
        ])
        assert abs(result - 250) < 1  # floor at 25%

    def test_opposite_side_not_correlated(self):
        result = apply_correlation_discount(1000, "SOL-USD", "long", [
            _FakePosition("AVAX-USD", "short"),  # opposite side
        ])
        assert result == 1000  # no discount

    def test_same_symbol_ignored(self):
        result = apply_correlation_discount(1000, "SOL-USD", "long", [
            _FakePosition("SOL-USD", "long"),  # same symbol, handled elsewhere
        ])
        assert result == 1000

    def test_different_groups_no_discount(self):
        result = apply_correlation_discount(1000, "BTC-USD", "long", [
            _FakePosition("ETH-USD", "long"),  # different group
            _FakePosition("SOL-USD", "long"),  # different group
        ])
        assert result == 1000


class TestDrawdownScaling:
    def test_no_peak_no_scaling(self):
        result = apply_drawdown_scaling(1000, 10000)
        # peak is 0 initially, so no scaling
        assert result == 1000

    def test_at_peak_no_scaling(self):
        import src.risk.position_sizer as ps
        with ps._peak_lock:
            old_peak = ps._peak_portfolio_usd
        update_peak(10000)
        result = apply_drawdown_scaling(1000, 10000)
        assert result == 1000
        # Restore
        with ps._peak_lock:
            ps._peak_portfolio_usd = old_peak

    def test_5pct_drawdown_75pct_size(self):
        import src.risk.position_sizer as ps
        with ps._peak_lock:
            old_peak = ps._peak_portfolio_usd
        update_peak(10000)
        result = apply_drawdown_scaling(1000, 9500)  # 5% drawdown
        assert abs(result - 750) < 1
        with ps._peak_lock:
            ps._peak_portfolio_usd = old_peak

    def test_20pct_drawdown_10pct_size(self):
        import src.risk.position_sizer as ps
        with ps._peak_lock:
            old_peak = ps._peak_portfolio_usd
        update_peak(10000)
        result = apply_drawdown_scaling(1000, 8000)  # 20% drawdown
        assert abs(result - 100) < 1
        with ps._peak_lock:
            ps._peak_portfolio_usd = old_peak
