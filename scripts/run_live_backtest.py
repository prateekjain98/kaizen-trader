#!/usr/bin/env python3
"""Run the LIVE-strategy backtest harness and write JSON results.

Replays src.engine.rule_brain.RuleBrain over historical Binance funding events.
See src/backtesting/live_replay.py for explicit limitations.

Usage:
    python scripts/run_live_backtest.py --symbols BTC,ETH,SOL --start 2026-01-01 --end 2026-04-01
    python scripts/run_live_backtest.py --symbols BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX --days 90
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtesting.live_replay import replay


def _parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX",
                   help="Comma-separated symbols")
    p.add_argument("--start", help="YYYY-MM-DD UTC")
    p.add_argument("--end", help="YYYY-MM-DD UTC")
    p.add_argument("--days", type=int, help="Lookback window from today (overrides --start)")
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--out", default=None, help="Output JSON path (default: data/backtest_<ts>.json)")
    args = p.parse_args()

    if args.days:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=args.days)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
    else:
        if not (args.start and args.end):
            p.error("must provide --days or both --start and --end")
        start_ms = _parse_date(args.start)
        end_ms = _parse_date(args.end)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Replaying {len(symbols)} symbols over {(end_ms-start_ms)/86400000:.0f}d (balance ${args.balance:.0f})")
    t0 = time.time()
    result = replay(symbols=symbols, start_ms=start_ms, end_ms=end_ms, initial_balance=args.balance)
    elapsed = time.time() - t0

    out = result.to_dict()
    out["elapsed_seconds"] = elapsed
    out["generated_at"] = datetime.now(timezone.utc).isoformat()

    out_path = args.out or f"data/backtest_{int(time.time())}.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n=== BACKTEST RESULT ===")
    print(f"  Symbols:        {len(symbols)} ({','.join(symbols)})")
    print(f"  Period:         {datetime.fromtimestamp(start_ms/1000, timezone.utc).date()} → "
          f"{datetime.fromtimestamp(end_ms/1000, timezone.utc).date()}")
    print(f"  Trades:         {result.num_trades}")
    print(f"  Win rate:       {result.win_rate:.1f}%")
    print(f"  Total PnL:      ${result.total_pnl_usd:+.2f} ({result.total_pnl_pct:+.2f}%)")
    print(f"  Avg trade:      {result.avg_trade_pnl_pct:+.2f}%")
    print(f"  Max DD:         {result.max_dd_pct:.2f}%")
    print(f"  Sharpe proxy:   {result.sharpe_proxy:.3f}")
    print(f"  Fees paid:      ${result.fees_paid_usd:.2f}")
    print(f"  Final balance:  ${result.final_balance:.2f}")
    print(f"  Elapsed:        {elapsed:.1f}s")
    print(f"\nLimitations (declared honest):")
    for n in result.notes:
        print(f"  - {n}")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
