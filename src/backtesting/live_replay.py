"""Honest backtest harness that replays the LIVE RuleBrain on historical data.

Unlike `engine.py` which uses a custom momentum scanner, this module feeds
real historical funding rates into `src.engine.rule_brain.RuleBrain` (the same
brain that runs in production) and simulates fills against historical klines.

EXPLICIT LIMITATIONS (declared so honest gating works):
  * Only `funding_squeeze` signals are replayed in this first iteration.
    `listing_pump` / `fgi_contrarian` / `trending_breakout` need separate
    historical sources (CoinGecko, alt.me) and are NOT simulated here.
  * The 7-stage `entry_filters.run_filters` chain is BYPASSED. Several of
    those filters (oi_delta, basis, volatility, top_trader_crowding,
    cvd_flow, liquidation_cascade) hit live Binance endpoints with no
    historical analogue and would silently return PASS-on-error during a
    backtest. Until each filter has an offline replay, including them
    would inflate results dishonestly. Treat the numbers from this harness
    as the BRAIN's edge, not the full chain's edge.
  * Funding-rate-aware sizing multiplier from executor is approximated with
    the same 1.25/0.5/1.0 tiers but applied here to size_usd directly.
  * No partial fills, no slippage beyond a flat taker fee per side.
  * Exits: stop_pct, target_pct, or max-hold (default 24h). Fast-cut and
    progressive trail tiers from the production executor are NOT applied
    here — adding them is the next iteration.

Outputs metrics:
  num_trades, win_rate, total_pnl_usd, total_pnl_pct, max_dd_pct,
  avg_trade_pnl_pct, sharpe_proxy, fees_paid_usd
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from src.backtesting.data_loader import load_klines
from src.backtesting.funding_loader import load_funding_rates
from src.engine.rule_brain import RuleBrain
from src.engine.signal_detector import SignalPacket


TAKER_FEE_PER_SIDE = 0.0004  # Binance Futures taker fee (0.04%)
MAX_HOLD_HOURS = 24
KLINE_INTERVAL = "1h"


@dataclass
class SimTrade:
    symbol: str
    side: str
    strategy: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: str
    fees_usd: float
    score: int
    funding_rate: float


@dataclass
class BacktestResult:
    start_ms: int
    end_ms: int
    symbols: list[str]
    initial_balance: float
    final_balance: float
    num_trades: int
    win_rate: float
    total_pnl_usd: float
    total_pnl_pct: float
    max_dd_pct: float
    avg_trade_pnl_pct: float
    sharpe_proxy: float
    fees_paid_usd: float
    trades: list[SimTrade] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trades"] = [asdict(t) for t in self.trades]
        return d


def _kline_at(klines: list[dict], ts_ms: int) -> Optional[dict]:
    """Return the kline whose open_time covers ts_ms (binary search)."""
    if not klines:
        return None
    lo, hi = 0, len(klines) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        k = klines[mid]
        if k["open_time"] <= ts_ms <= k["close_time"]:
            return k
        if ts_ms < k["open_time"]:
            hi = mid - 1
        else:
            lo = mid + 1
    return None


def _kline_index_after(klines: list[dict], ts_ms: int) -> Optional[int]:
    """Return index of first kline with open_time > ts_ms."""
    for i, k in enumerate(klines):
        if k["open_time"] > ts_ms:
            return i
    return None


def _price_change_24h_pct(klines: list[dict], idx: int) -> float:
    if idx <= 0:
        return 0.0
    look_back = min(24, idx)
    past_close = klines[idx - look_back]["close"]
    cur_close = klines[idx]["close"]
    if past_close <= 0:
        return 0.0
    return (cur_close - past_close) / past_close * 100.0


def _volume_24h_usd(klines: list[dict], idx: int) -> float:
    if idx <= 0:
        return 0.0
    look_back = min(24, idx)
    total = 0.0
    for k in klines[max(0, idx - look_back):idx]:
        total += float(k["close"]) * float(k["volume"])
    return total


def _accel_1h_pct(klines: list[dict], idx: int) -> float:
    if idx <= 0:
        return 0.0
    prev = klines[idx - 1]
    cur = klines[idx]
    if prev["close"] <= 0:
        return 0.0
    return (cur["close"] - prev["close"]) / prev["close"] * 100.0


def _funding_size_multiplier(rate: float) -> float:
    if abs(rate) > 0.001:  # 0.1%
        return 1.25
    if abs(rate) < 0.0002:  # 0.02%
        return 0.5
    return 1.0


def _simulate_exit(
    klines: list[dict],
    entry_idx: int,
    entry_price: float,
    side: str,
    stop_pct: float,
    target_pct: float,
) -> tuple[int, float, str]:
    """Walk forward from entry_idx; return (exit_idx, exit_price, reason)."""
    max_idx = min(entry_idx + MAX_HOLD_HOURS, len(klines) - 1)
    for i in range(entry_idx, max_idx + 1):
        k = klines[i]
        high, low = float(k["high"]), float(k["low"])
        if side == "long":
            if low <= entry_price * (1 - stop_pct):
                return i, entry_price * (1 - stop_pct), "stop"
            if high >= entry_price * (1 + target_pct):
                return i, entry_price * (1 + target_pct), "target"
        else:  # short
            if high >= entry_price * (1 + stop_pct):
                return i, entry_price * (1 + stop_pct), "stop"
            if low <= entry_price * (1 - target_pct):
                return i, entry_price * (1 - target_pct), "target"
    final = klines[max_idx]
    return max_idx, float(final["close"]), "max_hold"


def replay(
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    initial_balance: float = 1000.0,
) -> BacktestResult:
    """Run the live RuleBrain over historical funding events, simulate fills.

    Loads funding-rate history + 1h klines per symbol, synthesizes
    `funding_squeeze` SignalPackets at each funding event (every 8h on Binance),
    feeds them through RuleBrain.tick(), and simulates the resulting trades
    against the kline series.
    """
    notes: list[str] = []
    notes.append("entry_filters chain BYPASSED (no historical replay yet)")
    notes.append("only funding_squeeze signals replayed (no listing/fgi/trending)")
    notes.append(f"taker fee per side: {TAKER_FEE_PER_SIDE*100:.3f}%")
    notes.append(f"exits: stop|target|max_hold {MAX_HOLD_HOURS}h (no fast-cut/trail)")

    brain = RuleBrain(balance=initial_balance)
    balance = initial_balance
    peak_balance = initial_balance
    max_dd_pct = 0.0
    trades: list[SimTrade] = []
    klines_by_symbol: dict[str, list[dict]] = {}
    funding_by_symbol: dict[str, list[dict]] = {}

    for sym in symbols:
        klines_by_symbol[sym] = load_klines(sym, KLINE_INTERVAL, start_ms, end_ms)
        funding_by_symbol[sym] = load_funding_rates(sym, start_ms, end_ms)

    # Build a unified, time-ordered event stream of (ts_ms, symbol, rate, mark)
    events: list[tuple[int, str, float, float]] = []
    for sym, frs in funding_by_symbol.items():
        for fr in frs:
            events.append((int(fr["funding_time"]), sym, float(fr["funding_rate"]), float(fr["mark_price"])))
    events.sort(key=lambda e: e[0])

    open_positions: list[dict] = []  # mirrors what brain expects in self.open_positions

    for ts_ms, sym, rate, mark in events:
        # Close any expired sim positions before this event (timeline-correct)
        still_open = []
        for pos in open_positions:
            if pos["exit_time_ms"] <= ts_ms:
                trades.append(pos["trade"])
                balance += pos["trade"].pnl_usd
                peak_balance = max(peak_balance, balance)
                if peak_balance > 0:
                    dd = (peak_balance - balance) / peak_balance * 100.0
                    max_dd_pct = max(max_dd_pct, dd)
                # Tell brain about the close so re-entry cooldown applies
                brain.review_trade({
                    "symbol": pos["trade"].symbol,
                    "pnl_pct": pos["trade"].pnl_pct,
                    "exit": pos["trade"].exit_price,
                    "exit_price": pos["trade"].exit_price,
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        klines = klines_by_symbol.get(sym, [])
        if not klines:
            continue
        idx = _kline_index_after(klines, ts_ms)
        if idx is None or idx >= len(klines):
            continue

        # Synthesize signal context from klines
        change_24h = _price_change_24h_pct(klines, idx)
        vol_24h = _volume_24h_usd(klines, idx)
        accel = _accel_1h_pct(klines, idx)

        # Only generate funding_squeeze packets when rate matters
        if abs(rate) < 0.0005:
            continue

        side_hint = "long" if rate < 0 else "short"
        packet = SignalPacket(
            signal_id=f"bt-{sym}-{ts_ms}",
            symbol=sym,
            signal_type="funding_squeeze",
            priority=2,
            timestamp=float(ts_ms),
            price_usd=mark,
            volume_24h=vol_24h,
            price_change_24h=change_24h,
            funding_rate=rate,
            source="binance_funding",
            reasoning=f"hist funding {rate*100:+.3f}%",
            suggested_side=side_hint,
            suggested_stop_pct=0.06,
            suggested_target_pct=0.08,
            data={
                "acceleration_1h": accel,
                "funding_rate": rate,
                "mark_price": mark,
            },
        )

        # Refresh brain state and tick
        brain.balance = balance
        brain.funding_rates = {sym: rate}
        brain.open_positions = [
            {"symbol": p["trade"].symbol, "size_usd": p["trade"].size_usd}
            for p in open_positions
        ]
        brain.add_signal(packet)
        decisions = brain.tick()

        for d in decisions:
            if d.action != "BUY":
                continue
            d_klines = klines_by_symbol.get(d.symbol)
            if not d_klines:
                continue
            entry_idx = _kline_index_after(d_klines, ts_ms)
            if entry_idx is None or entry_idx >= len(d_klines):
                continue
            entry_price = float(d_klines[entry_idx]["open"])
            if entry_price <= 0:
                continue

            # Apply funding-rate-aware sizing (live executor mirror)
            size_usd = d.size_usd * _funding_size_multiplier(rate)
            if size_usd > balance * 0.4:
                size_usd = balance * 0.4
            if size_usd < 5:
                continue

            exit_idx, exit_price, reason = _simulate_exit(
                d_klines, entry_idx, entry_price, d.side, d.stop_pct, d.target_pct
            )
            qty = size_usd / entry_price
            gross_pnl = (exit_price - entry_price) * qty if d.side == "long" else (entry_price - exit_price) * qty
            fees = (entry_price * qty * TAKER_FEE_PER_SIDE) + (exit_price * qty * TAKER_FEE_PER_SIDE)
            net_pnl = gross_pnl - fees
            pnl_pct = net_pnl / size_usd * 100.0

            trade = SimTrade(
                symbol=d.symbol,
                side=d.side,
                strategy=getattr(d, "strategy_type", "funding_squeeze") or "funding_squeeze",
                entry_time_ms=int(d_klines[entry_idx]["open_time"]),
                exit_time_ms=int(d_klines[exit_idx]["close_time"]),
                entry_price=entry_price,
                exit_price=exit_price,
                size_usd=size_usd,
                pnl_usd=net_pnl,
                pnl_pct=pnl_pct,
                exit_reason=reason,
                fees_usd=fees,
                score=0,  # rule_brain doesn't expose score on TradeDecision
                funding_rate=rate,
            )
            open_positions.append({
                "trade": trade,
                "exit_time_ms": trade.exit_time_ms,
            })

    # Drain any remaining positions
    for pos in open_positions:
        trades.append(pos["trade"])
        balance += pos["trade"].pnl_usd
        peak_balance = max(peak_balance, balance)
        if peak_balance > 0:
            dd = (peak_balance - balance) / peak_balance * 100.0
            max_dd_pct = max(max_dd_pct, dd)

    num_trades = len(trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    win_rate = (wins / num_trades * 100.0) if num_trades else 0.0
    total_pnl = sum(t.pnl_usd for t in trades)
    total_pnl_pct = (total_pnl / initial_balance * 100.0) if initial_balance else 0.0
    avg_pnl_pct = (sum(t.pnl_pct for t in trades) / num_trades) if num_trades else 0.0
    fees_paid = sum(t.fees_usd for t in trades)

    # Crude Sharpe proxy: mean / stdev of trade-level pct returns (no annualisation)
    sharpe_proxy = 0.0
    if num_trades > 1:
        mean_r = avg_pnl_pct
        var = sum((t.pnl_pct - mean_r) ** 2 for t in trades) / (num_trades - 1)
        std = var ** 0.5
        if std > 0:
            sharpe_proxy = mean_r / std

    return BacktestResult(
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=symbols,
        initial_balance=initial_balance,
        final_balance=balance,
        num_trades=num_trades,
        win_rate=win_rate,
        total_pnl_usd=total_pnl,
        total_pnl_pct=total_pnl_pct,
        max_dd_pct=max_dd_pct,
        avg_trade_pnl_pct=avg_pnl_pct,
        sharpe_proxy=sharpe_proxy,
        fees_paid_usd=fees_paid,
        trades=trades,
        notes=notes,
    )
