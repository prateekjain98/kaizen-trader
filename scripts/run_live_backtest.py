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
    p.add_argument("--no-filters", action="store_true",
                   help="Disable replayable entry-filter chain (brain-only)")
    p.add_argument("--no-top-movers", action="store_true",
                   help="Disable top-movers historical reconstruction (funding-only)")
    p.add_argument("--include-15m-accel", action="store_true",
                   help="Enable sub-hour accel detection from 15m klines "
                        "(opt-in; empirically hurts PnL — kept for tuning)")
    p.add_argument("--min-score", type=int, default=None,
                   help="Override RuleBrain MIN_SCORE_TO_TRADE for this run "
                        "(prod default = 40). Lowering it tests whether more "
                        "marginal signals would have been profitable.")
    p.add_argument("--split", type=int, default=1,
                   help="Split the date range into N equal non-overlapping windows "
                        "(out-of-sample validation). With N>1, each window runs "
                        "independently and final verdict requires ALL windows positive.")
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
    n_splits = max(1, args.split)
    window_ms = (end_ms - start_ms) // n_splits
    windows: list[tuple[int, int]] = [
        (start_ms + i * window_ms, start_ms + (i + 1) * window_ms if i < n_splits - 1 else end_ms)
        for i in range(n_splits)
    ]

    print(f"Replaying {len(symbols)} symbols over {(end_ms-start_ms)/86400000:.0f}d "
          f"(balance ${args.balance:.0f}, splits={n_splits})")

    all_results = []
    for w_idx, (w_start, w_end) in enumerate(windows, start=1):
        if n_splits > 1:
            print(f"\n--- Window {w_idx}/{n_splits}: "
                  f"{datetime.fromtimestamp(w_start/1000, timezone.utc).date()} → "
                  f"{datetime.fromtimestamp(w_end/1000, timezone.utc).date()} ---")
        t0 = time.time()
        result = replay(symbols=symbols, start_ms=w_start, end_ms=w_end,
                        initial_balance=args.balance,
                        apply_filters=not args.no_filters,
                        include_top_movers=not args.no_top_movers,
                        include_15m_accel=args.include_15m_accel,
                        min_score_override=args.min_score)
        elapsed = time.time() - t0
        all_results.append(result)

        print(f"  Trades:       {result.num_trades}")
        print(f"  Win rate:     {result.win_rate:.1f}%")
        print(f"  PnL:          ${result.total_pnl_usd:+.2f} ({result.total_pnl_pct:+.2f}%)")
        print(f"  Avg trade:    {result.avg_trade_pnl_pct:+.2f}%")
        print(f"  Sharpe proxy: {result.sharpe_proxy:.3f}")
        print(f"  Max DD:       {result.max_dd_pct:.2f}%")
        bs = result.by_strategy()
        if bs:
            print(f"  By strategy:")
            for k, v in sorted(bs.items(), key=lambda kv: -kv[1]["total_pnl_usd"]):
                print(f"    {k:18s} n={v['num_trades']:3d} WR={v['win_rate']:.0f}% "
                      f"pnl=${v['total_pnl_usd']:+.2f} avg={v['avg_trade_pnl_pct']:+.2f}%")
        print(f"  Elapsed:      {elapsed:.1f}s")

    # Aggregate
    total_pnl = sum(r.total_pnl_usd for r in all_results)
    total_trades = sum(r.num_trades for r in all_results)
    all_positive = all(r.total_pnl_usd >= 0 for r in all_results)

    out = {
        "windows": [r.to_dict() for r in all_results],
        "aggregate": {
            "n_windows": n_splits,
            "all_windows_non_negative": all_positive,
            "total_pnl_usd": total_pnl,
            "total_trades": total_trades,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
    }
    out_path = args.out or f"data/backtest_{int(time.time())}.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n=== AGGREGATE ===")
    print(f"  Windows:                  {n_splits}")
    print(f"  Total trades:             {total_trades}")
    print(f"  Total PnL:                ${total_pnl:+.2f}")
    print(f"  All windows non-negative: {all_positive}")
    print(f"  Wrote {out_path}")
    if n_splits > 1:
        print(f"\n  Out-of-sample verdict:    "
              f"{'ROBUST (all windows ≥ 0)' if all_positive else 'OVERFIT-RISK (a window negative)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
