"""Tests for batch 2 features: adaptive stops, hourly stats, consecutive loss cooldown."""

import time
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

from tests.conftest import make_position, _now_ms


# ─── Adaptive Stop-Loss ─────────────────────────────────────────────────────

class TestAdaptiveStops:
    def test_fallback_with_insufficient_history(self):
        from src.risk.adaptive_stops import compute_adaptive_stop, _mae_cache
        _mae_cache.clear()

        with patch("src.risk.adaptive_stops.get_closed_trades", return_value=[]):
            result = compute_adaptive_stop("momentum_swing", 0.07)
            assert result == 0.07  # falls back to default

    def test_computes_from_winning_trades(self):
        from src.risk.adaptive_stops import compute_adaptive_stop, _mae_cache
        _mae_cache.clear()

        # Create 25 winning trades with known MAE values
        trades = []
        for i in range(25):
            pos = make_position(
                strategy="momentum_swing", pnl_pct=0.03,
                closed_at=_now_ms(), status="closed",
            )
            pos.mae_pct = -0.02 - (i * 0.001)  # MAE ranges from -0.02 to -0.044
            trades.append(pos)

        with patch("src.risk.adaptive_stops.get_closed_trades", return_value=trades):
            result = compute_adaptive_stop("momentum_swing", 0.07)
            # 80th percentile of abs(MAE) values, with 10% buffer
            assert 0.01 <= result <= 0.15
            assert result != 0.07  # should not be the fallback

    def test_caches_result(self):
        from src.risk.adaptive_stops import compute_adaptive_stop, _mae_cache
        _mae_cache.clear()

        with patch("src.risk.adaptive_stops.get_closed_trades", return_value=[]) as mock:
            compute_adaptive_stop("test_strat", 0.05)
            compute_adaptive_stop("test_strat", 0.05)
            # Should only call get_closed_trades once due to caching
            assert mock.call_count == 1

    def test_clamps_to_valid_range(self):
        from src.risk.adaptive_stops import compute_adaptive_stop, _mae_cache
        _mae_cache.clear()

        # Trades with extremely high MAE (should clamp to 15%)
        trades = []
        for i in range(25):
            pos = make_position(strategy="test_extreme", pnl_pct=0.01,
                                closed_at=_now_ms(), status="closed")
            pos.mae_pct = -0.50  # 50% drawdown — extreme
            trades.append(pos)

        with patch("src.risk.adaptive_stops.get_closed_trades", return_value=trades):
            result = compute_adaptive_stop("test_extreme", 0.07)
            assert result <= 0.15  # clamped to max

    def test_ignores_losing_trades(self):
        from src.risk.adaptive_stops import compute_adaptive_stop, _mae_cache
        _mae_cache.clear()

        # Mix of winners and losers, but not enough winners
        trades = []
        for i in range(15):
            pos = make_position(strategy="mixed", pnl_pct=0.03,
                                closed_at=_now_ms(), status="closed")
            pos.mae_pct = -0.02
            trades.append(pos)
        for i in range(30):
            pos = make_position(strategy="mixed", pnl_pct=-0.05,
                                closed_at=_now_ms(), status="closed")
            pos.mae_pct = -0.10
            trades.append(pos)

        with patch("src.risk.adaptive_stops.get_closed_trades", return_value=trades):
            result = compute_adaptive_stop("mixed", 0.07)
            assert result == 0.07  # not enough winners (only 15 < 20)


# ─── Hourly Stats Heat Map ──────────────────────────────────────────────────

class TestHourlyStats:
    def test_returns_24_buckets(self):
        from src.evaluation.hourly_stats import get_hourly_stats, _cache
        _cache.clear()

        with patch("src.evaluation.hourly_stats.get_closed_trades", return_value=[]):
            buckets = get_hourly_stats("momentum_swing")
            assert len(buckets) == 24
            assert all(b.hour == i for i, b in enumerate(buckets))

    def test_empty_bucket_properties(self):
        from src.evaluation.hourly_stats import HourlyBucket
        b = HourlyBucket(hour=12)
        assert b.win_rate == 0.0
        assert b.avg_pnl_pct == 0.0
        assert b.trades == 0

    def test_bucket_with_trades(self):
        from src.evaluation.hourly_stats import HourlyBucket
        b = HourlyBucket(hour=14, trades=10, wins=7, total_pnl_pct=0.15)
        assert b.win_rate == 0.7
        assert abs(b.avg_pnl_pct - 0.015) < 0.001

    def test_hour_adjustment_positive(self):
        from src.evaluation.hourly_stats import get_hour_adjustment, _cache
        _cache.clear()

        # Create trades that win heavily at hour 14
        from datetime import datetime, timezone
        trades = []
        for i in range(10):
            pos = make_position(strategy="test_hourly", pnl_pct=0.05,
                                closed_at=_now_ms(), status="closed")
            # Set opened_at to be at 14:00 UTC
            dt = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
            pos.opened_at = dt.timestamp() * 1000
            trades.append(pos)

        with patch("src.evaluation.hourly_stats.get_closed_trades", return_value=trades):
            adj = get_hour_adjustment("test_hourly", hour=14)
            assert adj > 0  # should be positive (100% win rate > 50%)

    def test_hour_adjustment_negative(self):
        from src.evaluation.hourly_stats import get_hour_adjustment, _cache
        _cache.clear()

        from datetime import datetime, timezone
        trades = []
        for i in range(10):
            pos = make_position(strategy="test_bad_hour", pnl_pct=-0.05,
                                closed_at=_now_ms(), status="closed")
            dt = datetime(2026, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
            pos.opened_at = dt.timestamp() * 1000
            trades.append(pos)

        with patch("src.evaluation.hourly_stats.get_closed_trades", return_value=trades):
            adj = get_hour_adjustment("test_bad_hour", hour=3)
            assert adj < 0  # should be negative (0% win rate < 50%)

    def test_hour_adjustment_insufficient_data(self):
        from src.evaluation.hourly_stats import get_hour_adjustment, _cache
        _cache.clear()

        with patch("src.evaluation.hourly_stats.get_closed_trades", return_value=[]):
            adj = get_hour_adjustment("no_trades", hour=12)
            assert adj == 0.0  # insufficient data

    def test_caches_result(self):
        from src.evaluation.hourly_stats import get_hourly_stats, _cache
        _cache.clear()

        with patch("src.evaluation.hourly_stats.get_closed_trades", return_value=[]) as mock:
            get_hourly_stats("cached_test")
            get_hourly_stats("cached_test")
            assert mock.call_count == 1


# ─── Consecutive Loss Cooldown ───────────────────────────────────────────────

class TestConsecutiveLossCooldown:
    def _reset(self):
        from src.risk.loss_cooldown import _lock, _consecutive_losses, _cooldown_until
        with _lock:
            _consecutive_losses.clear()
            _cooldown_until.clear()

    def test_no_cooldown_by_default(self):
        from src.risk.loss_cooldown import is_on_cooldown
        self._reset()
        assert is_on_cooldown("momentum_swing") is False

    def test_wins_reset_counter(self):
        from src.risk.loss_cooldown import record_trade_result, get_consecutive_losses
        self._reset()

        with patch("src.risk.loss_cooldown.log"):
            record_trade_result("test_strat", is_win=False)
            record_trade_result("test_strat", is_win=False)
            assert get_consecutive_losses("test_strat") == 2

            record_trade_result("test_strat", is_win=True)
            assert get_consecutive_losses("test_strat") == 0

    def test_three_losses_triggers_cooldown(self):
        from src.risk.loss_cooldown import record_trade_result, is_on_cooldown
        self._reset()

        with patch("src.risk.loss_cooldown.log"):
            record_trade_result("bad_strat", is_win=False)
            assert is_on_cooldown("bad_strat") is False
            record_trade_result("bad_strat", is_win=False)
            assert is_on_cooldown("bad_strat") is False
            record_trade_result("bad_strat", is_win=False)
            assert is_on_cooldown("bad_strat") is True

    def test_cooldown_expires(self):
        from src.risk.loss_cooldown import (
            record_trade_result, is_on_cooldown,
            _cooldown_until, _lock,
        )
        self._reset()

        with patch("src.risk.loss_cooldown.log"):
            record_trade_result("expire_test", is_win=False)
            record_trade_result("expire_test", is_win=False)
            record_trade_result("expire_test", is_win=False)
            assert is_on_cooldown("expire_test") is True

            # Manually expire the cooldown
            with _lock:
                _cooldown_until["expire_test"] = time.time() - 1

            assert is_on_cooldown("expire_test") is False

    def test_different_strategies_independent(self):
        from src.risk.loss_cooldown import record_trade_result, is_on_cooldown
        self._reset()

        with patch("src.risk.loss_cooldown.log"):
            for _ in range(3):
                record_trade_result("strat_a", is_win=False)

            assert is_on_cooldown("strat_a") is True
            assert is_on_cooldown("strat_b") is False

    def test_cooldown_remaining(self):
        from src.risk.loss_cooldown import (
            record_trade_result, get_cooldown_remaining_s,
        )
        self._reset()

        with patch("src.risk.loss_cooldown.log"):
            for _ in range(3):
                record_trade_result("remain_test", is_win=False)

            remaining = get_cooldown_remaining_s("remain_test")
            assert remaining > 0
            assert remaining <= 1800  # should be <= 30 minutes

    def test_no_cooldown_remaining_when_not_cooling(self):
        from src.risk.loss_cooldown import get_cooldown_remaining_s
        self._reset()
        assert get_cooldown_remaining_s("no_cooldown") == 0.0

    def test_win_after_cooldown_resets(self):
        from src.risk.loss_cooldown import (
            record_trade_result, is_on_cooldown, get_consecutive_losses,
            _cooldown_until, _lock,
        )
        self._reset()

        with patch("src.risk.loss_cooldown.log"):
            for _ in range(3):
                record_trade_result("reset_test", is_win=False)
            assert is_on_cooldown("reset_test") is True

            # Expire cooldown
            with _lock:
                _cooldown_until["reset_test"] = time.time() - 1

            # Record a win
            record_trade_result("reset_test", is_win=True)
            assert get_consecutive_losses("reset_test") == 0
            assert is_on_cooldown("reset_test") is False
