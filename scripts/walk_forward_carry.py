"""Walk-forward validation for the funding_carry strategy.

60d in-sample / 30d out-of-sample, step 30d. Over a 180d range that
yields 4 folds. IS window only confirms carry events exist (no
hyperparameter tuning); OOS runs live_replay blind and records PnL.

Aggregate OOS PnL + OOS Sharpe are the only honest numbers. Carry
literature claims OOS Sharpe ~0.5-1.5; <0 is fabrication.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.funding_carry_loader import reconstruct_funding_carry
from src.backtesting.live_replay import replay


_DAY_MS = 86_400_000

DEFAULT_UNIVERSE = [
    "BTC", "ETH", "DOGE", "LDO", "OP", "ARB", "INJ", "SUI", "JUP", "WIF",
    "PEPE", "BONK", "ORDI", "TIA", "SEI", "APT", "FIL", "ATOM", "NEAR",
    "ALGO", "STX", "BLUR",
]
REDUCED_UNIVERSE = [
    "BTC", "ETH", "DOGE", "OP", "ARB", "INJ", "SUI", "TIA", "SEI", "APT",
    "NEAR", "ATOM",
]


@dataclass
class FoldResult:
    fold_idx: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    is_carry_events: int
    oos_trades: int
    oos_pnl_usd: float
    oos_win_rate: float
    oos_carry_pnl_usd: float
    oos_carry_trades: int
    elapsed_s: float


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _to_ms(s: str) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _fold(idx: int, is_s: int, is_e: int, oos_s: int, oos_e: int,
          symbols: list[str], balance: float) -> FoldResult:
    t0 = time.time()
    try:
        is_events = reconstruct_funding_carry(symbols, is_s, is_e)
    except Exception as e:
        print(f"  [fold {idx}] IS reconstruction failed: {e}")
        is_events = []

    # Isolate carry: disable other event sources so OOS PnL is
    # carry-attributable. Filters/regime/slippage stay on for realism.
    res = replay(
        symbols=symbols, start_ms=oos_s, end_ms=oos_e,
        initial_balance=balance,
        include_top_movers=False, include_15m_accel=False,
        include_fgi_contrarian=False, include_listing_pump=False,
        include_stable_flow=False, include_funding_carry=True,
    )
    bs = res.by_strategy()
    carry_keys = [k for k in bs if "funding_carry" in k]
    carry_pnl = sum(bs[k]["total_pnl_usd"] for k in carry_keys)
    carry_trades = sum(bs[k]["num_trades"] for k in carry_keys)

    return FoldResult(
        fold_idx=idx,
        is_start=_date(is_s), is_end=_date(is_e),
        oos_start=_date(oos_s), oos_end=_date(oos_e),
        is_carry_events=len(is_events),
        oos_trades=res.num_trades,
        oos_pnl_usd=res.total_pnl_usd,
        oos_win_rate=res.win_rate,
        oos_carry_pnl_usd=carry_pnl,
        oos_carry_trades=carry_trades,
        elapsed_s=time.time() - t0,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-11-03")
    ap.add_argument("--end", default="2026-05-02")
    ap.add_argument("--train-days", type=int, default=60)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--symbols", default=",".join(DEFAULT_UNIVERSE))
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--reduced", action="store_true")
    args = ap.parse_args()

    symbols = REDUCED_UNIVERSE if args.reduced else [
        s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms = _to_ms(args.start)
    end_ms = _to_ms(args.end)
    train_ms = args.train_days * _DAY_MS
    test_ms = args.test_days * _DAY_MS

    folds = []
    cursor = start_ms
    while cursor + train_ms + test_ms <= end_ms:
        folds.append((cursor, cursor + train_ms,
                      cursor + train_ms, cursor + train_ms + test_ms))
        cursor += test_ms

    print(f"Walk-forward funding_carry: {args.train_days}d IS / {args.test_days}d OOS, "
          f"{len(folds)} folds, {len(symbols)} symbols")
    print(f"  Range: {args.start} → {args.end}")
    print(f"  Universe: {','.join(symbols)}")
    print(f"\n{'F':>2} {'IS period':>23} {'OOS period':>23} "
          f"{'ISevt':>5} {'OOSn':>4} {'OOSPnL':>9} {'WR%':>5} "
          f"{'CarryPnL':>9} {'Cn':>3} {'sec':>4}")

    results = []
    for i, (a, b, c, d) in enumerate(folds, start=1):
        fr = _fold(i, a, b, c, d, symbols, args.balance)
        results.append(fr)
        print(f"{fr.fold_idx:>2d} "
              f"{fr.is_start}→{fr.is_end} "
              f"{fr.oos_start}→{fr.oos_end} "
              f"{fr.is_carry_events:>5d} {fr.oos_trades:>4d} "
              f"${fr.oos_pnl_usd:>+8.2f} {fr.oos_win_rate:>5.1f} "
              f"${fr.oos_carry_pnl_usd:>+8.2f} {fr.oos_carry_trades:>3d} "
              f"{fr.elapsed_s:>4.0f}")

    n = len(results)
    tot_trades = sum(r.oos_trades for r in results)
    tot_pnl = sum(r.oos_pnl_usd for r in results)
    carry_pnl = sum(r.oos_carry_pnl_usd for r in results)
    carry_trades = sum(r.oos_carry_trades for r in results)
    pos_folds = sum(1 for r in results if r.oos_carry_pnl_usd > 0)

    rets = [r.oos_pnl_usd / args.balance for r in results]
    if n >= 2:
        m = sum(rets) / n
        v = sum((x - m) ** 2 for x in rets) / (n - 1)
        s = v ** 0.5
        oos_sharpe = (m / s) * (12 ** 0.5) if s > 0 else 0.0
    else:
        oos_sharpe = 0.0

    crets = [r.oos_carry_pnl_usd / args.balance for r in results]
    if n >= 2:
        m = sum(crets) / n
        s = (sum((x - m) ** 2 for x in crets) / (n - 1)) ** 0.5
        carry_sharpe = (m / s) * (12 ** 0.5) if s > 0 else 0.0
    else:
        carry_sharpe = 0.0

    print("\n" + "=" * 70)
    print(f"AGGREGATE OOS ({n} folds)")
    print(f"  Total OOS trades:        {tot_trades}")
    print(f"  Total OOS PnL:           ${tot_pnl:+.2f}")
    print(f"  Carry-attributable PnL:  ${carry_pnl:+.2f} ({carry_trades} trades)")
    print(f"  Profitable folds (carry):{pos_folds}/{n}")
    print(f"  OOS Sharpe (all):        {oos_sharpe:+.2f}")
    print(f"  OOS Sharpe (carry-only): {carry_sharpe:+.2f}")
    print("=" * 70)

    ts = int(time.time())
    out = Path("data") / f"walk_forward_carry_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {"start": args.start, "end": args.end,
                   "train_days": args.train_days, "test_days": args.test_days,
                   "symbols": symbols, "balance": args.balance},
        "folds": [asdict(r) for r in results],
        "aggregate": {
            "n_folds": n, "total_oos_trades": tot_trades,
            "total_oos_pnl_usd": tot_pnl, "carry_pnl_usd": carry_pnl,
            "carry_trades": carry_trades, "profitable_folds": pos_folds,
            "oos_sharpe_all": oos_sharpe, "oos_sharpe_carry": carry_sharpe,
        },
    }, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
