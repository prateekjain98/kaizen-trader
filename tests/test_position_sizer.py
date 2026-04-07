"""Tests for Kelly Criterion position sizing."""

import pytest
from unittest.mock import patch

from src.risk.position_sizer import kelly_size, log_kelly_rationale, StrategyStats


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
