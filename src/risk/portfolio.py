"""Portfolio-level risk manager — now powered by declarative ProtectionChain."""

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.config import env
from src.risk.protections import ProtectionChain, ProtectionContext, DEFAULT_PROTECTIONS
from src.storage.database import log
from src.types import Position


@dataclass
class DailyStats:
    date: str
    realized_pnl: float = 0
    trade_count: int = 0


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_lock = threading.RLock()  # RLock: _get_chain() may re-enter from can_open/register_close
_daily_stats = DailyStats(date=_today_utc())
_open_positions: dict[str, Position] = {}
_daily_returns: list[float] = []
_protection_chain: Optional[ProtectionChain] = None


def init_protections(config_list: Optional[list[dict]] = None) -> None:
    """Initialize the protection chain. Call once at startup."""
    global _protection_chain
    with _lock:
        _protection_chain = ProtectionChain.from_config(config_list or DEFAULT_PROTECTIONS)
        rule_names = [r.name for r in _protection_chain.rules]
    log("info", f"Protection chain initialized: {', '.join(rule_names)}")


def _get_chain() -> ProtectionChain:
    """Lazy-init the chain if not explicitly initialized."""
    global _protection_chain
    with _lock:
        if _protection_chain is None:
            _protection_chain = ProtectionChain.from_config(DEFAULT_PROTECTIONS)
        return _protection_chain


def _maybe_reset_day() -> None:
    global _daily_stats
    today = _today_utc()
    reset_needed = False
    with _lock:
        if _daily_stats.date != today:
            if _daily_stats.realized_pnl != 0:
                _daily_returns.append(_daily_stats.realized_pnl)
                if len(_daily_returns) > 365:
                    _daily_returns.pop(0)
            _daily_stats = DailyStats(date=today)
            reset_needed = True
    if reset_needed:
        _get_chain().reset_day()
        log("info", f"Daily stats reset for {today}")


def can_open_position() -> bool:
    _maybe_reset_day()

    # Negative equity check: stop trading if paper balance is depleted
    from src.execution.paper import get_paper_balance
    from src.config import env as _env
    if _env.paper_trading:
        balance = get_paper_balance()
        if balance < 50:  # $50 minimum to open new positions
            log("warn", f"Trading halted — paper balance ${balance:.2f} below $50 minimum")
            return False

    with _lock:
        chain = _get_chain()
        ctx = ProtectionContext(
            realized_pnl_today=_daily_stats.realized_pnl,
            open_position_count=len(_open_positions),
            timestamp_ms=time.time() * 1000,
        )
        verdict = chain.can_open(ctx)
    if not verdict.allowed:
        log("warn", f"Blocked by {verdict.rule_name}: {verdict.reason}")
        return False
    return True


def register_open(position: Position) -> None:
    with _lock:
        _open_positions[position.id] = position


def register_close(position: Position, pnl_usd: float, is_partial: bool = False) -> None:
    _maybe_reset_day()
    with _lock:
        _daily_stats.realized_pnl += pnl_usd
        if not is_partial:
            # Only remove position and count trade on full close
            _open_positions.pop(position.id, None)
            _daily_stats.trade_count += 1
            _get_chain().notify_close(position, pnl_usd)


def update_position_price(position_id: str, current_price: float) -> None:
    with _lock:
        pos = _open_positions.get(position_id)
        if pos:
            pos.current_price = current_price


def get_open_positions() -> list[Position]:
    with _lock:
        return list(_open_positions.values())


def get_daily_stats() -> DailyStats:
    _maybe_reset_day()
    with _lock:
        return DailyStats(date=_daily_stats.date, realized_pnl=_daily_stats.realized_pnl, trade_count=_daily_stats.trade_count)


def is_circuit_breaker_open() -> bool:
    """Legacy API — checks if the chain would block a new position."""
    _maybe_reset_day()
    with _lock:
        chain = _get_chain()
        ctx = ProtectionContext(
            realized_pnl_today=_daily_stats.realized_pnl,
            open_position_count=len(_open_positions),
            timestamp_ms=time.time() * 1000,
        )
        return not chain.can_open(ctx).allowed


def compute_sharpe(risk_free_rate_annual: float = 0.05) -> Optional[float]:
    with _lock:
        returns_snapshot = list(_daily_returns)
    if len(returns_snapshot) < 30:
        return None
    n = len(returns_snapshot)
    mean = sum(returns_snapshot) / n
    variance = sum((r - mean) ** 2 for r in returns_snapshot) / (n - 1)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return None
    daily_rf = risk_free_rate_annual / 365
    return (mean - daily_rf) / std_dev * math.sqrt(365)


def compute_max_drawdown() -> float:
    with _lock:
        returns_snapshot = list(_daily_returns)
    if not returns_snapshot:
        return 0
    peak = equity = max_dd = 0.0
    for r in returns_snapshot:
        equity += r
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def compute_cvar(confidence: float = 0.95) -> Optional[float]:
    """Compute Conditional Value at Risk (Expected Shortfall).

    Returns the average loss in the worst (1-confidence) fraction of days.
    E.g., at 95% confidence, returns the average of the worst 5% of daily returns.
    Returns None if insufficient data (<30 days).
    """
    with _lock:
        returns_snapshot = list(_daily_returns)
    if len(returns_snapshot) < 30:
        return None
    sorted_returns = sorted(returns_snapshot)
    cutoff_idx = max(1, int(len(sorted_returns) * (1 - confidence)))
    tail = sorted_returns[:cutoff_idx]
    return sum(tail) / len(tail) if tail else None
