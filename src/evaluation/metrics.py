"""Performance metrics engine."""

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from src.storage.database import get_closed_trades
from src.utils.safe_math import safe_ratio


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
    # New metrics
    omega_ratio: Optional[float] = None        # sum(gains) / sum(losses) above/below threshold
    gain_to_pain: Optional[float] = None       # total return / sum of absolute losses
    expectancy: Optional[float] = None         # (win_rate * avg_win) - (loss_rate * avg_loss)
    avg_win_hold_hours: float = 0.0
    avg_loss_hold_hours: float = 0.0
    median_pnl_pct: float = 0.0
    avg_mae_pct: float = 0.0                   # average max adverse excursion
    avg_mfe_pct: float = 0.0                   # average max favorable excursion
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
    if avg_loss_pct == 0 or avg_win_pct == 0:
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

    sharpe_raw = (pnl_mean / pnl_std) * math.sqrt(252) if pnl_std > 0 and len(pnl_pcts) >= 30 else None
    sortino_raw = (pnl_mean / downside_std) * math.sqrt(252) if downside_std > 0 and len(pnl_pcts) >= 30 else None
    sharpe = safe_ratio(sharpe_raw) if sharpe_raw is not None else None
    sortino = safe_ratio(sortino_raw) if sortino_raw is not None else None

    max_dd = _max_drawdown(pnl_usds)
    total_pnl = sum(pnl_usds)
    calmar_raw = total_pnl / max_dd if max_dd > 0 and total_pnl > 0 else None
    calmar = safe_ratio(calmar_raw) if calmar_raw is not None else None

    # Omega ratio: sum of gains / sum of losses (threshold = 0)
    gains_sum = sum(p for p in pnl_pcts if p > 0)
    losses_sum = abs(sum(p for p in pnl_pcts if p < 0))
    omega = safe_ratio(gains_sum / losses_sum) if losses_sum > 0 else None

    # Gain-to-Pain: total return / sum of absolute losses
    abs_losses_sum = sum(abs(p) for p in pnl_usds if p < 0)
    gain_to_pain = safe_ratio(total_pnl / abs_losses_sum) if abs_losses_sum > 0 else None

    # Expectancy: (win_rate * avg_win) - (loss_rate * avg_loss)
    wins_pct = [p for p in pnl_pcts if p > 0]
    losses_pct = [p for p in pnl_pcts if p <= 0]
    avg_win_pct_val = _mean(wins_pct) if wins_pct else 0
    avg_loss_pct_val = abs(_mean(losses_pct)) if losses_pct else 0
    expectancy = (win_rate * avg_win_pct_val) - ((1 - win_rate) * avg_loss_pct_val)

    # Median PnL
    sorted_pnls = sorted(pnl_pcts)
    n = len(sorted_pnls)
    median_pnl = sorted_pnls[n // 2] if n % 2 == 1 else (sorted_pnls[n // 2 - 1] + sorted_pnls[n // 2]) / 2 if n > 0 else 0

    # Win/loss hold time breakdown
    win_holds = [(t.closed_at - t.opened_at) / 3_600_000 for t in trades if t.closed_at and (t.pnl_pct or 0) > 0]
    loss_holds = [(t.closed_at - t.opened_at) / 3_600_000 for t in trades if t.closed_at and (t.pnl_pct or 0) <= 0]

    # MAE/MFE averages (if positions have the data)
    mae_vals = [getattr(t, "mae_pct", 0) for t in trades if hasattr(t, "mae_pct") and getattr(t, "mae_pct", 0) != 0]
    mfe_vals = [getattr(t, "mfe_pct", 0) for t in trades if hasattr(t, "mfe_pct") and getattr(t, "mfe_pct", 0) != 0]

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
        avg_hold_hours=avg_hold_hours,
        omega_ratio=omega,
        gain_to_pain=gain_to_pain,
        expectancy=expectancy,
        avg_win_hold_hours=_mean(win_holds),
        avg_loss_hold_hours=_mean(loss_holds),
        median_pnl_pct=median_pnl,
        avg_mae_pct=_mean(mae_vals) if mae_vals else 0,
        avg_mfe_pct=_mean(mfe_vals) if mfe_vals else 0,
        by_strategy=by_strategy,
    )


def format_metrics(m: PortfolioMetrics) -> str:
    lines = [
        f"Trades:        {m.total_trades}",
        f"Win rate:      {m.win_rate*100:.1f}%",
        f"Profit factor: {'inf' if m.profit_factor == float('inf') else f'{m.profit_factor:.2f}'}",
        f"Total P&L:     ${m.total_pnl_usd:.2f}",
        f"Median P&L:    {m.median_pnl_pct*100:.2f}%",
        f"Expectancy:    {m.expectancy*100:.3f}% per trade" if m.expectancy is not None else "Expectancy:    N/A",
        f"Avg hold:      {m.avg_hold_hours:.1f}h (win: {m.avg_win_hold_hours:.1f}h, loss: {m.avg_loss_hold_hours:.1f}h)",
        f"Max drawdown:  {m.max_drawdown_pct*100:.1f}%",
        f"Sharpe:        {m.sharpe_ratio:.2f}" if m.sharpe_ratio is not None else "Sharpe:        (insufficient data)",
        f"Sortino:       {m.sortino_ratio:.2f}" if m.sortino_ratio is not None else "Sortino:       (insufficient data)",
        f"Calmar:        {m.calmar_ratio:.2f}" if m.calmar_ratio is not None else "Calmar:        (insufficient data)",
        f"Omega:         {m.omega_ratio:.2f}" if m.omega_ratio is not None else "Omega:         (insufficient data)",
        f"Gain/Pain:     {m.gain_to_pain:.2f}" if m.gain_to_pain is not None else "Gain/Pain:     (insufficient data)",
        f"Avg MAE:       {m.avg_mae_pct*100:.2f}%  Avg MFE: {m.avg_mfe_pct*100:.2f}%",
        "",
        "By strategy:",
    ]
    for s in m.by_strategy:
        lines.append(
            f"  {s.strategy:<28} trades={s.total_trades:>3}  win={s.win_rate*100:.0f}%  "
            f"pnl=${s.total_pnl_usd:>8.0f}  kelly={s.kelly_fraction*100:.1f}%"
        )
    return "\n".join(lines)


# ─── Monte Carlo Significance Test ───────────────────────────────────────────

@dataclass
class SignificanceResult:
    strategy: str
    actual_sharpe: float
    p_value: float          # fraction of random shuffles with higher Sharpe
    significant: bool       # True if p < 0.05
    num_trades: int
    num_simulations: int


def monte_carlo_significance(
    strategy: Optional[str] = None,
    num_simulations: int = 5000,
    limit: int = 500,
) -> list[SignificanceResult]:
    """Test whether strategy returns are statistically significant vs. random.

    Shuffles trade returns N times, computes Sharpe each time, and reports
    the p-value (fraction of random shuffles with Sharpe >= actual).

    Args:
        strategy: Test a specific strategy, or None to test all.
        num_simulations: Number of random permutations.
        limit: Max trades to consider.

    Returns:
        List of SignificanceResult per strategy.
    """
    trades = get_closed_trades(limit)
    trades = [t for t in trades if t.pnl_pct is not None]

    if strategy:
        groups = {strategy: [t for t in trades if t.strategy == strategy]}
    else:
        groups: dict[str, list] = {}
        for t in trades:
            groups.setdefault(t.strategy, []).append(t)

    results = []
    for strat, strat_trades in groups.items():
        returns = [t.pnl_pct for t in strat_trades if t.pnl_pct is not None]
        if len(returns) < 20:
            continue

        actual_sharpe = _compute_sharpe(returns)
        if actual_sharpe is None:
            continue

        # Monte Carlo: shuffle returns and compute Sharpe each time
        count_better = 0
        for _ in range(num_simulations):
            shuffled = returns.copy()
            random.shuffle(shuffled)
            sim_sharpe = _compute_sharpe(shuffled)
            if sim_sharpe is not None and sim_sharpe >= actual_sharpe:
                count_better += 1

        p_value = count_better / num_simulations

        results.append(SignificanceResult(
            strategy=strat,
            actual_sharpe=actual_sharpe,
            p_value=p_value,
            significant=p_value < 0.05,
            num_trades=len(returns),
            num_simulations=num_simulations,
        ))

    return results


def _compute_sharpe(returns: list[float]) -> Optional[float]:
    """Compute Sharpe ratio from a list of per-trade returns."""
    if len(returns) < 5:
        return None
    avg = _mean(returns)
    std = _std_dev(returns, avg)
    if std < 1e-12:
        return None
    return safe_ratio(avg / std)
