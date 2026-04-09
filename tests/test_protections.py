"""Tests for the declarative protection / circuit-breaker system."""

import time
import pytest

from src.risk.protections import (
    ProtectionVerdict, ProtectionContext, ProtectionChain,
    MaxDailyLossGuard, MaxOpenPositionsGuard, StoplossGuard,
    MaxDrawdownGuard, CooldownPeriod, DEFAULT_PROTECTIONS,
)
from tests.conftest import make_position


def _ctx(pnl=0, positions=0) -> ProtectionContext:
    return ProtectionContext(
        realized_pnl_today=pnl,
        open_position_count=positions,
        timestamp_ms=time.time() * 1000,
    )


# ── MaxDailyLossGuard ─────────────────────────────────────────────────────

class TestMaxDailyLossGuard:
    def test_allows_within_limit(self):
        rule = MaxDailyLossGuard(max_daily_loss_usd=300)
        v = rule.check(_ctx(pnl=-200))
        assert v.allowed is True

    def test_blocks_over_limit(self):
        rule = MaxDailyLossGuard(max_daily_loss_usd=300)
        v = rule.check(_ctx(pnl=-350))
        assert v.allowed is False
        assert "350" in v.reason

    def test_allows_positive_pnl(self):
        rule = MaxDailyLossGuard(max_daily_loss_usd=300)
        v = rule.check(_ctx(pnl=500))
        assert v.allowed is True


# ── MaxOpenPositionsGuard ─────────────────────────────────────────────────

class TestMaxOpenPositionsGuard:
    def test_allows_under_cap(self):
        rule = MaxOpenPositionsGuard(max_open_positions=5)
        v = rule.check(_ctx(positions=3))
        assert v.allowed is True

    def test_blocks_at_cap(self):
        rule = MaxOpenPositionsGuard(max_open_positions=5)
        v = rule.check(_ctx(positions=5))
        assert v.allowed is False

    def test_blocks_over_cap(self):
        rule = MaxOpenPositionsGuard(max_open_positions=5)
        v = rule.check(_ctx(positions=7))
        assert v.allowed is False


# ── StoplossGuard ─────────────────────────────────────────────────────────

class TestStoplossGuard:
    def test_allows_no_stops(self):
        rule = StoplossGuard(max_consecutive_stops=3)
        v = rule.check(_ctx())
        assert v.allowed is True

    def test_blocks_after_consecutive_stops(self):
        rule = StoplossGuard(max_consecutive_stops=3)
        for _ in range(3):
            p = make_position(exit_reason="trailing_stop", pnl_usd=-10)
            rule.on_trade_closed(p, -10)
        v = rule.check(_ctx())
        assert v.allowed is False
        assert "3 consecutive" in v.reason

    def test_win_resets_counter(self):
        rule = StoplossGuard(max_consecutive_stops=3)
        for _ in range(2):
            p = make_position(exit_reason="trailing_stop", pnl_usd=-10)
            rule.on_trade_closed(p, -10)
        # A win resets the counter
        p = make_position(exit_reason="take_profit", pnl_usd=20)
        rule.on_trade_closed(p, 20)
        v = rule.check(_ctx())
        assert v.allowed is True

    def test_day_reset_clears(self):
        rule = StoplossGuard(max_consecutive_stops=3)
        for _ in range(3):
            p = make_position(exit_reason="trailing_stop", pnl_usd=-10)
            rule.on_trade_closed(p, -10)
        rule.on_day_reset()
        v = rule.check(_ctx())
        assert v.allowed is True


# ── MaxDrawdownGuard ──────────────────────────────────────────────────────

class TestMaxDrawdownGuard:
    def test_allows_no_drawdown(self):
        rule = MaxDrawdownGuard(max_drawdown_pct=0.15)
        v = rule.check(_ctx())
        assert v.allowed is True

    def test_blocks_on_drawdown(self):
        # starting_equity=1000 so drawdown math is clearer
        rule = MaxDrawdownGuard(max_drawdown_pct=0.15, starting_equity=1000)
        # Build equity to 2000, then draw down
        p = make_position(pnl_usd=1000)
        rule.on_trade_closed(p, 1000)
        p2 = make_position(pnl_usd=-500)
        rule.on_trade_closed(p2, -500)
        # peak=2000, current=1500, dd = 500/2000 = 0.25 > 0.15
        v = rule.check(_ctx())
        assert v.allowed is False
        assert "25.0%" in v.reason

    def test_blocks_on_early_losses(self):
        """Early losses from starting equity should trigger drawdown."""
        rule = MaxDrawdownGuard(max_drawdown_pct=0.15, starting_equity=1000)
        # Lose 200 right away — no profit first
        p = make_position(pnl_usd=-200)
        rule.on_trade_closed(p, -200)
        # peak=1000, current=800, dd = 200/1000 = 0.20 > 0.15
        v = rule.check(_ctx())
        assert v.allowed is False

    def test_allows_small_drawdown(self):
        rule = MaxDrawdownGuard(max_drawdown_pct=0.15, starting_equity=1000)
        p = make_position(pnl_usd=1000)
        rule.on_trade_closed(p, 1000)
        p2 = make_position(pnl_usd=-100)
        rule.on_trade_closed(p2, -100)
        # peak=2000, current=1900, dd = 100/2000 = 0.05 < 0.15
        v = rule.check(_ctx())
        assert v.allowed is True

    def test_day_reset_preserves_current_equity(self):
        rule = MaxDrawdownGuard(max_drawdown_pct=0.15, starting_equity=1000)
        p = make_position(pnl_usd=1000)
        rule.on_trade_closed(p, 1000)
        p2 = make_position(pnl_usd=-500)
        rule.on_trade_closed(p2, -500)
        rule.on_day_reset()  # peak resets to current equity
        v = rule.check(_ctx())
        assert v.allowed is True


# ── CooldownPeriod ────────────────────────────────────────────────────────

class TestCooldownPeriod:
    def test_allows_no_losses(self):
        rule = CooldownPeriod(cooldown_ms=1000, trigger_after_n_losses=3)
        v = rule.check(_ctx())
        assert v.allowed is True

    def test_triggers_cooldown_after_n_losses(self):
        rule = CooldownPeriod(cooldown_ms=60_000, trigger_after_n_losses=3)
        for _ in range(3):
            p = make_position(pnl_usd=-10)
            rule.on_trade_closed(p, -10)
        v = rule.check(_ctx())
        assert v.allowed is False
        assert "Cooldown active" in v.reason

    def test_cooldown_expires(self):
        rule = CooldownPeriod(cooldown_ms=100, trigger_after_n_losses=2)
        for _ in range(2):
            rule.on_trade_closed(make_position(pnl_usd=-10), -10)
        # Wait for cooldown to expire
        time.sleep(0.15)
        v = rule.check(_ctx())
        assert v.allowed is True

    def test_win_resets_counter(self):
        rule = CooldownPeriod(cooldown_ms=60_000, trigger_after_n_losses=3)
        for _ in range(2):
            rule.on_trade_closed(make_position(pnl_usd=-10), -10)
        rule.on_trade_closed(make_position(pnl_usd=20), 20)
        # Counter reset, need 3 more consecutive losses
        rule.on_trade_closed(make_position(pnl_usd=-10), -10)
        v = rule.check(_ctx())
        assert v.allowed is True


# ── ProtectionChain ───────────────────────────────────────────────────────

class TestProtectionChain:
    def test_all_pass(self):
        chain = ProtectionChain([
            MaxDailyLossGuard(max_daily_loss_usd=300),
            MaxOpenPositionsGuard(max_open_positions=5),
        ])
        v = chain.can_open(_ctx(pnl=-100, positions=2))
        assert v.allowed is True

    def test_first_block_short_circuits(self):
        chain = ProtectionChain([
            MaxDailyLossGuard(max_daily_loss_usd=300),
            MaxOpenPositionsGuard(max_open_positions=5),
        ])
        v = chain.can_open(_ctx(pnl=-400, positions=2))
        assert v.allowed is False
        assert v.rule_name == "max_daily_loss"

    def test_second_rule_blocks(self):
        chain = ProtectionChain([
            MaxDailyLossGuard(max_daily_loss_usd=300),
            MaxOpenPositionsGuard(max_open_positions=5),
        ])
        v = chain.can_open(_ctx(pnl=-100, positions=5))
        assert v.allowed is False
        assert v.rule_name == "max_open_positions"

    def test_notify_close_propagates(self):
        sl_guard = StoplossGuard(max_consecutive_stops=2)
        chain = ProtectionChain([sl_guard])
        for _ in range(2):
            p = make_position(exit_reason="trailing_stop", pnl_usd=-10)
            chain.notify_close(p, -10)
        v = chain.can_open(_ctx())
        assert v.allowed is False

    def test_from_config(self):
        chain = ProtectionChain.from_config(DEFAULT_PROTECTIONS)
        assert len(chain.rules) == 6

    def test_disabled_rule_skipped(self):
        config = [
            {"rule_type": "max_daily_loss", "enabled": False, "params": {"max_daily_loss_usd": 1}},
            {"rule_type": "max_open_positions", "params": {"max_open_positions": 5}},
        ]
        chain = ProtectionChain.from_config(config)
        assert len(chain.rules) == 1
        # max_daily_loss disabled, so even extreme loss is allowed
        v = chain.can_open(_ctx(pnl=-9999, positions=0))
        assert v.allowed is True

    def test_unknown_rule_type_raises(self):
        with pytest.raises(ValueError, match="Unknown protection rule"):
            ProtectionChain.from_config([{"rule_type": "nonexistent"}])

    def test_reset_day_propagates(self):
        sl_guard = StoplossGuard(max_consecutive_stops=2)
        chain = ProtectionChain([sl_guard])
        for _ in range(2):
            chain.notify_close(make_position(exit_reason="trailing_stop", pnl_usd=-10), -10)
        assert chain.can_open(_ctx()).allowed is False
        chain.reset_day()
        assert chain.can_open(_ctx()).allowed is True
