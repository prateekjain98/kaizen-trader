#!/usr/bin/env python3
"""Forward-collect Binance Futures liquidations to daily CSV files.

WHY: Binance's `!forceOrder@arr` WS is LIVE-ONLY — no history endpoint.
For backtest validation of the liquidation_cascade strategy we either:
  (a) use a single-exchange free historical proxy (Bitfinex — see
      src/backtesting/liquidation_loader.py), or
  (b) accumulate Binance live data forward-in-time, which is the EXACT
      source the prod tracker uses.

This script does (b). Run for ≥14 days to build a statistically-meaningful
dataset, then point live_replay at it via load_forward_collected().

USAGE
-----
  $ python3 scripts/collect_liquidations.py
  $ python3 scripts/collect_liquidations.py --out data/liquidations/forward

It writes one CSV per UTC day to <out>/<YYYY-MM-DD>.csv, header:
  timestamp,symbol,side,size_usd,price

Run under nohup / systemd / supervisor for persistence:
  $ nohup python3 scripts/collect_liquidations.py >> liq.log 2>&1 &

Requires: websocket-client (already a project dep via liquidation_tracker).
Stdlib only otherwise — no pandas, no aiohttp.
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _REPO_ROOT / "data" / "liquidations" / "forward"
_BINANCE_WS = "wss://fstream.binance.com/stream?streams=!forceOrder@arr"

# Match LiquidationTracker's allowlist for scaled-supply tokens.
_BINANCE_1000_PREFIX = frozenset({
    "1000SHIB", "1000LUNC", "1000PEPE", "1000FLOKI",
    "1000BONK", "1000SATS", "1000RATS", "1000XEC",
    "1000CHEEMS", "1000WHY", "1000CAT",
})


class DailyCSVWriter:
    """Append rows to <out>/<YYYY-MM-DD>.csv, rolling at UTC midnight."""

    FIELDS = ["timestamp", "symbol", "side", "size_usd", "price"]

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cur_date: str | None = None
        self._fp = None
        self._writer: csv.DictWriter | None = None
        self._row_count = 0

    def _date_for(self, ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def _rotate(self, date_str: str) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except OSError:
                pass
        path = self.out_dir / f"{date_str}.csv"
        is_new = not path.exists()
        self._fp = open(path, "a", newline="")
        self._writer = csv.DictWriter(self._fp, fieldnames=self.FIELDS)
        if is_new:
            self._writer.writeheader()
        self._cur_date = date_str

    def write(self, row: dict) -> None:
        with self._lock:
            d = self._date_for(row["timestamp"])
            if d != self._cur_date:
                self._rotate(d)
            self._writer.writerow(row)  # type: ignore[union-attr]
            self._row_count += 1
            # Flush every 25 rows so kill -9 doesn't lose the last hour.
            if self._row_count % 25 == 0:
                self._fp.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        with self._lock:
            if self._fp is not None:
                try:
                    self._fp.flush()
                    self._fp.close()
                except OSError:
                    pass
                self._fp = None
                self._writer = None


def _parse_message(message: str) -> dict | None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return None
    payload = data.get("data") or {}
    order = payload.get("o") or {}
    sym_raw = order.get("s", "")
    if not sym_raw.endswith("USDT"):
        return None
    sym = sym_raw.replace("USDT", "")
    if sym in _BINANCE_1000_PREFIX:
        sym = sym[4:]
    side_raw = order.get("S", "")
    try:
        qty = float(order.get("q", 0) or 0)
        avg_price = float(order.get("ap") or order.get("p") or 0)
    except (TypeError, ValueError):
        return None
    usd = qty * avg_price
    if usd <= 0:
        return None
    # SELL = long was liquidated. BUY = short was liquidated.
    side = "long" if side_raw == "SELL" else "short"
    # Prefer the venue's event timestamp ('E' or 'T') if present, else now.
    ts_ms = int(order.get("T") or payload.get("E") or time.time() * 1000)
    return {
        "timestamp": ts_ms,
        "symbol": sym,
        "side": side,
        "size_usd": round(usd, 4),
        "price": avg_price,
    }


def _run(out_dir: Path) -> int:
    try:
        import websocket  # type: ignore
    except ImportError:
        print("ERROR: websocket-client not installed. pip install websocket-client",
              file=sys.stderr)
        return 1

    writer = DailyCSVWriter(out_dir)
    stats = {"events": 0, "errors": 0, "started": time.time()}

    def on_message(_ws, message):
        row = _parse_message(message)
        if row is None:
            return
        writer.write(row)
        stats["events"] += 1
        if stats["events"] % 100 == 0:
            elapsed = max(1, time.time() - stats["started"])
            print(f"[{datetime.utcnow().isoformat()}Z] events={stats['events']} "
                  f"({stats['events']/elapsed:.1f}/s)")

    def on_error(_ws, error):
        stats["errors"] += 1
        print(f"WS error: {error}", file=sys.stderr)

    def on_close(_ws, code, msg):
        print(f"WS closed code={code} msg={msg}", file=sys.stderr)

    def on_open(_ws):
        print(f"Connected to {_BINANCE_WS}, writing to {out_dir}")

    def shutdown(*_a):
        print("Shutting down — flushing CSV.")
        writer.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    backoff = 1.0
    while True:
        try:
            ws = websocket.WebSocketApp(
                _BINANCE_WS,
                on_message=on_message, on_error=on_error,
                on_close=on_close, on_open=on_open,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"run_forever raised: {e}", file=sys.stderr)
        # Exponential backoff up to 60s, then steady reconnect.
        time.sleep(min(60.0, backoff))
        backoff = min(60.0, backoff * 2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", type=Path, default=_DEFAULT_OUT,
                   help=f"output directory (default: {_DEFAULT_OUT})")
    args = p.parse_args()
    return _run(args.out)


if __name__ == "__main__":
    sys.exit(main())
