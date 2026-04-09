"""Tests for the delta evaluator — parameter change tracking and auto-revert."""

import time
import uuid
from unittest.mock import patch, MagicMock

import pytest

from src.self_healing.delta_evaluator import (
    DeltaEvaluator, ParameterDelta, TradeSnapshot, get_evaluator,
)
from src.config import CONFIG_BOUNDS
from src.types import ScannerConfig, Position
from tests.conftest import make_position


def _make_trades(count: int, win_rate: float, avg_pnl: float,
                 base_time: float = None) -> list[Position]:
    """Create a list of mock closed positions with the given win rate and avg PnL."""
    base_time = base_time or time.time() * 1000
    trades = []
    wins = int(count * win_rate)
    for i in range(count):
        is_win = i < wins
        pnl = avg_pnl * 2 if is_win else -(avg_pnl * 2 * wins / max(count - wins, 1))
        # Adjust so average PnL is roughly avg_pnl
        p = make_position(
            pnl_pct=abs(avg_pnl) if is_win else -abs(avg_pnl),
            pnl_usd=100 * (abs(avg_pnl) if is_win else -abs(avg_pnl)),
            status="closed",
            exit_reason="trailing_stop",
            closed_at=base_time + i * 1000,
        )
        p.id = f"test-{uuid.uuid4().hex[:8]}"
        trades.append(p)
    return trades


# ── TradeSnapshot computation ────────────────────────────────────────────


class TestComputeSnapshot:
    def test_empty_trades(self):
        ev = DeltaEvaluator()
        snap = ev._compute_snapshot([])
        assert snap.win_rate == 0.0
        assert snap.avg_pnl_pct == 0.0
        assert snap.count == 0

    def test_all_winners(self):
        ev = DeltaEvaluator()
        trades = _make_trades(5, win_rate=1.0, avg_pnl=0.02)
        snap = ev._compute_snapshot(trades)
        assert snap.win_rate == 1.0
        assert snap.avg_pnl_pct > 0
        assert snap.count == 5

    def test_all_losers(self):
        ev = DeltaEvaluator()
        trades = _make_trades(5, win_rate=0.0, avg_pnl=0.02)
        snap = ev._compute_snapshot(trades)
        assert snap.win_rate == 0.0
        assert snap.avg_pnl_pct < 0
        assert snap.count == 5

    def test_mixed_trades(self):
        ev = DeltaEvaluator()
        trades = _make_trades(10, win_rate=0.6, avg_pnl=0.01)
        snap = ev._compute_snapshot(trades)
        assert 0.5 <= snap.win_rate <= 0.7
        assert snap.count == 10


# ── record_delta ─────────────────────────────────────────────────────────


class TestRecordDelta:
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_records_with_correct_snapshot(self, mock_log, mock_get_trades):
        trades = _make_trades(10, win_rate=0.6, avg_pnl=0.01)
        mock_get_trades.return_value = trades

        ev = DeltaEvaluator()
        config = ScannerConfig()
        delta = ev.record_delta(
            parameter="momentum_pct_swing",
            old_value=0.02, new_value=0.03,
            reason="entered_pump_top", source="immediate_healer",
            config=config,
        )

        assert delta.parameter == "momentum_pct_swing"
        assert delta.old_value == 0.02
        assert delta.new_value == 0.03
        assert delta.source == "immediate_healer"
        assert delta.evaluation_status == "pending"
        assert delta.trades_before.count == 10
        assert delta.trades_before.win_rate == pytest.approx(0.6, abs=0.01)
        assert len(ev.get_all_deltas()) == 1

    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_records_claude_source(self, mock_log, mock_get_trades):
        mock_get_trades.return_value = []

        ev = DeltaEvaluator()
        config = ScannerConfig()
        delta = ev.record_delta(
            parameter="base_trail_pct_swing",
            old_value=0.07, new_value=0.08,
            reason="win rate too low", source="claude_analysis",
            config=config,
        )

        assert delta.source == "claude_analysis"
        assert delta.trades_before.count == 0


# ── evaluate_pending_deltas ──────────────────────────────────────────────


class TestEvaluatePendingDeltas:
    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_stays_pending_with_insufficient_trades(self, mock_log, mock_get_trades, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()

        # Record a delta with empty before-snapshot
        mock_get_trades.return_value = []
        ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test", "immediate_healer", config)

        # Only 5 trades after the delta (below MIN_TRADES_FOR_EVAL=10)
        now = time.time() * 1000
        after_trades = _make_trades(5, win_rate=0.5, avg_pnl=0.01, base_time=now + 1000)
        mock_get_trades.return_value = after_trades

        evaluated = ev.evaluate_pending_deltas(config)
        assert len(evaluated) == 0
        assert len(ev.get_pending_deltas()) == 1

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_improved_verdict(self, mock_log, mock_get_trades, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()

        # Before snapshot: 40% win rate
        before_trades = _make_trades(20, win_rate=0.4, avg_pnl=0.005)
        mock_get_trades.return_value = before_trades
        delta = ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test", "immediate_healer", config)

        # After: 60% win rate (big improvement) — need >= MIN_TRADES_FOR_EVAL (25)
        now = time.time() * 1000
        after_trades = _make_trades(30, win_rate=0.7, avg_pnl=0.02, base_time=delta.timestamp + 1)
        mock_get_trades.return_value = after_trades

        evaluated = ev.evaluate_pending_deltas(config)
        assert len(evaluated) == 1
        assert evaluated[0].verdict == "improved"
        assert evaluated[0].evaluation_status == "evaluated"

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_worsened_verdict_triggers_revert(self, mock_log, mock_get_trades, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()

        # Before snapshot: 60% win rate
        before_trades = _make_trades(20, win_rate=0.6, avg_pnl=0.02)
        mock_get_trades.return_value = before_trades
        delta = ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test", "immediate_healer", config)

        # Set config to the new value
        config.momentum_pct_swing = 0.03

        # After: 30% win rate (big drop)
        now = time.time() * 1000
        after_trades = _make_trades(30, win_rate=0.3, avg_pnl=-0.01, base_time=delta.timestamp + 1)
        mock_get_trades.return_value = after_trades

        evaluated = ev.evaluate_pending_deltas(config)
        assert len(evaluated) == 1
        assert evaluated[0].verdict == "worsened"
        assert evaluated[0].evaluation_status == "reverted"
        # Config should be reverted to old value
        assert config.momentum_pct_swing == 0.02

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_neutral_verdict(self, mock_log, mock_get_trades, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()

        # Before snapshot: 50% win rate
        before_trades = _make_trades(20, win_rate=0.5, avg_pnl=0.01)
        mock_get_trades.return_value = before_trades
        delta = ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test", "immediate_healer", config)

        # After: ~37% win rate (11/30 wins) — snapshot takes first 20 → 11/20=55%, +5% vs 50% before, below 8% threshold
        now = time.time() * 1000
        after_trades = _make_trades(30, win_rate=0.37, avg_pnl=0.01, base_time=delta.timestamp + 1)
        mock_get_trades.return_value = after_trades

        evaluated = ev.evaluate_pending_deltas(config)
        assert len(evaluated) == 1
        assert evaluated[0].verdict == "neutral"
        assert evaluated[0].evaluation_status == "evaluated"

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_max_reverts_per_cycle(self, mock_log, mock_get_trades, mock_snap):
        """Only 1 revert per evaluation cycle."""
        ev = DeltaEvaluator()
        config = ScannerConfig()

        # Record two deltas
        before_trades = _make_trades(20, win_rate=0.6, avg_pnl=0.02)
        mock_get_trades.return_value = before_trades

        delta1 = ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test1", "immediate_healer", config)
        delta2 = ev.record_delta("base_trail_pct_swing", 0.07, 0.08, "test2", "immediate_healer", config)

        config.momentum_pct_swing = 0.03
        config.base_trail_pct_swing = 0.08

        # Both worsened
        now = time.time() * 1000
        after_trades = _make_trades(30, win_rate=0.2, avg_pnl=-0.03,
                                     base_time=max(delta1.timestamp, delta2.timestamp) + 1)
        mock_get_trades.return_value = after_trades

        evaluated = ev.evaluate_pending_deltas(config)
        assert len(evaluated) == 2

        reverted = [d for d in evaluated if d.evaluation_status == "reverted"]
        non_reverted_worsened = [d for d in evaluated if d.verdict == "worsened" and d.evaluation_status == "evaluated"]
        assert len(reverted) == 1  # max 1 revert
        assert len(non_reverted_worsened) == 1  # second one marked evaluated but not reverted

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_revert_respects_config_bounds(self, mock_log, mock_get_trades, mock_snap):
        """Revert should clamp to CONFIG_BOUNDS even if old_value was out of bounds."""
        ev = DeltaEvaluator()
        config = ScannerConfig()

        before_trades = _make_trades(20, win_rate=0.6, avg_pnl=0.02)
        mock_get_trades.return_value = before_trades

        lo, hi = CONFIG_BOUNDS["momentum_pct_swing"]
        # Simulate a delta where old_value was below bounds (shouldn't happen, but be safe)
        delta = ev.record_delta("momentum_pct_swing", lo - 0.1, 0.03, "test", "immediate_healer", config)
        config.momentum_pct_swing = 0.03

        # Worsened performance
        after_trades = _make_trades(30, win_rate=0.2, avg_pnl=-0.03, base_time=delta.timestamp + 1)
        mock_get_trades.return_value = after_trades

        evaluated = ev.evaluate_pending_deltas(config)
        assert len(evaluated) == 1
        assert evaluated[0].evaluation_status == "reverted"
        # Should be clamped to lower bound, not the out-of-bounds old value
        assert config.momentum_pct_swing == lo


# ── revert_parameter directly ────────────────────────────────────────────


class TestRevertParameter:
    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.log")
    def test_revert_sets_value(self, mock_log, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()
        config.momentum_pct_swing = 0.05

        delta = ParameterDelta(
            id="test-id", parameter="momentum_pct_swing",
            old_value=0.03, new_value=0.05,
            reason="test", source="immediate_healer",
            trades_before=TradeSnapshot(win_rate=0.5, avg_pnl_pct=0.01, count=10),
        )

        ev._revert_parameter(delta, config)
        assert config.momentum_pct_swing == 0.03
        mock_snap.assert_called_once()

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.log")
    def test_revert_unknown_parameter(self, mock_log, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()

        delta = ParameterDelta(
            id="test-id", parameter="nonexistent_param",
            old_value=0.5, new_value=0.6,
            reason="test", source="immediate_healer",
            trades_before=TradeSnapshot(win_rate=0.5, avg_pnl_pct=0.01, count=10),
        )

        ev._revert_parameter(delta, config)
        # Should log a warning and not crash
        mock_snap.assert_not_called()

    @patch("src.self_healing.delta_evaluator.snapshot_config")
    @patch("src.self_healing.delta_evaluator.log")
    def test_revert_clamps_to_upper_bound(self, mock_log, mock_snap):
        ev = DeltaEvaluator()
        config = ScannerConfig()

        _, hi = CONFIG_BOUNDS["momentum_pct_swing"]
        delta = ParameterDelta(
            id="test-id", parameter="momentum_pct_swing",
            old_value=hi + 1.0, new_value=0.05,
            reason="test", source="immediate_healer",
            trades_before=TradeSnapshot(win_rate=0.5, avg_pnl_pct=0.01, count=10),
        )

        ev._revert_parameter(delta, config)
        assert config.momentum_pct_swing == hi


# ── get_pending_deltas / get_all_deltas ──────────────────────────────────


class TestDeltaQueries:
    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_get_pending_filters_evaluated(self, mock_log, mock_get_trades):
        mock_get_trades.return_value = []
        ev = DeltaEvaluator()
        config = ScannerConfig()

        d1 = ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test1", "immediate_healer", config)
        d2 = ev.record_delta("base_trail_pct_swing", 0.07, 0.08, "test2", "claude_analysis", config)
        d1.evaluation_status = "evaluated"

        pending = ev.get_pending_deltas()
        assert len(pending) == 1
        assert pending[0].parameter == "base_trail_pct_swing"

    @patch("src.self_healing.delta_evaluator.get_closed_trades")
    @patch("src.self_healing.delta_evaluator.log")
    def test_get_all_returns_everything(self, mock_log, mock_get_trades):
        mock_get_trades.return_value = []
        ev = DeltaEvaluator()
        config = ScannerConfig()

        ev.record_delta("momentum_pct_swing", 0.02, 0.03, "test1", "immediate_healer", config)
        ev.record_delta("base_trail_pct_swing", 0.07, 0.08, "test2", "claude_analysis", config)

        assert len(ev.get_all_deltas()) == 2


# ── Module singleton ─────────────────────────────────────────────────────


class TestGetEvaluator:
    def test_returns_same_instance(self):
        import src.self_healing.delta_evaluator as mod
        mod._evaluator = None  # reset
        e1 = get_evaluator()
        e2 = get_evaluator()
        assert e1 is e2
        mod._evaluator = None  # cleanup
