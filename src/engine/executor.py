"""Binance execution engine — manages orders, positions, and risk.

Handles:
    - Market order placement via Binance Futures API
    - Position tracking with stop loss and take profit
    - Risk limits (max positions, max daily loss, position size cap)
    - Paper trading mode for testing
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.config import env
from src.engine.claude_brain import TradeDecision
from src.engine.log import log


@dataclass
class Position:
    """A live trading position."""
    id: str
    symbol: str
    side: str               # "long" or "short"
    entry_price: float
    size_usd: float
    quantity: float
    stop_pct: float
    target_pct: float
    opened_at: float        # unix ms
    signal_type: str
    reasoning: str
    current_price: float = 0
    high_watermark: float = 0
    low_watermark: float = float("inf")

    @property
    def stop_price(self) -> float:
        if self.side == "long":
            return self.entry_price * (1 - self.stop_pct)
        return self.entry_price * (1 + self.stop_pct)

    @property
    def target_price(self) -> float:
        if self.side == "long":
            return self.entry_price * (1 + self.target_pct)
        return self.entry_price * (1 - self.target_pct)

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0
        if self.side == "long":
            return (self.current_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.current_price) / self.entry_price

    @property
    def unrealized_pnl_usd(self) -> float:
        return self.unrealized_pnl_pct * self.size_usd

    @property
    def hold_hours(self) -> float:
        return (time.time() * 1000 - self.opened_at) / 3_600_000


@dataclass
class ClosedTrade:
    """A completed trade with P&L."""
    position: Position
    exit_price: float
    pnl_pct: float
    pnl_usd: float
    exit_reason: str        # "stop", "target", "timeout", "manual"
    closed_at: float


class Executor:
    """Manages trade execution and position lifecycle.

    Supports both paper trading and live Binance Futures execution.
    """

    # Risk limits
    MAX_POSITIONS = 10
    MAX_POSITION_SIZE = 500     # $500 max per trade
    MAX_DAILY_LOSS = 1000       # stop trading after -$1000/day
    COMMISSION_PCT = 0.00075    # Binance with BNB discount

    def __init__(self, paper: bool = True, initial_balance: float = 10_000):
        self.paper = paper
        self.balance = initial_balance
        self.positions: list[Position] = []
        self.closed_trades: list[ClosedTrade] = []
        self.daily_pnl: float = 0
        self._daily_reset_ts: float = time.time()
        self._binance: Optional[object] = None

        if not paper:
            from src.execution.providers import BinanceProvider
            self._binance = BinanceProvider()

    def _reset_daily(self):
        now = time.time()
        if now - self._daily_reset_ts > 86400:
            self.daily_pnl = 0
            self._daily_reset_ts = now

    def can_trade(self) -> bool:
        """Check if we're allowed to open new positions."""
        self._reset_daily()
        if len(self.positions) >= self.MAX_POSITIONS:
            return False
        if self.daily_pnl <= -self.MAX_DAILY_LOSS:
            return False
        if self.balance < 50:
            return False
        return True

    def has_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)

    def open_position(self, decision: TradeDecision) -> Optional[Position]:
        """Open a new position based on Claude's decision."""
        if not self.can_trade():
            return None
        if self.has_position(decision.symbol):
            return None

        size = min(decision.size_usd, self.MAX_POSITION_SIZE, self.balance * 0.4)
        if size < 10:
            return None

        commission = size * self.COMMISSION_PCT
        if size + commission > self.balance:
            return None

        entry_price = decision.entry_price
        if entry_price <= 0:
            return None

        quantity = size / entry_price

        # Execute on Binance (or paper)
        if not self.paper and self._binance:
            try:
                side_str = "BUY" if decision.side == "long" else "SELL"
                trade = self._binance._place_order(
                    decision.symbol, decision.signal_id, side_str, quantity, entry_price
                )
                if trade.status == "failed":
                    log("warn", f"Binance order failed for {decision.symbol}: {trade.error}")
                    return None
                entry_price = trade.price if trade.price > 0 else entry_price
                quantity = trade.quantity if trade.quantity > 0 else quantity
            except Exception as e:
                log("error", f"Execution error for {decision.symbol}: {e}")
                return None

        self.balance -= (size + commission)

        pos = Position(
            id=str(uuid.uuid4()),
            symbol=decision.symbol,
            side=decision.side,
            entry_price=entry_price,
            size_usd=size,
            quantity=quantity,
            stop_pct=decision.stop_pct,
            target_pct=decision.target_pct,
            opened_at=time.time() * 1000,
            signal_type=decision.reasoning[:50],
            reasoning=decision.reasoning,
            current_price=entry_price,
            high_watermark=entry_price,
            low_watermark=entry_price,
        )

        self.positions.append(pos)
        log("trade", f"OPEN {pos.side.upper()} {pos.symbol} ${size:.0f} @ ${entry_price:.4f} "
            f"stop={pos.stop_pct*100:.0f}% target={pos.target_pct*100:.0f}% [{decision.confidence}]")
        return pos

    def update_price(self, symbol: str, price: float):
        """Update current price for a symbol and check stops/targets."""
        for pos in self.positions:
            if pos.symbol != symbol:
                continue
            pos.current_price = price
            pos.high_watermark = max(pos.high_watermark, price)
            pos.low_watermark = min(pos.low_watermark, price)

            # Check stop loss
            if pos.side == "long" and price <= pos.stop_price:
                self._close_position(pos, price, "stop")
            elif pos.side == "short" and price >= pos.stop_price:
                self._close_position(pos, price, "stop")

            # Check take profit
            elif pos.side == "long" and price >= pos.target_price:
                self._close_position(pos, price, "target")
            elif pos.side == "short" and price <= pos.target_price:
                self._close_position(pos, price, "target")

            # Check max hold time (48h)
            elif pos.hold_hours > 48:
                self._close_position(pos, price, "timeout")

    def _close_position(self, pos: Position, exit_price: float, reason: str):
        """Close a position and record the trade."""
        commission = pos.size_usd * self.COMMISSION_PCT

        if pos.side == "long":
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

        pnl_usd = pos.size_usd * pnl_pct - commission

        # Execute close on Binance
        if not self.paper and self._binance:
            try:
                close_side = "SELL" if pos.side == "long" else "BUY"
                self._binance._place_order(
                    pos.symbol, pos.id, close_side, pos.quantity, exit_price
                )
            except Exception as e:
                log("error", f"Close execution error for {pos.symbol}: {e}")

        self.balance += pos.size_usd + pnl_usd
        self.daily_pnl += pnl_usd

        closed = ClosedTrade(
            position=pos, exit_price=exit_price,
            pnl_pct=pnl_pct, pnl_usd=pnl_usd,
            exit_reason=reason, closed_at=time.time() * 1000,
        )
        self.closed_trades.append(closed)
        self.positions = [p for p in self.positions if p.id != pos.id]

        emoji = "+" if pnl_usd >= 0 else ""
        log("trade", f"CLOSE {pos.symbol} {reason} {emoji}${pnl_usd:.2f} ({pnl_pct*100:+.2f}%) "
            f"held {pos.hold_hours:.1f}h | Balance: ${self.balance:,.2f}")

    def get_stats(self) -> dict:
        """Get current portfolio stats."""
        total_trades = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.pnl_usd > 0)
        total_pnl = sum(t.pnl_usd for t in self.closed_trades)
        unrealized = sum(p.unrealized_pnl_usd for p in self.positions)

        return {
            "balance": self.balance,
            "open_positions": len(self.positions),
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": wins / total_trades * 100 if total_trades > 0 else 0,
            "total_pnl": total_pnl,
            "unrealized_pnl": unrealized,
            "daily_pnl": self.daily_pnl,
            "daily_api_cost": 0,  # filled by runner
        }
