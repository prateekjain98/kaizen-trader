#!/usr/bin/env python3
"""Long-running poller that snapshots BTC mempool fee state every 5 min.

mempool.space has no history endpoint — we have to build our own. Run this as
a daemon (systemd / nohup) or invoke once-per-5min via cron. After ~7 days of
snapshots accumulate, src.backtesting.mempool_loader.regime_from_recent will
return real "elevated" / "extreme" classifications instead of "calm" fallback.

Output: data/mempool/snapshots.csv (append-only, single CSV header on first
write). Each row: ts_ms, fastest_fee, half_hour_fee, mempool_vsize,
total_pending_fee_btc.

Usage
-----
One-shot (suitable for cron — append a single row, exit):
    python scripts/collect_mempool.py --once

Daemon (loop forever, snapshot every 5 min):
    python scripts/collect_mempool.py
    nohup python scripts/collect_mempool.py >> data/mempool/collector.log 2>&1 &

Cron (every 5 min):
    */5 * * * * cd /path/to/kaizen-trader && /usr/bin/python3 scripts/collect_mempool.py --once

Systemd unit (sketch):
    [Service]
    ExecStart=/usr/bin/python3 /path/to/kaizen-trader/scripts/collect_mempool.py
    Restart=always
    WorkingDirectory=/path/to/kaizen-trader
"""

import argparse
import sys
import time
from pathlib import Path

# Allow running from repo root without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.mempool_loader import append_snapshot, _DEFAULT_CSV  # noqa: E402


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Snapshot BTC mempool fee state.")
    p.add_argument("--once", action="store_true", help="Take one snapshot and exit (for cron).")
    p.add_argument("--interval", type=int, default=300, help="Seconds between snapshots in daemon mode.")
    p.add_argument("--out", type=Path, default=_DEFAULT_CSV, help="CSV output path.")
    args = p.parse_args()

    _log(f"writing to {args.out}")

    while True:
        try:
            row = append_snapshot(args.out)
            if row is None:
                _log("fetch failed (network/API). will retry next tick.")
            else:
                _log(f"snapshot: fastest={row['fastest_fee']} sat/vB "
                     f"vsize={row['mempool_vsize']} pending={row['total_pending_fee_btc']:.4f} BTC")
        except Exception as e:
            _log(f"unexpected error: {e}")

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
