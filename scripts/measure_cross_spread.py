"""Cross-exchange funding spread reconnaissance.

Fetches current funding rate per venue (Binance, Bybit, OKX, Hyperliquid)
for a fixed symbol set, normalises to per-8h, prints spread report.

Run: python3 scripts/measure_cross_spread.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.cross_funding_loader import (  # noqa: E402
    fetch_cross_funding,
    find_spread_events,
)

SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "SUI", "WIF", "PEPE", "ADA"]
MIN_SPREAD = 0.0005  # 5 bps per 8h


def _bps(x: float) -> str:
    return f"{x * 10_000:+.2f}bps"


def main() -> int:
    print(f"\nCross-exchange funding snapshot ({len(SYMBOLS)} symbols, all rates per-8h)\n")

    rows: list[dict] = []
    for sym in SYMBOLS:
        rates = fetch_cross_funding(sym)
        if not rates:
            print(f"  {sym:<6} NO DATA")
            continue
        hi_v, hi = max(rates.items(), key=lambda kv: kv[1])
        lo_v, lo = min(rates.items(), key=lambda kv: kv[1])
        spread = hi - lo
        rows.append({
            "symbol": sym,
            "rates": rates,
            "spread": spread,
            "long_venue": lo_v,
            "short_venue": hi_v,
        })

    rows.sort(key=lambda r: r["spread"], reverse=True)

    header = f"{'SYM':<6} {'binance':>12} {'bybit':>12} {'okx':>12} {'hyperliq':>12}   {'SPREAD':>10}  {'LONG':>10} -> {'SHORT':<10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        rates = r["rates"]
        cells = []
        for venue in ("binance", "bybit", "okx", "hyperliquid"):
            cells.append(_bps(rates[venue]) if venue in rates else "       n/a")
        print(
            f"{r['symbol']:<6} {cells[0]:>12} {cells[1]:>12} {cells[2]:>12} {cells[3]:>12}   "
            f"{_bps(r['spread']):>10}  {r['long_venue']:>10} -> {r['short_venue']:<10}"
        )

    over = [r for r in rows if r["spread"] >= MIN_SPREAD]
    print()
    print(f"Symbols with spread > {MIN_SPREAD * 10_000:.0f}bps/8h:  {len(over)} / {len(rows)}")
    if rows:
        top = rows[0]
        print(f"Max spread:                       {_bps(top['spread'])}  ({top['symbol']}: {top['long_venue']} -> {top['short_venue']})")

    print("\nTop 10 widest current spreads:")
    for r in rows[:10]:
        print(f"  {r['symbol']:<6} {_bps(r['spread']):>10}   {r['long_venue']} -> {r['short_venue']}")

    # Also exercise find_spread_events for parity / sanity check
    events = find_spread_events(SYMBOLS, MIN_SPREAD)
    print(f"\nfind_spread_events() returned {len(events)} events at >= {MIN_SPREAD * 10_000:.0f}bps/8h.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
