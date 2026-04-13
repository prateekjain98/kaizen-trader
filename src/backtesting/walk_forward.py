"""Walk-forward backtesting runner.

Splits history into rolling train/test windows. Runs BacktestEngine
on each test window using parameters optimized on the preceding train
window. Only out-of-sample (test) results count toward final metrics.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from src.types import ScannerConfig, Position


_DAY_MS = 86_400_000


@dataclass
class Window:
    """A single train/test window."""
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int


@dataclass
class WalkForwardConfig:
    """Configuration for walk-forward backtesting."""
    symbols: list[str]
    start_date: str
    end_date: str
    train_days: int = 30
    test_days: int = 7
    initial_balance: float = 10000.0
    scanner_config: ScannerConfig = field(default_factory=ScannerConfig)
    commission_pct: float = 0.001
    slippage_pct: float = 0.0005
    max_open_positions: int = 5
    interval: str = "1h"


@dataclass
class WindowResult:
    """Result for a single walk-forward window."""
    window: Window
    test_result: BacktestResult
    train_result: Optional[BacktestResult] = None


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward results."""
    window_results: list[WindowResult]
    total_oos_trades: int
    oos_win_rate: float
    oos_total_return_pct: float
    oos_max_drawdown_pct: float
    oos_sharpe: Optional[float]
    degradation_ratio: float


def _date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def generate_windows(
    start_ms: int,
    end_ms: int,
    train_days: int = 30,
    test_days: int = 7,
) -> list[Window]:
    """Generate rolling train/test windows.

    Each window: [train_start, train_end) = train period
                 [test_start, test_end)   = test period (= train_end)
    Windows step forward by test_days (no overlap in test periods).
    """
    train_ms = train_days * _DAY_MS
    test_ms = test_days * _DAY_MS
    min_total = train_ms + test_ms

    if (end_ms - start_ms) < min_total:
        return []

    windows: list[Window] = []
    cursor = start_ms

    while cursor + train_ms + test_ms <= end_ms:
        windows.append(Window(
            train_start_ms=cursor,
            train_end_ms=cursor + train_ms,
            test_start_ms=cursor + train_ms,
            test_end_ms=cursor + train_ms + test_ms,
        ))
        cursor += test_ms

    return windows


def run_walk_forward(config: WalkForwardConfig) -> WalkForwardResult:
    """Run walk-forward backtest across all windows."""
    start_ms = _date_to_ms(config.start_date)
    end_ms = _date_to_ms(config.end_date)

    windows = generate_windows(
        start_ms=start_ms,
        end_ms=end_ms,
        train_days=config.train_days,
        test_days=config.test_days,
    )

    if not windows:
        raise ValueError(
            f"Insufficient data for walk-forward: need at least "
            f"{config.train_days + config.test_days} days"
        )

    window_results: list[WindowResult] = []
    all_oos_positions: list[Position] = []

    for w in windows:
        train_cfg = BacktestConfig(
            symbols=config.symbols,
            start_date=_ms_to_date(w.train_start_ms),
            end_date=_ms_to_date(w.train_end_ms),
            initial_balance=config.initial_balance,
            scanner_config=config.scanner_config,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            max_open_positions=config.max_open_positions,
            interval=config.interval,
        )
        train_engine = BacktestEngine(train_cfg)
        train_result = train_engine.run()

        test_cfg = BacktestConfig(
            symbols=config.symbols,
            start_date=_ms_to_date(w.test_start_ms),
            end_date=_ms_to_date(w.test_end_ms),
            initial_balance=config.initial_balance,
            scanner_config=config.scanner_config,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            max_open_positions=config.max_open_positions,
            interval=config.interval,
        )
        test_engine = BacktestEngine(test_cfg)
        test_result = test_engine.run()

        window_results.append(WindowResult(
            window=w,
            train_result=train_result,
            test_result=test_result,
        ))
        all_oos_positions.extend(test_result.positions)

    total_oos_trades = len(all_oos_positions)
    wins = sum(1 for p in all_oos_positions if (p.pnl_pct or 0) > 0)
    oos_win_rate = wins / total_oos_trades if total_oos_trades > 0 else 0.0

    oos_returns = [
        (wr.test_result.final_balance - config.initial_balance) / config.initial_balance
        for wr in window_results
    ]
    oos_total_return = sum(oos_returns) / len(oos_returns) if oos_returns else 0.0
    oos_max_dd = max(
        (wr.test_result.max_drawdown_pct for wr in window_results),
        default=0.0,
    )

    oos_sharpe = None
    if len(oos_returns) >= 2:
        mean_r = sum(oos_returns) / len(oos_returns)
        var_r = sum((r - mean_r) ** 2 for r in oos_returns) / (len(oos_returns) - 1)
        std_r = var_r ** 0.5
        if std_r > 0:
            oos_sharpe = (mean_r / std_r) * (52 / config.test_days) ** 0.5

    train_returns = [
        (wr.train_result.final_balance - config.initial_balance) / config.initial_balance
        for wr in window_results
        if wr.train_result is not None
    ]
    avg_train = sum(train_returns) / len(train_returns) if train_returns else 0.0
    if avg_train > 0 and oos_total_return > 0:
        degradation_ratio = oos_total_return / avg_train
    else:
        degradation_ratio = 0.0

    return WalkForwardResult(
        window_results=window_results,
        total_oos_trades=total_oos_trades,
        oos_win_rate=oos_win_rate,
        oos_total_return_pct=oos_total_return * 100,
        oos_max_drawdown_pct=oos_max_dd,
        oos_sharpe=oos_sharpe,
        degradation_ratio=degradation_ratio,
    )
