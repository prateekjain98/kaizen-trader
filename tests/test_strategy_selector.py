"""Tests for Darwinian strategy selection."""

import time
import pytest

from src.evaluation.strategy_selector import (
    StrategySelector, SelectionConfig, StrategyHealth,
)
from tests.conftest import make_position


def _closed_positions(strategy, pnl_pcts):
    """Create a list of closed positions for a single strategy."""
    now = time.time() * 1000
    positions = []
    for i, pnl in enumerate(pnl_pcts):
        p = make_position(
            strategy=strategy, pnl_pct=pnl, pnl_usd=pnl * 1000,
            status="closed", exit_reason="trailing_stop",
            opened_at=now - (len(pnl_pcts) - i) * 3_600_000,
            closed_at=now - (len(pnl_pcts) - i - 1) * 3_600_000,
        )
        p.id = f"{strategy}-{i}"
        positions.append(p)
    return positions


# ── Basic enable/disable ──────────────────────────────────────────────────

class TestIsStrategyEnabled:
    def test_unknown_strategy_enabled_by_default(self):
        sel = StrategySelector()
        assert sel.is_strategy_enabled("totally_new_strategy") is True

    def test_after_creation_enabled(self):
        sel = StrategySelector()
        sel._get_or_create("momentum_swing")
        assert sel.is_strategy_enabled("momentum_swing") is True


# ── Consecutive loss disable ──────────────────────────────────────────────

class TestConsecutiveLossDisable:
    def test_disables_after_max_consecutive(self):
        sel = StrategySelector(SelectionConfig(max_consecutive_losses=5))
        for i in range(5):
            p = make_position(strategy="bad_strat", pnl_pct=-0.02)
            p.id = f"loss-{i}"
            sel.on_trade_closed(p)
        assert sel.is_strategy_enabled("bad_strat") is False

    def test_win_resets_counter(self):
        sel = StrategySelector(SelectionConfig(max_consecutive_losses=5))
        for i in range(4):
            p = make_position(strategy="strat", pnl_pct=-0.02)
            p.id = f"loss-{i}"
            sel.on_trade_closed(p)
        # A win resets
        p = make_position(strategy="strat", pnl_pct=0.03)
        p.id = "win"
        sel.on_trade_closed(p)
        assert sel.is_strategy_enabled("strat") is True
        # Need 5 more consecutive to disable
        for i in range(4):
            p = make_position(strategy="strat", pnl_pct=-0.02)
            p.id = f"loss2-{i}"
            sel.on_trade_closed(p)
        assert sel.is_strategy_enabled("strat") is True

    def test_tracks_per_strategy(self):
        sel = StrategySelector(SelectionConfig(max_consecutive_losses=3))
        for i in range(3):
            p = make_position(strategy="strat_a", pnl_pct=-0.02)
            p.id = f"a-{i}"
            sel.on_trade_closed(p)
        for i in range(2):
            p = make_position(strategy="strat_b", pnl_pct=-0.02)
            p.id = f"b-{i}"
            sel.on_trade_closed(p)
        assert sel.is_strategy_enabled("strat_a") is False
        assert sel.is_strategy_enabled("strat_b") is True


# ── Rolling evaluation ────────────────────────────────────────────────────

class TestEvaluateStrategies:
    def test_insufficient_trades_no_action(self):
        sel = StrategySelector(SelectionConfig(min_trades_before_eval=10))
        positions = _closed_positions("momentum_swing", [0.01] * 5)
        sel.evaluate_strategies(positions)
        assert sel.is_strategy_enabled("momentum_swing") is True

    def test_low_win_rate_disables(self):
        sel = StrategySelector(SelectionConfig(
            min_trades_before_eval=10, min_win_rate=0.25,
            rolling_window_trades=30,
        ))
        # 20% win rate: 2 wins, 8 losses
        pnls = [0.02, 0.03] + [-0.02] * 8
        positions = _closed_positions("losing_strat", pnls)
        sel.evaluate_strategies(positions)
        assert sel.is_strategy_enabled("losing_strat") is False

    def test_healthy_strategy_stays_enabled(self):
        sel = StrategySelector(SelectionConfig(min_trades_before_eval=10))
        # 70% win rate
        pnls = [0.03] * 7 + [-0.02] * 3
        positions = _closed_positions("healthy_strat", pnls)
        sel.evaluate_strategies(positions)
        assert sel.is_strategy_enabled("healthy_strat") is True

    def test_negative_sharpe_disables(self):
        sel = StrategySelector(SelectionConfig(
            min_trades_before_eval=10, min_sharpe_threshold=-0.5,
        ))
        # Consistently negative with variance
        pnls = [-0.05, -0.03, 0.01, -0.04, -0.06, -0.02, -0.05, -0.03, 0.01, -0.07]
        positions = _closed_positions("bad_sharpe", pnls)
        sel.evaluate_strategies(positions)
        h = sel._health.get("bad_sharpe")
        assert h is not None
        if h.rolling_sharpe is not None:
            assert h.rolling_sharpe < -0.5
            assert h.enabled is False

    def test_multiple_strategies_evaluated_independently(self):
        sel = StrategySelector(SelectionConfig(min_trades_before_eval=10, min_win_rate=0.25))
        good = _closed_positions("good_strat", [0.03] * 7 + [-0.02] * 3)
        bad = _closed_positions("bad_strat", [0.01] + [-0.02] * 9)
        sel.evaluate_strategies(good + bad)
        assert sel.is_strategy_enabled("good_strat") is True
        assert sel.is_strategy_enabled("bad_strat") is False


# ── Force enable ──────────────────────────────────────────────────────────

class TestForceEnable:
    def test_force_enable_overrides(self):
        sel = StrategySelector(SelectionConfig(max_consecutive_losses=3))
        for i in range(3):
            p = make_position(strategy="strat", pnl_pct=-0.02)
            p.id = f"loss-{i}"
            sel.on_trade_closed(p)
        assert sel.is_strategy_enabled("strat") is False
        sel.force_enable("strat")
        assert sel.is_strategy_enabled("strat") is True

    def test_force_enable_resets_metadata(self):
        sel = StrategySelector(SelectionConfig(max_consecutive_losses=3))
        for i in range(3):
            p = make_position(strategy="strat", pnl_pct=-0.02)
            p.id = f"loss-{i}"
            sel.on_trade_closed(p)
        sel.force_enable("strat")
        h = sel._health["strat"]
        assert h.disabled_at is None
        assert h.disable_reason is None
        assert h.trades_since_enable == 0


# ── Health report ─────────────────────────────────────────────────────────

class TestHealthReport:
    def test_report_includes_all_tracked(self):
        sel = StrategySelector()
        sel._get_or_create("strat_a")
        sel._get_or_create("strat_b")
        report = sel.get_health_report()
        ids = {h.strategy_id for h in report}
        assert ids == {"strat_a", "strat_b"}

    def test_report_empty_initially(self):
        sel = StrategySelector()
        assert sel.get_health_report() == []
