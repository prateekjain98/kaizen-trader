"""Cross-exchange funding spread reconnaissance: prints per-venue rates
(per-8h) and ranks symbols by absolute spread."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.cross_funding_loader import (  # noqa: E402
    fetch_cross_funding, find_spread_events,
)

SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "SUI", "WIF", "PEPE", "ADA"]
MIN_SPREAD = 0.0005  # 5 bps per 8h
VENUES = ("binance", "bybit", "okx", "hyperliquid")


def _bps(x: float) -> str:
    return f"{x * 10_000:+.2f}bps"


def main() -> int:
    print(f"\nCross-exchange funding snapshot ({len(SYMBOLS)} symbols, per-8h)\n")
    rows: list[dict] = []
    for sym in SYMBOLS:
        rates = fetch_cross_funding(sym)
        if not rates:
            print(f"  {sym:<6} NO DATA")
            continue
        hi_v, hi = max(rates.items(), key=lambda kv: kv[1])
        lo_v, lo = min(rates.items(), key=lambda kv: kv[1])
        rows.append({"symbol": sym, "rates": rates, "spread": hi - lo,
                     "long_venue": lo_v, "short_venue": hi_v})
    rows.sort(key=lambda r: r["spread"], reverse=True)

    hdr = f"{'SYM':<6} {'binance':>11} {'bybit':>11} {'okx':>11} {'hyperliq':>11}   {'SPREAD':>10}  LONG -> SHORT"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        cells = [_bps(r["rates"][v]) if v in r["rates"] else "      n/a" for v in VENUES]
        print(f"{r['symbol']:<6} {cells[0]:>11} {cells[1]:>11} {cells[2]:>11} {cells[3]:>11}   "
              f"{_bps(r['spread']):>10}  {r['long_venue']} -> {r['short_venue']}")

    over = [r for r in rows if r["spread"] >= MIN_SPREAD]
    print(f"\nSymbols with spread >= {MIN_SPREAD * 10_000:.0f}bps/8h: {len(over)} / {len(rows)}")
    if rows:
        top = rows[0]
        print(f"Max spread: {_bps(top['spread'])} ({top['symbol']}: {top['long_venue']} -> {top['short_venue']})")
    print("\nTop 10 widest current spreads:")
    for r in rows[:10]:
        print(f"  {r['symbol']:<6} {_bps(r['spread']):>10}   {r['long_venue']} -> {r['short_venue']}")
    if not rows:  # only re-hit APIs if main pass returned nothing
        print(f"\nfind_spread_events: {len(find_spread_events(SYMBOLS, MIN_SPREAD))} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
