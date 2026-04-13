"""Tests for the self-healing engine — loss classification and parameter adaptation."""

import time
import pytest
from dataclasses import asdict
from unittest.mock import patch

from src.self_healing.healer import (
    _clamp, _classify_loss_reason, _apply_loss_adaptation,
    on_position_closed, reset_session_count, _MAX_ADAPTATIONS_PER_SESSION,
)
from src.config import CONFIG_BOUNDS
from src.types import ScannerConfig, Position
from tests.conftest import make_position


# ── _clamp ─────────────────────────────────────────────────────────────────

class TestHealerClamp:
    def test_within_bounds(self):
        assert _clamp(0.05, "momentum_pct_swing") == 0.05

    def test_below_lower_bound(self):
        lo, _ = CONFIG_BOUNDS["momentum_pct_swing"]
        assert _clamp(0.001, "momentum_pct_swing") == lo

    def test_above_upper_bound(self):
        _, hi = CONFIG_BOUNDS["momentum_pct_swing"]
        assert _clamp(0.99, "momentum_pct_swing") == hi

    def test_all_keys_have_valid_bounds(self):
        for key, (lo, hi) in CONFIG_BOUNDS.items():
            assert lo < hi, f"{key}: lower bound {lo} >= upper bound {hi}"


# ── _classify_loss_reason ─────────────────────────────────────────────────

class TestClassifyLossReason:
    def test_entered_pump_top(self):
        """High momentum at entry + short hold -> pump top."""
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1800,
            opened_at=now - 2 * 3_600_000,  # 2 hours ago
            closed_at=now, pnl_pct=-0.03,
            exit_reason="trailing_stop", status="closed",
        )
        p.momentum_at_entry = 0.10  # high momentum triggers entered_pump_top
        assert _classify_loss_reason(p, ScannerConfig()) == "entered_pump_top"

    def test_stop_too_tight(self):
        """Short hold + trailing stop exit -> stop too tight."""
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1990,  # low momentum at entry
            opened_at=now - 1 * 3_600_000,  # 1 hour
            closed_at=now, pnl_pct=-0.01,
            exit_reason="trailing_stop", status="closed",
        )
        assert _classify_loss_reason(p, ScannerConfig()) == "stop_too_tight"

    def test_stop_too_wide(self):
        """Long hold + big loss -> stop too wide."""
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1990,
            opened_at=now - 24 * 3_600_000,  # 24 hours
            closed_at=now, pnl_pct=-0.06,
            exit_reason="trailing_stop", status="closed",
        )
        assert _classify_loss_reason(p, ScannerConfig()) == "stop_too_wide"

    def test_low_qual_score(self):
        """Low quality score but no other conditions met -> low qual."""
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1990,
            opened_at=now - 5 * 3_600_000,  # 5 hours
            closed_at=now, pnl_pct=-0.02,
            exit_reason="trailing_stop", status="closed",
            qual_score=50,
        )
        assert _classify_loss_reason(p, ScannerConfig()) == "low_qual_score"

    def test_unknown(self):
        """No specific pattern matched -> unknown."""
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1990,
            opened_at=now - 5 * 3_600_000,
            closed_at=now, pnl_pct=-0.02,
            exit_reason="trailing_stop", status="closed",
            qual_score=70,
        )
        assert _classify_loss_reason(p, ScannerConfig()) == "unknown"

    def test_tight_stop_priority_over_pump_top(self):
        """When both pump top and tight stop conditions met, stop_too_tight wins.

        Short holds with trailing_stop exits are definitively stop-related,
        while momentum_at_entry can be coincidental.
        """
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1800,
            opened_at=now - 1 * 3_600_000,  # 1 hour (< 2h for tight, < 4h for pump)
            closed_at=now, pnl_pct=-0.03,
            exit_reason="trailing_stop", status="closed",
        )
        p.momentum_at_entry = 0.10  # high momentum triggers pump top
        assert _classify_loss_reason(p, ScannerConfig()) == "stop_too_tight"

    def test_pump_top_when_not_tight_stop(self):
        """Pump top detected when hold > 2h (not tight stop)."""
        now = time.time() * 1000
        p = make_position(
            entry_price=2000, low_watermark=1800,
            opened_at=now - 3 * 3_600_000,  # 3 hours (> 2h, still < 4h)
            closed_at=now, pnl_pct=-0.05,
            exit_reason="trailing_stop", status="closed",
        )
        p.momentum_at_entry = 0.10
        assert _classify_loss_reason(p, ScannerConfig()) == "entered_pump_top"


# ── _apply_loss_adaptation ────────────────────────────────────────────────

class TestApplyLossAdaptation:
    def test_pump_top_raises_momentum_swing(self):
        config = ScannerConfig()
        old_val = config.momentum_pct_swing
        p = make_position(tier="swing")
        result = _apply_loss_adaptation(p, "entered_pump_top", config)
        assert config.momentum_pct_swing == old_val + 0.01
        assert "momentum_pct_swing" in result["changes"]
        assert "raise" in result["action"]

    def test_pump_top_raises_momentum_scalp(self):
        config = ScannerConfig()
        old_val = config.momentum_pct_scalp
        p = make_position(tier="scalp")
        result = _apply_loss_adaptation(p, "entered_pump_top", config)
        assert config.momentum_pct_scalp == old_val + 0.01

    def test_stop_too_tight_widens_trail(self):
        config = ScannerConfig()
        old_val = config.base_trail_pct_swing
        p = make_position(tier="swing")
        result = _apply_loss_adaptation(p, "stop_too_tight", config)
        assert config.base_trail_pct_swing == old_val + 0.01
        assert "widen" in result["action"]

    def test_stop_too_wide_tightens_trail(self):
        config = ScannerConfig()
        old_val = config.base_trail_pct_swing
        p = make_position(tier="swing")
        result = _apply_loss_adaptation(p, "stop_too_wide", config)
        assert config.base_trail_pct_swing == old_val - 0.01
        assert "tighten" in result["action"]

    def test_low_qual_raises_min_score(self):
        config = ScannerConfig()
        old_val = config.min_qual_score_swing
        p = make_position(tier="swing")
        result = _apply_loss_adaptation(p, "low_qual_score", config)
        assert config.min_qual_score_swing == old_val + 2

    def test_funding_squeeze_raises_threshold(self):
        config = ScannerConfig()
        old_val = config.funding_rate_extreme_threshold
        p = make_position()
        result = _apply_loss_adaptation(p, "funding_squeeze", config)
        assert abs(config.funding_rate_extreme_threshold - (old_val + 0.0002)) < 1e-10

    def test_unknown_makes_no_change(self):
        config = ScannerConfig()
        original = asdict(config)
        p = make_position()
        result = _apply_loss_adaptation(p, "unknown", config)
        assert asdict(config) == original
        assert result["changes"] == {}

    def test_respects_config_bounds(self):
        config = ScannerConfig()
        _, hi = CONFIG_BOUNDS["momentum_pct_swing"]
        config.momentum_pct_swing = hi  # at upper bound
        p = make_position(tier="swing")
        _apply_loss_adaptation(p, "entered_pump_top", config)
        assert config.momentum_pct_swing == hi  # didn't exceed bound

    def test_respects_lower_bounds(self):
        config = ScannerConfig()
        lo, _ = CONFIG_BOUNDS["base_trail_pct_swing"]
        config.base_trail_pct_swing = lo  # at lower bound
        p = make_position(tier="swing")
        _apply_loss_adaptation(p, "stop_too_wide", config)
        assert config.base_trail_pct_swing == lo  # didn't go below bound


# ── on_position_closed ────────────────────────────────────────────────────

class TestOnPositionClosed:
    @patch("src.self_healing.healer.insert_diagnosis")
    @patch("src.self_healing.healer.snapshot_config")
    @patch("src.self_healing.healer.log")
    def test_skips_winners(self, mock_log, mock_snap, mock_diag):
        reset_session_count()
        config = ScannerConfig()
        p = make_position(pnl_pct=0.05, pnl_usd=50, status="closed",
                          exit_reason="take_profit")
        on_position_closed(p, config, "bull")
        mock_diag.assert_not_called()
        mock_snap.assert_not_called()

    @patch("src.self_healing.healer.insert_diagnosis")
    @patch("src.self_healing.healer.snapshot_config")
    @patch("src.self_healing.healer.log")
    def test_skips_small_losses(self, mock_log, mock_snap, mock_diag):
        reset_session_count()
        config = ScannerConfig()
        p = make_position(pnl_pct=-0.003, pnl_usd=-3, status="closed",
                          exit_reason="trailing_stop")
        on_position_closed(p, config, "neutral")
        mock_diag.assert_not_called()

    @patch("src.self_healing.healer.insert_diagnosis")
    @patch("src.self_healing.healer.snapshot_config")
    @patch("src.self_healing.healer.log")
    def test_processes_real_loss(self, mock_log, mock_snap, mock_diag):
        reset_session_count()
        config = ScannerConfig()
        now = time.time() * 1000
        p = make_position(
            pnl_pct=-0.02, pnl_usd=-20, status="closed",
            exit_reason="trailing_stop", qual_score=50,
            opened_at=now - 5 * 3_600_000, closed_at=now,
            low_watermark=1990,
        )
        on_position_closed(p, config, "neutral")
        mock_diag.assert_called_once()
        mock_snap.assert_called_once()

    @patch("src.self_healing.healer.insert_diagnosis")
    @patch("src.self_healing.healer.snapshot_config")
    @patch("src.self_healing.healer.log")
    def test_session_cap(self, mock_log, mock_snap, mock_diag):
        reset_session_count()
        config = ScannerConfig()
        now = time.time() * 1000

        # Fill up to the cap
        for i in range(_MAX_ADAPTATIONS_PER_SESSION):
            p = make_position(
                pnl_pct=-0.02, pnl_usd=-20, status="closed",
                exit_reason="trailing_stop", qual_score=50,
                opened_at=now - 5 * 3_600_000, closed_at=now,
                low_watermark=1990,
            )
            p.id = f"cap-test-{i}"
            on_position_closed(p, config, "neutral")

        assert mock_diag.call_count == _MAX_ADAPTATIONS_PER_SESSION

        # 21st call should be skipped
        mock_diag.reset_mock()
        p = make_position(
            pnl_pct=-0.02, pnl_usd=-20, status="closed",
            exit_reason="trailing_stop", qual_score=50,
            opened_at=now - 5 * 3_600_000, closed_at=now,
            low_watermark=1990,
        )
        p.id = "cap-test-over"
        on_position_closed(p, config, "neutral")
        mock_diag.assert_not_called()
