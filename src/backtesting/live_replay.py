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

from src.backtesting.data_loader import load_klines, load_futures_klines
from src.backtesting.funding_loader import load_funding_rates
from src.backtesting.oi_loader import load_open_interest
from src.backtesting.replay_filters import (
    REPLAYABLE as REPLAYABLE_FILTERS,
    SKIPPED as SKIPPED_FILTERS,
    run_offline_filters,
)
from src.backtesting.top_movers_loader import (
    reconstruct as reconstruct_top_movers,
    accel_events_from_15m,
)
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

    def by_strategy(self) -> dict[str, dict]:
        """Aggregate PnL stats by strategy type for visibility into which
        signal sources drive results. Shows whether funding_squeeze, large_move,
        major_pump, etc. each carry their weight."""
        groups: dict[str, list[SimTrade]] = {}
        for t in self.trades:
            groups.setdefault(t.strategy or "unknown", []).append(t)
        out: dict[str, dict] = {}
        for k, ts in groups.items():
            n = len(ts)
            wins = sum(1 for t in ts if t.pnl_usd > 0)
            tot = sum(t.pnl_usd for t in ts)
            avg_pct = sum(t.pnl_pct for t in ts) / n if n else 0.0
            out[k] = {
                "num_trades": n,
                "win_rate": (wins / n * 100.0) if n else 0.0,
                "total_pnl_usd": tot,
                "avg_trade_pnl_pct": avg_pct,
            }
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trades"] = [asdict(t) for t in self.trades]
        d["by_strategy"] = self.by_strategy()
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
    apply_filters: bool = True,
    include_top_movers: bool = True,
    include_15m_accel: bool = False,
) -> BacktestResult:
    """Run the live RuleBrain over historical funding events, simulate fills.

    Loads funding-rate history + 1h klines per symbol, synthesizes
    `funding_squeeze` SignalPackets at each funding event (every 8h on Binance),
    feeds them through RuleBrain.tick(), runs the replayable subset of the
    prod filter chain (time_of_day, correlation, volatility, oi_delta), and
    simulates the resulting trades against the kline series.

    `apply_filters=False` disables the offline filter chain — useful for
    measuring brain-only edge in isolation.
    """
    notes: list[str] = []
    if apply_filters:
        notes.append(f"replayed filters: {','.join(REPLAYABLE_FILTERS)}")
        notes.append(f"SKIPPED filters (no historical analogue): {','.join(SKIPPED_FILTERS)}")
    else:
        notes.append("entry_filters chain DISABLED (apply_filters=False)")
    if include_top_movers:
        notes.append("top-movers events reconstructed from klines (large_move/major_pump)")
    else:
        notes.append("top-movers events DISABLED")
    notes.append("listing/fgi/trending signals NOT replayed (no historical loaders yet)")
    notes.append(f"taker fee per side: {TAKER_FEE_PER_SIDE*100:.3f}%")
    notes.append(f"exits: stop|target|max_hold {MAX_HOLD_HOURS}h (no fast-cut/trail)")

    brain = RuleBrain(balance=initial_balance)
    balance = initial_balance
    peak_balance = initial_balance
    max_dd_pct = 0.0
    trades: list[SimTrade] = []
    # Note: existing klines_by_symbol is loaded via load_klines() which hits
    # the SPOT API. We now also load futures klines so basis_check can
    # compare perp vs spot. (Entry/exit fills still simulate against
    # klines_by_symbol = spot — small basis-noise vs prod, but baseline.)
    klines_by_symbol: dict[str, list[dict]] = {}
    klines_15m_by_symbol: dict[str, list[dict]] = {}
    futures_klines_by_symbol: dict[str, list[dict]] = {}
    funding_by_symbol: dict[str, list[dict]] = {}
    oi_by_symbol: dict[str, list[dict]] = {}

    oi_missing_count = 0
    need_15m = apply_filters or include_top_movers
    for sym in symbols:
        klines_by_symbol[sym] = load_klines(sym, KLINE_INTERVAL, start_ms, end_ms)
        funding_by_symbol[sym] = load_funding_rates(sym, start_ms, end_ms)
        if need_15m:
            try:
                klines_15m_by_symbol[sym] = load_klines(sym, "15m", start_ms, end_ms)
            except Exception as e:
                notes.append(f"15m klines unavailable for {sym}: {e}")
                klines_15m_by_symbol[sym] = []
        if apply_filters:
            try:
                futures_klines_by_symbol[sym] = load_futures_klines(sym, KLINE_INTERVAL, start_ms, end_ms)
            except Exception as e:
                notes.append(f"futures klines unavailable for {sym}: {e}")
                futures_klines_by_symbol[sym] = []
            try:
                oi_by_symbol[sym] = load_open_interest(sym, start_ms, end_ms)
                if not oi_by_symbol[sym]:
                    oi_missing_count += 1
            except Exception as e:
                notes.append(f"oi history unavailable for {sym}: {e}")
                oi_by_symbol[sym] = []
                oi_missing_count += 1
    if apply_filters and oi_missing_count > 0:
        # Binance's openInterestHist endpoint typically retains only ~30d of
        # data. For windows older than that, this filter silently fail-opens.
        # Surface this loud-and-clear so honest gating works.
        notes.append(
            f"⚠ oi_delta fail-open for {oi_missing_count}/{len(symbols)} "
            f"symbols (Binance OI history retention limit, ~30d) — that "
            f"filter effectively bypassed in this window"
        )

    # Build a unified, time-ordered event stream.
    # Each event is (ts_ms, kind, symbol, payload) where kind is "funding"
    # or "top_mover".
    events: list[tuple[int, str, str, dict]] = []
    for sym, frs in funding_by_symbol.items():
        for fr in frs:
            events.append((
                int(fr["funding_time"]), "funding", sym,
                {"funding_rate": float(fr["funding_rate"]), "mark_price": float(fr["mark_price"])},
            ))

    if include_top_movers:
        tm_events = reconstruct_top_movers(symbols=symbols, start_ms=start_ms, end_ms=end_ms)
        for tm in tm_events:
            events.append((tm["ts_ms"], "top_mover", tm["symbol"], tm))

        if include_15m_accel:
            # Sub-hour accel detection from 15m klines. EMPIRICALLY hurts
            # PnL at all thresholds 5/8/10% (commits 1fdfe6d + this) — kept
            # behind a flag so future tuning can revisit; off by default.
            accel_event_count = 0
            for sym in symbols:
                for ev in accel_events_from_15m(sym, klines_15m_by_symbol.get(sym, [])):
                    events.append((ev["ts_ms"], "top_mover", sym, ev))
                    accel_event_count += 1
            notes.append(f"15m sliding-1h accel events added: {accel_event_count} (opt-in)")
        else:
            notes.append("15m sliding-1h accel detection DISABLED (default — adds noise, see commit log)")

    events.sort(key=lambda e: e[0])

    open_positions: list[dict] = []  # mirrors what brain expects in self.open_positions

    for ts_ms, kind, sym, payload in events:
        rate = float(payload.get("funding_rate", 0.0)) if kind == "funding" else 0.0
        mark = float(payload.get("mark_price", 0.0)) if kind == "funding" else float(payload.get("price", 0.0))
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

        if kind == "funding":
            # Only generate funding_squeeze packets when rate matters
            if abs(rate) < 0.0005:
                continue
            side_hint = "long" if rate < 0 else "short"
            packet = SignalPacket(
                signal_id=f"bt-fund-{sym}-{ts_ms}",
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
        else:  # top_mover
            tm_change = float(payload.get("change_pct", change_24h))
            tm_accel = float(payload.get("accel_1h_pct", accel))
            tm_vol = float(payload.get("volume_24h_usd", vol_24h))
            tm_type = payload.get("event_type", "large_move")
            side_hint = "long" if tm_change > 0 else "short"
            packet = SignalPacket(
                signal_id=f"bt-tm-{sym}-{ts_ms}",
                symbol=sym,
                signal_type=tm_type,
                priority=2 if abs(tm_change) > 20 else 1,
                timestamp=float(ts_ms),
                price_usd=mark,
                volume_24h=tm_vol,
                price_change_24h=tm_change,
                funding_rate=0.0,
                source="binance_movers",
                reasoning=f"hist {tm_type} {tm_change:+.1f}% / 24h vol ${tm_vol/1e6:.0f}M / accel1h {tm_accel:+.1f}%",
                suggested_side=side_hint,
                suggested_stop_pct=0.08,
                suggested_target_pct=0.15,
                data={
                    "acceleration_1h": tm_accel,
                    "change_pct": tm_change,
                    "volume_24h": tm_vol,
                    "price": mark,
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

            # Replayable filter chain (time_of_day, correlation, volatility, oi_delta)
            if apply_filters:
                check = run_offline_filters(
                    symbol=d.symbol,
                    side=d.side,
                    ts_ms=ts_ms,
                    funding_rate=rate,
                    open_position_symbols=[p["trade"].symbol for p in open_positions],
                    klines_15m=klines_15m_by_symbol.get(d.symbol, []),
                    oi_history=oi_by_symbol.get(d.symbol, []),
                    spot_klines_1h=klines_by_symbol.get(d.symbol, []),
                    futures_klines_1h=futures_klines_by_symbol.get(d.symbol, []),
                )
                if not check.allowed:
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
