#!/usr/bin/env python3
"""CLI entry point for backtesting.

Usage:
    python scripts/backtest.py --symbol BTC --start 2025-01-01 --end 2025-06-01
    python scripts/backtest.py --symbol BTC,ETH --start 2025-01-01 --end 2025-06-01 --interval 4h
    python scripts/backtest.py --symbol SOL --start 2025-03-01 --end 2025-04-01 --balance 5000
"""

import argparse
import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtesting.engine import BacktestEngine, BacktestConfig
from src.types import ScannerConfig
from src.evaluation.metrics import format_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest kaizen-trader strategies against historical data")
    parser.add_argument("--symbol", required=True, help="Comma-separated symbols (e.g. BTC,ETH,SOL)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--balance", type=float, default=10000.0, help="Initial balance in USD (default: 10000)")
    parser.add_argument("--interval", default="1h", help="Candle interval: 1m, 5m, 15m, 1h, 4h, 1d (default: 1h)")
    parser.add_argument("--max-positions", type=int, default=5, help="Max simultaneous open positions (default: 5)")
    parser.add_argument("--commission", type=float, default=0.001, help="Commission per trade as decimal (default: 0.001)")
    parser.add_argument("--slippage", type=float, default=0.0005, help="Slippage per trade as decimal (default: 0.0005)")

    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbol.split(",")]

    config = BacktestConfig(
        symbols=symbols,
        start_date=args.start,
        end_date=args.end,
        initial_balance=args.balance,
        scanner_config=ScannerConfig(),
        commission_pct=args.commission,
        slippage_pct=args.slippage,
        max_open_positions=args.max_positions,
        interval=args.interval,
    )

    print(f"Running backtest: {', '.join(symbols)}")
    print(f"  Period:     {args.start} to {args.end}")
    print(f"  Interval:   {args.interval}")
    print(f"  Balance:    ${config.initial_balance:,.2f}")
    print(f"  Commission: {config.commission_pct*100:.2f}%")
    print(f"  Slippage:   {config.slippage_pct*100:.3f}%")
    print(f"  Max positions: {config.max_open_positions}")
    print()

    engine = BacktestEngine(config)
    result = engine.run()

    print("=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Final balance:   ${result.final_balance:,.2f}")
    print(f"Total return:    {((result.final_balance - config.initial_balance) / config.initial_balance) * 100:.2f}%")
    print(f"Max drawdown:    {result.max_drawdown_pct * 100:.2f}%")
    print(f"Total trades:    {result.total_trades}")
    print(f"Equity curve:    {len(result.equity_curve)} data points")
    print()
    print(format_metrics(result.metrics))
    print("=" * 60)


if __name__ == "__main__":
    main()
