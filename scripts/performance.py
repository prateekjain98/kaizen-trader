"""Performance report — prints a full metrics breakdown."""

import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from src.evaluation.metrics import compute_metrics, format_metrics
from src.storage.database import get_closed_trades

args = sys.argv[1:]
last_index = args.index("--last") if "--last" in args else -1
limit = int(args[last_index + 1]) if last_index >= 0 and last_index + 1 < len(args) else 500
csv_mode = "--csv" in args

metrics = compute_metrics(limit)
trades = get_closed_trades(limit)

if csv_mode:
    print("symbol,strategy,side,tier,pnl_pct,pnl_usd,hold_hours,exit_reason,qual_score,opened_at")
    for t in trades:
        hold_h = f"{(t.closed_at - t.opened_at) / 3_600_000:.2f}" if t.closed_at else ""
        print(",".join([
            t.symbol, t.strategy, t.side, t.tier,
            f"{t.pnl_pct:.4f}" if t.pnl_pct is not None else "",
            f"{t.pnl_usd:.2f}" if t.pnl_usd is not None else "",
            hold_h, t.exit_reason or "", str(t.qual_score),
            datetime.fromtimestamp(t.opened_at / 1000, tz=timezone.utc).isoformat() if t.opened_at else "",
        ]))
else:
    print()
    print("=" * 50)
    print("  kaizen-trader — Performance Report")
    print(f"  {datetime.now(timezone.utc).isoformat()}")
    print("=" * 50)
    print()
    print(format_metrics(metrics))
    print()
    print("=" * 50)
    print()
