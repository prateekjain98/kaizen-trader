#!/usr/bin/env python3
"""Counterfactual: would prod's fast_cut events have done better held?

Reads a hardcoded list of real prod fast_cut entries (from journalctl
extraction), fetches 1h Binance klines for the 48h after entry, and
simulates the prod exit logic WITH and WITHOUT fast_cut. Reports
PnL delta. Real data only.
"""
import sys, os, requests, time
from typing import Optional

# (symbol, ts_iso_utc, entry, stop, target, side, size_usd, actual_fast_cut_pnl_usd)
EVENTS = [
    ("CHIP",  "2026-04-27 05:01:14", 0.0824, 0.0741, 0.1029, "long", 11.0, -0.35),
    ("SONIC", "2026-04-27 05:01:12", 0.0444, 0.0399, 0.0555, "long", 11.0, -0.74),
    ("BIO",   "2026-04-30 06:01:27", 0.0419, 0.0395, 0.0523, "long", 14.0, -0.30),
    ("MEGA",  "2026-04-30 14:01:46", 0.1721, 0.1549, 0.2151, "long", 13.0, -0.30),
    ("NFP",   "2026-05-01 10:01:43", 0.0158, 0.0150, 0.0197, "long", 13.0, -0.29),
    ("NFP",   "2026-05-01 18:00:47", 0.0180, 0.0167, 0.0225, "long", 13.0, -0.29),
    ("KNC",   "2026-05-02 04:27:32", 0.1785, 0.1673, 0.2231, "long", 13.0, -0.58),
    ("KNC",   "2026-05-02 11:00:39", 0.1616, 0.1570, 0.2020, "long", 13.0, -0.33),
    ("KNC",   "2026-05-02 16:01:31", 0.1745, 0.1657, 0.2181, "long", 13.0, -0.58),
    ("KNC",   "2026-05-02 21:45:07", 0.1702, 0.1659, 0.2127, "long", 13.0, -0.33),
]

FAST_CUT_PCT = -0.02
FAST_CUT_MIN_BARS = 2
MAX_HOLD_BARS = 48


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list:
    sym = f"{symbol.upper()}USDT"
    url = "https://fapi.binance.com/fapi/v1/klines"
    r = requests.get(url, params={"symbol": sym, "interval": "1h",
                                   "startTime": start_ms, "endTime": end_ms,
                                   "limit": 100}, timeout=10)
    if r.status_code != 200:
        return []
    return [{"open_time": k[0], "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4])} for k in r.json()]


def simulate(klines, entry_price, side, stop_pct, target_pct,
             apply_fast_cut: bool):
    """Returns (exit_price, reason). Mirrors live_replay._simulate_exit
    minus the trail-tier (we want pure fast_cut effect)."""
    hard_stop = entry_price * (1 - stop_pct) if side == "long" else entry_price * (1 + stop_pct)
    target = entry_price * (1 + target_pct) if side == "long" else entry_price * (1 - target_pct)
    underwater_streak = 0
    for i, k in enumerate(klines[:MAX_HOLD_BARS]):
        # stop
        if side == "long" and k["low"] <= hard_stop:
            return hard_stop, "stop"
        # target
        if side == "long" and k["high"] >= target:
            return target, "target"
        # fast_cut on bar close
        if apply_fast_cut:
            underwater = k["close"] <= entry_price * (1 + FAST_CUT_PCT)
            underwater_streak = underwater_streak + 1 if underwater else 0
            if i + 1 >= FAST_CUT_MIN_BARS and underwater_streak >= FAST_CUT_MIN_BARS:
                return k["close"], "fast_cut"
    final = klines[min(len(klines)-1, MAX_HOLD_BARS-1)]
    return final["close"], "max_hold"


def main():
    from datetime import datetime, timezone
    print(f"{'SYM':<6} {'ENTRY':>8} {'WITH_FC':<25} {'NO_FC':<25} {'DELTA_$':>8}")
    print("-" * 80)
    sum_with = sum_no = 0.0
    for sym, ts_iso, entry, stop, target, side, size, actual_pnl in EVENTS:
        dt = datetime.strptime(ts_iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        start_ms = int(dt.timestamp() * 1000)
        end_ms = start_ms + 49 * 3600 * 1000
        klines = fetch_klines(sym, start_ms, end_ms)
        if not klines:
            print(f"{sym:<6} {entry:>8.4f} NO KLINES")
            continue
        stop_pct = (entry - stop) / entry if side == "long" else (stop - entry) / entry
        target_pct = (target - entry) / entry if side == "long" else (entry - target) / entry
        # WITH fast_cut
        ex_w, rs_w = simulate(klines, entry, side, stop_pct, target_pct, True)
        pnl_w = (ex_w - entry) / entry * size if side == "long" else (entry - ex_w) / entry * size
        # WITHOUT fast_cut
        ex_n, rs_n = simulate(klines, entry, side, stop_pct, target_pct, False)
        pnl_n = (ex_n - entry) / entry * size if side == "long" else (entry - ex_n) / entry * size
        delta = pnl_n - pnl_w
        sum_with += pnl_w
        sum_no += pnl_n
        print(f"{sym:<6} {entry:>8.4f} {rs_w}@${ex_w:.4f}=${pnl_w:+.2f}    {rs_n}@${ex_n:.4f}=${pnl_n:+.2f}    {delta:+.2f}")
        time.sleep(0.05)
    print("-" * 80)
    print(f"TOTAL: WITH fast_cut=${sum_with:+.2f}  WITHOUT fast_cut=${sum_no:+.2f}  delta=${sum_no-sum_with:+.2f}")
    print(f"  (positive delta → disabling fast_cut would have helped)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
