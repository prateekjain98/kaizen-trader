"""Performance metrics engine."""

import math
from dataclasses import dataclass, field
from typing import Optional

from src.storage.database import get_closed_trades


@dataclass
class StrategyMetrics:
    strategy: str
    total_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    avg_hold_hours: float
    total_pnl_usd: float
    kelly_fraction: float
    max_consec_losses: int


@dataclass
class PortfolioMetrics:
    total_trades: int
    win_rate: float
    profit_factor: float
    total_pnl_usd: float
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    calmar_ratio: Optional[float]
    max_drawdown_pct: float
    avg_hold_hours: float
    by_strategy: list[StrategyMetrics] = field(default_factory=list)


def _mean(arr: list[float]) -> float:
    return sum(arr) / len(arr) if arr else 0


def _std_dev(arr: list[float], avg: Optional[float] = None) -> float:
    m = avg if avg is not None else _mean(arr)
    n = len(arr)
    if n < 2:
        return 0
    variance = sum((v - m) ** 2 for v in arr) / (n - 1)
    return math.sqrt(variance)


def _max_drawdown(returns: list[float]) -> float:
    peak = equity = max_dd = 0.0
    for r in returns:
        equity += r
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _max_consecutive_losses(pnls: list[float]) -> int:
    mx = streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            mx = max(mx, streak)
        else:
            streak = 0
    return mx


def _kelly_fraction(win_rate: float, avg_win_pct: float, avg_loss_pct: float) -> float:
    if avg_loss_pct == 0:
        return 0
    b = avg_win_pct / avg_loss_pct
    p = win_rate
    q = 1 - p
    return max(0, (b * p - q) / b)


def compute_metrics(lookback_trades: int = 500) -> PortfolioMetrics:
    trades = get_closed_trades(lookback_trades)
    if not trades:
        return PortfolioMetrics(
            total_trades=0, win_rate=0, profit_factor=0, total_pnl_usd=0,
            sharpe_ratio=None, sortino_ratio=None, calmar_ratio=None,
            max_drawdown_pct=0, avg_hold_hours=0,
        )

    pnl_pcts = [t.pnl_pct or 0 for t in trades]
    pnl_usds = [t.pnl_usd or 0 for t in trades]
    wins_usd = [p for p in pnl_usds if p > 0]
    losses_usd = [p for p in pnl_usds if p <= 0]

    gross_wins = sum(wins_usd)
    gross_losses = abs(sum(losses_usd))

    win_rate = len(wins_usd) / len(trades)
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0)

    hold_hours = [
        (t.closed_at - t.opened_at) / 3_600_000 if t.closed_at else 0
        for t in trades
    ]
    avg_hold_hours = _mean(hold_hours)

    pnl_mean = _mean(pnl_pcts)
    pnl_std = _std_dev(pnl_pcts, pnl_mean)
    downside = [p for p in pnl_pcts if p < 0]
    downside_std = _std_dev(downside, 0)

    sharpe = (pnl_mean / pnl_std) * math.sqrt(252) if pnl_std > 0 and len(pnl_pcts) >= 30 else None
    sortino = (pnl_mean / downside_std) * math.sqrt(252) if downside_std > 0 and len(pnl_pcts) >= 30 else None

    max_dd = _max_drawdown(pnl_usds)
    total_pnl = sum(pnl_usds)
    calmar = total_pnl / max_dd if max_dd > 0 and total_pnl > 0 else None

    # Per-strategy breakdown
    strategy_map: dict[str, list] = {}
    for t in trades:
        strategy_map.setdefault(t.strategy, []).append(t)

    by_strategy: list[StrategyMetrics] = []
    for strategy, st_trades in strategy_map.items():
        st_pnls = [t.pnl_pct or 0 for t in st_trades]
        st_wins = [p for p in st_pnls if p > 0]
        st_losses = [p for p in st_pnls if p <= 0]
        st_win_rate = len(st_wins) / len(st_trades)
        avg_win = _mean(st_wins) if st_wins else 0
        avg_loss = abs(_mean(st_losses)) if st_losses else 0

        by_strategy.append(StrategyMetrics(
            strategy=strategy,
            total_trades=len(st_trades),
            win_rate=st_win_rate,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            profit_factor=(st_win_rate * avg_win) / ((1 - st_win_rate) * avg_loss) if avg_loss > 0 else 0,
            avg_hold_hours=_mean([(t.closed_at - t.opened_at) / 3_600_000 if t.closed_at else 0 for t in st_trades]),
            total_pnl_usd=sum(t.pnl_usd or 0 for t in st_trades),
            kelly_fraction=_kelly_fraction(st_win_rate, avg_win, avg_loss),
            max_consec_losses=_max_consecutive_losses(st_pnls),
        ))

    by_strategy.sort(key=lambda s: s.total_pnl_usd, reverse=True)

    return PortfolioMetrics(
        total_trades=len(trades), win_rate=win_rate, profit_factor=profit_factor,
        total_pnl_usd=total_pnl, sharpe_ratio=sharpe, sortino_ratio=sortino,
        calmar_ratio=calmar, max_drawdown_pct=max_dd,
        avg_hold_hours=avg_hold_hours, by_strategy=by_strategy,
    )


def format_metrics(m: PortfolioMetrics) -> str:
    lines = [
        f"Trades:        {m.total_trades}",
        f"Win rate:      {m.win_rate*100:.1f}%",
        f"Profit factor: {'inf' if m.profit_factor == float('inf') else f'{m.profit_factor:.2f}'}",
        f"Total P&L:     ${m.total_pnl_usd:.2f}",
        f"Avg hold:      {m.avg_hold_hours:.1f}h",
        f"Max drawdown:  {m.max_drawdown_pct*100:.1f}%",
        f"Sharpe:        {m.sharpe_ratio:.2f}" if m.sharpe_ratio is not None else "Sharpe:        (insufficient data)",
        f"Sortino:       {m.sortino_ratio:.2f}" if m.sortino_ratio is not None else "Sortino:       (insufficient data)",
        f"Calmar:        {m.calmar_ratio:.2f}" if m.calmar_ratio is not None else "Calmar:        (insufficient data)",
        "",
        "By strategy:",
    ]
    for s in m.by_strategy:
        lines.append(
            f"  {s.strategy:<28} trades={s.total_trades:>3}  win={s.win_rate*100:.0f}%  "
            f"pnl=${s.total_pnl_usd:>8.0f}  kelly={s.kelly_fraction*100:.1f}%"
        )
    return "\n".join(lines)
