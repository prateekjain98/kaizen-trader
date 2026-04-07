"""Portfolio-level risk manager."""

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.config import env
from src.storage.database import log
from src.types import Position


@dataclass
class DailyStats:
    date: str
    realized_pnl: float = 0
    trade_count: int = 0


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_daily_stats = DailyStats(date=_today_utc())
_circuit_breaker_open = False
_open_positions: dict[str, Position] = {}
_daily_returns: list[float] = []


def _maybe_reset_day() -> None:
    global _daily_stats, _circuit_breaker_open
    today = _today_utc()
    if _daily_stats.date != today:
        if _daily_stats.realized_pnl != 0:
            _daily_returns.append(_daily_stats.realized_pnl)
            if len(_daily_returns) > 365:
                _daily_returns.pop(0)
        _daily_stats = DailyStats(date=today)
        _circuit_breaker_open = False
        log("info", f"Daily stats reset for {today}")


def can_open_position() -> bool:
    _maybe_reset_day()
    if _circuit_breaker_open:
        log("warn", f"Circuit breaker OPEN — daily loss ${-_daily_stats.realized_pnl:.2f} exceeded ${env.max_daily_loss_usd}")
        return False
    if len(_open_positions) >= env.max_open_positions:
        log("info", f"Position cap reached ({len(_open_positions)}/{env.max_open_positions})")
        return False
    return True


def register_open(position: Position) -> None:
    _open_positions[position.id] = position


def register_close(position: Position, pnl_usd: float) -> None:
    global _circuit_breaker_open
    _open_positions.pop(position.id, None)
    _maybe_reset_day()
    _daily_stats.realized_pnl += pnl_usd
    _daily_stats.trade_count += 1
    if _daily_stats.realized_pnl < -env.max_daily_loss_usd:
        _circuit_breaker_open = True
        log("warn", f"CIRCUIT BREAKER TRIGGERED — daily loss ${-_daily_stats.realized_pnl:.2f} > ${env.max_daily_loss_usd}")


def update_position_price(position_id: str, current_price: float) -> None:
    pos = _open_positions.get(position_id)
    if pos:
        pos.current_price = current_price


def get_open_positions() -> list[Position]:
    return list(_open_positions.values())


def get_daily_stats() -> DailyStats:
    _maybe_reset_day()
    return DailyStats(date=_daily_stats.date, realized_pnl=_daily_stats.realized_pnl, trade_count=_daily_stats.trade_count)


def is_circuit_breaker_open() -> bool:
    _maybe_reset_day()
    return _circuit_breaker_open


def compute_sharpe(risk_free_rate_annual: float = 0.05) -> Optional[float]:
    if len(_daily_returns) < 30:
        return None
    n = len(_daily_returns)
    mean = sum(_daily_returns) / n
    variance = sum((r - mean) ** 2 for r in _daily_returns) / (n - 1)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return None
    daily_rf = risk_free_rate_annual / 365
    return (mean - daily_rf) / std_dev * math.sqrt(365)


def compute_max_drawdown() -> float:
    if not _daily_returns:
        return 0
    peak = equity = max_dd = 0.0
    for r in _daily_returns:
        equity += r
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd
