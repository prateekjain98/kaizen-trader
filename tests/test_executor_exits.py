"""Unit tests for Executor exit triggers + race-safe close.

Covers each `_close_position` reason exercised via Executor.update_price:
- "stop"     — hard stop loss hit (long + short)
- "target"   — take-profit hit (long + short)
- "timeout"  — hold > 48h
- trailing stop activation + override of stop_price

Plus the _closing race guard: concurrent calls can't double-close.
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.engine.executor import Executor, Position
from src.engine.claude_brain import TradeDecision


def _decision(symbol: str = "TEST", side: str = "long",
              entry: float = 100.0, size_usd: float = 50.0,
              stop_pct: float = 0.10, target_pct: float = 0.25) -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        side=side,
        size_usd=size_usd,
        entry_price=entry,
        stop_pct=stop_pct,
        target_pct=target_pct,
        confidence="high",
        reasoning="test",
        signal_id=f"sig-{uuid.uuid4().hex[:8]}",
        timestamp=time.time() * 1000,
    )


@pytest.fixture
def executor(tmp_path, monkeypatch):
    """Fresh paper-mode executor with isolated state.

    Critical: the production Executor.__init__ auto-loads from a fixed
    portfolio.json path. Tests must point that at a temp dir or they
    inherit positions from whatever is currently running on this machine.
    """
    fake_portfolio = tmp_path / "portfolio.json"
    monkeypatch.setattr("src.engine.executor._PORTFOLIO_FILE", fake_portfolio)
    e = Executor(paper=True, initial_balance=1000.0)
    e._save_state = MagicMock()
    # Belt and braces: even if the patch raced, force-clear state.
    e.positions.clear()
    e.closed_trades.clear()
    e.balance = 1000.0
    e.daily_pnl = 0.0
    e._closing.clear()
    return e


# ─── stop-loss triggers ─────────────────────────────────────────────────────

def test_long_stop_loss_fires_at_stop_price(executor):
    pos = executor.open_position(_decision(symbol="LONGSTOP", side="long",
                                            entry=100.0, stop_pct=0.10))
    assert pos is not None
    assert len(executor.positions) == 1
    # Drop price exactly to stop (entry * 0.90)
    executor.update_price("LONGSTOP", 90.0)
    assert len(executor.positions) == 0
    assert len(executor.closed_trades) == 1
    assert executor.closed_trades[-1].exit_reason == "stop"


def test_short_stop_loss_fires_at_stop_price(executor):
    pos = executor.open_position(_decision(symbol="SHORTSTOP", side="short",
                                            entry=100.0, stop_pct=0.10))
    assert pos is not None
    # Short stop is entry * 1.10 = 110
    executor.update_price("SHORTSTOP", 110.0)
    assert len(executor.positions) == 0
    assert executor.closed_trades[-1].exit_reason == "stop"


def test_stop_does_not_fire_above_threshold(executor):
    pos = executor.open_position(_decision(symbol="HOLD", side="long",
                                            entry=100.0, stop_pct=0.10))
    executor.update_price("HOLD", 95.0)  # -5%, stop is at -10%
    assert len(executor.positions) == 1


# ─── take-profit triggers ───────────────────────────────────────────────────

def test_long_target_fires_at_target_price(executor):
    pos = executor.open_position(_decision(symbol="LONGTGT", side="long",
                                            entry=100.0, stop_pct=0.10, target_pct=0.25))
    executor.update_price("LONGTGT", 125.0)
    assert len(executor.positions) == 0
    assert executor.closed_trades[-1].exit_reason == "target"


def test_short_target_fires_at_target_price(executor):
    pos = executor.open_position(_decision(symbol="SHORTTGT", side="short",
                                            entry=100.0, stop_pct=0.10, target_pct=0.25))
    executor.update_price("SHORTTGT", 75.0)
    assert len(executor.positions) == 0
    assert executor.closed_trades[-1].exit_reason == "target"


# ─── timeout (>48h) ─────────────────────────────────────────────────────────

def test_timeout_fires_after_48h(executor):
    pos = executor.open_position(_decision(symbol="TIMEOUT", side="long", entry=100.0))
    # Force opened_at to be 49 hours ago
    pos.opened_at = (time.time() - 49 * 3600) * 1000
    executor.update_price("TIMEOUT", 100.5)  # tiny price move, no stop/target hit
    assert len(executor.positions) == 0
    assert executor.closed_trades[-1].exit_reason == "timeout"


# ─── trailing stop ──────────────────────────────────────────────────────────

def test_trailing_stop_activates_and_overrides_stop_price(executor):
    """After +1.5x stop_pct profit, trailing stop kicks in.
    With stop_pct=0.10, activation is at +15%. Trail is price*(1-stop_pct).
    """
    pos = executor.open_position(_decision(symbol="TRAIL", side="long",
                                            entry=100.0, stop_pct=0.10, target_pct=0.50))
    # Push price to +20% — trailing should activate at +15%, set trail at 120*0.90=108
    executor.update_price("TRAIL", 120.0)
    assert pos.trailing_stop_price > 0
    assert pos.trailing_stop_price == pytest.approx(108.0, rel=1e-6)
    # stop_price property should return the trailing stop now (not the original 90)
    assert pos.stop_price == pytest.approx(108.0, rel=1e-6)
    # Drop to 107 — should hit trailing stop
    executor.update_price("TRAIL", 107.0)
    assert len(executor.positions) == 0
    # exit reason should be "trail" when trailing_stop_price moved off entry
    # (logging-attribution fix — prior code conflated trail with hard stop)
    assert executor.closed_trades[-1].exit_reason == "trail"


def test_trailing_stop_does_not_move_down(executor):
    pos = executor.open_position(_decision(symbol="TRAIL2", side="long",
                                            entry=100.0, stop_pct=0.10))
    executor.update_price("TRAIL2", 120.0)  # trail at 108
    first_trail = pos.trailing_stop_price
    executor.update_price("TRAIL2", 115.0)  # would set trail at 103.5 (lower) — ignore
    assert pos.trailing_stop_price == first_trail


# ─── race-safe close ────────────────────────────────────────────────────────

def test_close_position_is_race_safe(executor):
    """Two concurrent threads calling _close_position on the same pos should
    only execute the close once — _closing guard set deduplicates."""
    pos = executor.open_position(_decision(symbol="RACE", side="long", entry=100.0))
    balance_after_open = executor.balance  # already net of margin + entry commission
    barrier = threading.Barrier(2)

    def race_close():
        barrier.wait()
        executor._close_position(pos, 110.0, "stop")

    t1 = threading.Thread(target=race_close)
    t2 = threading.Thread(target=race_close)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert len(executor.positions) == 0
    assert len(executor.closed_trades) == 1
    # On close, balance += size_usd + pnl_usd (margin returned + pnl). If the
    # race had double-credited, balance would be ~2x above expected.
    exit_commission = pos.size_usd * executor.COMMISSION_PCT
    pnl_usd = pos.size_usd * 0.10 - pos.entry_commission - exit_commission
    expected_balance = balance_after_open + pos.size_usd + pnl_usd
    assert executor.balance == pytest.approx(expected_balance, rel=1e-3)


def test_simultaneous_stop_and_target_only_closes_once(executor):
    """If a tick somehow has price both ≤ stop AND ≥ target (impossible normally
    but possible with bad input), only one close fires."""
    pos = executor.open_position(_decision(symbol="EDGE", side="long",
                                            entry=100.0, stop_pct=0.50, target_pct=0.10))
    # stop at 50, target at 110 — feed price 110: target fires
    executor.update_price("EDGE", 110.0)
    assert len(executor.closed_trades) == 1
    assert executor.closed_trades[-1].exit_reason == "target"


def test_closed_trades_is_bounded_deque(executor):
    """closed_trades was previously a list growing without bound; now deque(500)."""
    from collections import deque
    assert isinstance(executor.closed_trades, deque)
    assert executor.closed_trades.maxlen == 500


# ─── Progressive trailing-stop tier tests ──────────────────────────────────


def test_trail_factor_returns_none_below_activation():
    """Below 1.5x stop_pct profit, no trailing yet."""
    from src.engine.executor import Executor
    assert Executor._trail_factor(0.0, 0.05) is None
    assert Executor._trail_factor(0.07, 0.05) is None  # 1.4x stop, just under


def test_trail_factor_tier_initial():
    """Initial activation tier: 1.5x stop ≤ profit < 3x stop → factor 1.0"""
    from src.engine.executor import Executor
    assert Executor._trail_factor(0.076, 0.05) == 1.0   # just over 1.5x (FP-safe)
    assert Executor._trail_factor(0.10, 0.05) == 1.0    # 2x stop
    assert Executor._trail_factor(0.149, 0.05) == 1.0   # just under 3x


def test_trail_factor_tier_mid():
    """Mid tier: 3x stop ≤ profit < 5x stop → factor 0.5"""
    from src.engine.executor import Executor
    assert Executor._trail_factor(0.151, 0.05) == 0.5   # just over 3x (FP-safe)
    assert Executor._trail_factor(0.20, 0.05) == 0.5    # 4x stop
    assert Executor._trail_factor(0.249, 0.05) == 0.5   # just under 5x


def test_trail_factor_tier_tight():
    """Tight tier: profit ≥ 5x stop → factor 0.25"""
    from src.engine.executor import Executor
    assert Executor._trail_factor(0.251, 0.05) == 0.25  # just over 5x (FP-safe)
    assert Executor._trail_factor(0.50, 0.05) == 0.25   # 10x stop


def test_progressive_trail_locks_more_at_higher_profit(executor):
    """A long that runs deep into profit should have trail close to current
    price (per the 0.25 tight tier), not the old constant-1.0 trail."""
    pos = executor.open_position(_decision(symbol="MEGA", side="long",
                                            entry=100.0, stop_pct=0.05, target_pct=0.50))
    # Step the price up: +5%, +20%, +30%
    executor.update_price("MEGA", 105.0)   # 1x stop profit, no trail
    assert pos.trailing_stop_price == 0
    executor.update_price("MEGA", 108.0)   # 1.6x stop profit → factor 1.0
    # trail = 108 * (1 - 0.05*1.0) = 102.6
    assert pos.trailing_stop_price == pytest.approx(102.6, rel=1e-3)
    executor.update_price("MEGA", 120.0)   # 4x stop profit → factor 0.5
    # trail = 120 * (1 - 0.05*0.5) = 117.0
    assert pos.trailing_stop_price == pytest.approx(117.0, rel=1e-3)
    executor.update_price("MEGA", 130.0)   # 6x stop profit → factor 0.25
    # trail = 130 * (1 - 0.05*0.25) = 128.375
    assert pos.trailing_stop_price == pytest.approx(128.375, rel=1e-3)


def test_progressive_trail_never_decreases_when_profit_retraces(executor):
    """If price retraces from a tight tier back to a looser tier, the trail
    must not move down — the new_trail > existing guard prevents it."""
    pos = executor.open_position(_decision(symbol="ZIG", side="long",
                                            entry=100.0, stop_pct=0.05, target_pct=0.50))
    executor.update_price("ZIG", 130.0)      # tight tier → trail 128.375
    assert pos.trailing_stop_price == pytest.approx(128.375, rel=1e-3)
    executor.update_price("ZIG", 110.0)      # retrace into initial tier
    # New computed trail at 110 with factor 1.0 = 110*0.95 = 104.5 — LOWER
    # than existing 128.375. Must be rejected (trail stays at 128.375).
    assert pos.trailing_stop_price == pytest.approx(128.375, rel=1e-3)


def test_progressive_trail_short_side(executor):
    """Mirror test for shorts: trailing tightens as profit grows."""
    pos = executor.open_position(_decision(symbol="DOWN", side="short",
                                            entry=100.0, stop_pct=0.05, target_pct=0.50))
    executor.update_price("DOWN", 92.0)   # 8% profit (1.6x stop) → factor 1.0
    # trail = 92 * (1 + 0.05*1.0) = 96.6
    assert pos.trailing_stop_price == pytest.approx(96.6, rel=1e-3)
    executor.update_price("DOWN", 80.0)   # 20% profit (4x stop) → factor 0.5
    # trail = 80 * (1 + 0.05*0.5) = 82.0
    assert pos.trailing_stop_price == pytest.approx(82.0, rel=1e-3)
    executor.update_price("DOWN", 70.0)   # 30% profit (6x stop) → factor 0.25
    # trail = 70 * (1 + 0.05*0.25) = 70.875
    assert pos.trailing_stop_price == pytest.approx(70.875, rel=1e-3)


# ─── Cross-symbol drawdown circuit breaker tests ──────────────────────────


def test_equity_drawdown_returns_zero_at_peak(executor):
    """No positions, balance untouched → drawdown 0."""
    assert executor._equity_drawdown_pct() == 0.0


def test_drawdown_blocks_can_trade_at_threshold(executor):
    """When equity drops 15%+ from peak, can_trade refuses."""
    # Establish a peak
    executor._equity_drawdown_pct()
    initial = executor.balance
    # Simulate a 20% loss by directly reducing balance (no positions to model).
    executor.balance = initial * 0.80
    assert executor.can_trade() is False
    # Restore — drawdown clears as new peak forms on the next equity rise.
    executor.balance = initial
    executor._peak_equity = initial  # explicit so test is deterministic
    assert executor._equity_drawdown_pct() == 0.0
