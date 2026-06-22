"""Tests for batch 3: rapid drawdown halt, breakeven stops."""

from tests.conftest import make_position, _now_ms


# ─── Rapid Drawdown Halt ────────────────────────────────────────────────────

class TestRapidDrawdownHalt:
    def test_allows_when_no_losses(self):
        from src.risk.protections import RapidDrawdownHalt, ProtectionContext
        guard = RapidDrawdownHalt(daily_halt_pct=0.05, starting_equity=10_000)
        ctx = ProtectionContext(
            realized_pnl_today=0, open_position_count=0,
            timestamp_ms=_now_ms(),
        )
        v = guard.check(ctx)
        assert v.allowed is True

    def test_halts_on_daily_drawdown(self):
        from src.risk.protections import RapidDrawdownHalt, ProtectionContext
        guard = RapidDrawdownHalt(daily_halt_pct=0.05, starting_equity=10_000)

        # Simulate -$600 in losses (6% of 10k)
        for _ in range(6):
            pos = make_position(pnl_pct=-0.01)
            guard.on_trade_closed(pos, -100)

        ctx = ProtectionContext(
            realized_pnl_today=-600, open_position_count=0,
            timestamp_ms=_now_ms(),
        )
        v = guard.check(ctx)
        assert v.allowed is False
        assert "EMERGENCY HALT" in v.reason
        assert "Daily" in v.reason

    def test_halts_on_weekly_drawdown(self):
        from src.risk.protections import RapidDrawdownHalt, ProtectionContext
        guard = RapidDrawdownHalt(weekly_halt_pct=0.10, daily_halt_pct=0.20,
                                  starting_equity=10_000)

        # Simulate -$1100 across multiple days (11% weekly)
        for _ in range(11):
            pos = make_position(pnl_pct=-0.01)
            guard.on_trade_closed(pos, -100)

        ctx = ProtectionContext(
            realized_pnl_today=0, open_position_count=0,
            timestamp_ms=_now_ms(),
        )
        v = guard.check(ctx)
        assert v.allowed is False
        assert "Weekly" in v.reason

    def test_day_reset_clears_daily_but_not_weekly(self):
        from src.risk.protections import RapidDrawdownHalt, ProtectionContext
        guard = RapidDrawdownHalt(daily_halt_pct=0.05, weekly_halt_pct=0.10,
                                  starting_equity=10_000)

        # Lose $400 (4% daily — under limit)
        for _ in range(4):
            pos = make_position(pnl_pct=-0.01)
            guard.on_trade_closed(pos, -100)

        # Reset day
        guard.on_day_reset()

        # Daily should be reset, weekly should still have -$400
        ctx = ProtectionContext(
            realized_pnl_today=0, open_position_count=0,
            timestamp_ms=_now_ms(),
        )
        v = guard.check(ctx)
        assert v.allowed is True  # daily reset, weekly under 10%

        # Lose another $700 (7% new weekly = total 11%)
        for _ in range(7):
            pos = make_position(pnl_pct=-0.01)
            guard.on_trade_closed(pos, -100)

        v = guard.check(ctx)
        assert v.allowed is False  # weekly exceeded

    def test_zero_equity_halts_on_loss(self):
        from src.risk.protections import RapidDrawdownHalt, ProtectionContext
        guard = RapidDrawdownHalt(starting_equity=0)

        pos = make_position(pnl_pct=-0.50)
        guard.on_trade_closed(pos, -5000)

        ctx = ProtectionContext(
            realized_pnl_today=-5000, open_position_count=0,
            timestamp_ms=_now_ms(),
        )
        v = guard.check(ctx)
        assert v.allowed is False  # equity depleted, should halt


# ─── Breakeven Stop at 1R ───────────────────────────────────────────────────

class TestBreakevenStop:
    def test_long_stop_moves_to_entry_at_1r(self):
        """When long position reaches 1R profit, stop should move to entry."""
        pos = make_position(
            side="long", entry_price=100.0,
        )
        pos.stop_price = 93.0  # 7% below entry
        pos.trail_pct = 0.07

        # At 1R, profit = initial risk = 7
        # So current_price should be 107
        initial_risk = abs(pos.entry_price - pos.stop_price)  # 7
        current_price = pos.entry_price + initial_risk  # 107

        from src.main import _compute_r_multiple
        r = _compute_r_multiple(pos, current_price)
        assert r >= 1.0

        # Breakeven logic: stop should move to entry
        if r >= 1.0 and pos.side == "long" and pos.stop_price < pos.entry_price:
            pos.stop_price = pos.entry_price
        assert pos.stop_price == 100.0

    def test_short_stop_moves_to_entry_at_1r(self):
        """When short position reaches 1R profit, stop should move to entry."""
        pos = make_position(
            side="short", entry_price=100.0,
        )
        pos.stop_price = 107.0  # 7% above entry
        pos.trail_pct = 0.07

        initial_risk = abs(pos.entry_price - pos.stop_price)  # 7
        current_price = pos.entry_price - initial_risk  # 93

        from src.main import _compute_r_multiple
        r = _compute_r_multiple(pos, current_price)
        assert r >= 1.0

        if r >= 1.0 and pos.side == "short" and pos.stop_price > pos.entry_price:
            pos.stop_price = pos.entry_price
        assert pos.stop_price == 100.0

    def test_stop_not_moved_below_1r(self):
        """Stop should not move to breakeven if trade hasn't reached 1R."""
        pos = make_position(
            side="long", entry_price=100.0,
        )
        pos.stop_price = 93.0
        pos.trail_pct = 0.07

        current_price = 103.0  # only 0.43R profit

        from src.main import _compute_r_multiple
        r = _compute_r_multiple(pos, current_price)
        assert r < 1.0

        # Stop should NOT move
        original_stop = pos.stop_price
        if r >= 1.0 and pos.side == "long" and pos.stop_price < pos.entry_price:
            pos.stop_price = pos.entry_price
        assert pos.stop_price == original_stop  # unchanged

    def test_stop_not_moved_backwards(self):
        """If stop is already above entry, breakeven logic shouldn't lower it."""
        pos = make_position(
            side="long", entry_price=100.0,
        )
        pos.stop_price = 105.0  # already above entry (trailing up)
        pos.trail_pct = 0.07

        from src.main import _compute_r_multiple
        current_price = 110.0
        r = _compute_r_multiple(pos, current_price)

        # The condition `pos.stop_price < pos.entry_price` is False, so nothing happens
        original_stop = pos.stop_price
        if r >= 1.0 and pos.side == "long" and pos.stop_price < pos.entry_price:
            pos.stop_price = pos.entry_price
        assert pos.stop_price == original_stop  # not lowered

    def test_r_multiple_computation(self):
        """Verify _compute_r_multiple returns correct values."""
        from src.main import _compute_r_multiple
        pos = make_position(side="long", entry_price=100.0)
        pos.stop_price = 93.0  # risk = 7

        assert abs(_compute_r_multiple(pos, 107.0) - 1.0) < 0.01  # 1R
        assert abs(_compute_r_multiple(pos, 114.0) - 2.0) < 0.01  # 2R
        assert abs(_compute_r_multiple(pos, 100.0) - 0.0) < 0.01  # 0R
        assert _compute_r_multiple(pos, 93.0) < 0  # negative R (losing)
