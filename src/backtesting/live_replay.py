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
from src.backtesting.fgi_loader import load_fear_greed_index, get_fgi_at_timestamp
from src.backtesting.stablecoin_loader import (
    load_stablecoin_history,
    get_stablecoin_flow_at_timestamp,
)
from src.backtesting.funding_loader import load_funding_rates
from src.backtesting.funding_carry_loader import reconstruct_funding_carry
from src.backtesting.listing_loader import load_exchange_listings
from src.backtesting.oi_loader import load_open_interest
from src.backtesting.top_ls_loader import load_top_ls_ratio
from src.backtesting.replay_filters import (
    REPLAYABLE as REPLAYABLE_FILTERS,
    SKIPPED as SKIPPED_FILTERS,
    run_offline_filters,
)
from src.backtesting.regime_detector import regime_at_timestamp
from src.backtesting.slippage_model import slippage_bps as _slippage_bps
from src.backtesting.top_movers_loader import (
    reconstruct as reconstruct_top_movers,
    accel_events_from_15m,
)
from src.engine.rule_brain import RuleBrain
from src.engine.signal_detector import SignalPacket
from src.engine.executor import Executor as _ProdExecutor


TAKER_FEE_PER_SIDE = 0.0004  # Binance Futures taker fee (0.04%)
# Mirror prod exit logic. Read constants directly off the prod Executor so any
# tuning over there propagates into the backtest without drift.
MAX_HOLD_HOURS = 48  # prod update_price uses pos.hold_hours > 48
TRAIL_TIERS = _ProdExecutor.TRAIL_TIERS  # ((5.0, 0.25), (3.0, 0.5), (1.5, 1.0))
FAST_CUT_PNL_THRESHOLD = -0.02  # mirrors _fast_cut_allowed: pnl_pct <= -2%
# Prod uses 30min hold + velocity_5min<=0. 1h bar granularity here, so we
# require >=2 bars (~2h hold) AND two consecutive closes <= -2% before firing.
# Single-bar fires whipsaw-cut every recovery (0% WR seen in prior attempt).
FAST_CUT_MIN_BARS = 2
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
    slippage_usd: float = 0.0


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
    total_slippage_usd: float = 0.0
    trades: list[SimTrade] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def exit_reason_histogram(self) -> dict[str, int]:
        """Count trades by exit_reason. Mirrors prod executor exit reasons:
        stop / trail / target / fast_cut / max_hold."""
        h: dict[str, int] = {}
        for t in self.trades:
            h[t.exit_reason] = h.get(t.exit_reason, 0) + 1
        return h

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
        d["exit_reasons"] = self.exit_reason_histogram()
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


def _funding_size_multiplier(rate: float, side: str = "long") -> float:
    """Direction-aware funding sizing — mirrors prod
    `Executor._funding_size_multiplier` exactly (audit fix C1).

      long  + neg funding → receive → BOOST 1.25×
      long  + pos funding → pay → PENALTY 0.5×
      short + pos funding → receive → BOOST
      short + neg funding → pay → PENALTY
      |rate| < 0.1% → 1.0× (noise band)

    Previous direction-blind version (1.25 on |rate|>0.1% regardless of side)
    systematically OVER-sized every extreme-funding trade — backtest PnL on
    the entire funding_squeeze family was inflated whenever rate sign was
    against the trade.
    """
    if abs(rate) < _ProdExecutor._FUNDING_FAVORABLE_THRESHOLD:
        return 1.0
    if side == "long":
        return _ProdExecutor._FUNDING_BOOST_MULT if rate < 0 else _ProdExecutor._FUNDING_PENALTY_MULT
    return _ProdExecutor._FUNDING_BOOST_MULT if rate > 0 else _ProdExecutor._FUNDING_PENALTY_MULT


def _trail_factor(profit_pct: float, stop_pct: float):
    """Mirror Executor._trail_factor: tightest matching tier wins."""
    for activation_mult, factor in TRAIL_TIERS:
        if profit_pct >= activation_mult * stop_pct:
            return factor
    return None


def _simulate_exit(
    klines: list[dict],
    entry_idx: int,
    entry_price: float,
    side: str,
    stop_pct: float,
    target_pct: float,
    symbol: str = "",
) -> tuple[int, float, str]:
    """Walk forward bar-by-bar mirroring prod executor.update_price.

    Applies progressive trailing stop tiers, hard target, fast-cut, and
    48h max hold. Returns (exit_idx, exit_price, reason in
    {stop|trail|target|fast_cut|max_hold}).

    The trail tier evaluation uses the bar's HIGH (long) / LOW (short) as
    the candidate price each bar — the most aggressive trail the bar would
    have produced under tick-level prod logic. Stop trigger then uses the
    bar's LOW (long) / HIGH (short) against the resulting effective stop.
    Target wins ties on the same bar (matches prod ordering: stop check
    fires first against the live trailing price as price walks up).
    """
    n = len(klines)
    max_idx = min(entry_idx + MAX_HOLD_HOURS, n - 1)

    if side == "long":
        hard_stop = entry_price * (1 - stop_pct)
        trail_price = 0.0  # init to 0; effective stop falls back to hard
    else:
        hard_stop = entry_price * (1 + stop_pct)
        trail_price = entry_price * (1 + stop_pct)  # init to hard for shorts

    target = entry_price * (1 + target_pct) if side == "long" else entry_price * (1 - target_pct)

    underwater_streak = 0  # consecutive bars closed <= entry*(1-2%) (long sense)
    fast_cut_exempt = symbol.upper() in {"BTC", "ETH", "SOL", "BNB", "XRP"}

    for i in range(entry_idx, max_idx + 1):
        k = klines[i]
        high, low, close = float(k["high"]), float(k["low"]), float(k["close"])
        bars_held = i - entry_idx + 1  # 1 at entry bar

        # --- Update progressive trail using bar extremum ---
        if side == "long":
            cand_price = high
            profit_pct = (cand_price - entry_price) / entry_price
            f = _trail_factor(profit_pct, stop_pct)
            if f is not None:
                new_trail = cand_price * (1 - stop_pct * f)
                if new_trail > trail_price:
                    trail_price = new_trail
            effective_stop = max(hard_stop, trail_price)
            using_trail = trail_price > hard_stop
        else:
            cand_price = low
            profit_pct = (entry_price - cand_price) / entry_price
            f = _trail_factor(profit_pct, stop_pct)
            if f is not None:
                new_trail = cand_price * (1 + stop_pct * f)
                if new_trail < trail_price:
                    trail_price = new_trail
            effective_stop = min(hard_stop, trail_price)
            using_trail = trail_price < hard_stop

        # --- Stop / trail trigger ---
        if side == "long" and low <= effective_stop:
            return i, effective_stop, ("trail" if using_trail else "stop")
        if side == "short" and high >= effective_stop:
            return i, effective_stop, ("trail" if using_trail else "stop")

        # --- Target trigger ---
        if side == "long" and high >= target:
            return i, target, "target"
        if side == "short" and low <= target:
            return i, target, "target"

        # --- Fast-cut: sustained-downtrend gate ---
        if not fast_cut_exempt:
            if side == "long":
                underwater = close <= entry_price * (1 + FAST_CUT_PNL_THRESHOLD)
            else:
                underwater = close >= entry_price * (1 - FAST_CUT_PNL_THRESHOLD)
            if underwater:
                underwater_streak += 1
            else:
                underwater_streak = 0
            if (
                bars_held >= FAST_CUT_MIN_BARS
                and underwater_streak >= FAST_CUT_MIN_BARS
            ):
                return i, close, "fast_cut"

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
    include_fgi_contrarian: bool = True,
    include_listing_pump: bool = True,
    include_stable_flow: bool = True,
    include_funding_carry: bool = True,
    apply_regime_gate: bool = True,
    apply_slippage: bool = True,
    min_score_override: Optional[int] = None,
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
    if include_fgi_contrarian:
        notes.append("fgi_contrarian events from alternative.me FGI history (BTC/ETH only)")
    else:
        notes.append("fgi_contrarian events DISABLED")
    if include_listing_pump:
        notes.append("listing_pump events from Binance Futures/Spot + Coinbase listing dates")
    else:
        notes.append("listing_pump events DISABLED")
    if include_stable_flow:
        notes.append("stable_flow events from DefiLlama stablecoin daily totals (BTC/ETH only)")
    else:
        notes.append("stable_flow events DISABLED")
    if include_funding_carry:
        notes.append("funding_carry events from cross-sectional 8h ranking (top/bot 10%)")
    else:
        notes.append("funding_carry events DISABLED")
    notes.append("listing/fgi/trending signals NOT replayed (no historical loaders yet)")
    if min_score_override is not None:
        # Monkeypatch the module attribute the brain reads so we can
        # measure what trades WOULD have been taken at a lower threshold.
        # Restored at function end.
        import src.engine.rule_brain as _rb
        _orig_min_score = _rb.MIN_SCORE_TO_TRADE
        _rb.MIN_SCORE_TO_TRADE = int(min_score_override)
        notes.append(f"MIN_SCORE_TO_TRADE override: {min_score_override} (prod is {_orig_min_score})")
    notes.append(f"taker fee per side: {TAKER_FEE_PER_SIDE*100:.3f}%")
    if apply_slippage:
        notes.append("slippage model ENABLED (sqrt market-impact, vol-aware; see slippage_model.py)")
    else:
        notes.append("slippage model DISABLED (--no-slippage; backtest will OVER-state PnL vs live)")
    notes.append(f"exits: prod-mirror trail({TRAIL_TIERS})/fast_cut(>={FAST_CUT_MIN_BARS}bar @<={FAST_CUT_PNL_THRESHOLD*100:.0f}%)/max_hold {MAX_HOLD_HOURS}h")

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
    ls_by_symbol: dict[str, list[dict]] = {}

    oi_missing_count = 0
    ls_missing_count = 0
    spot_unavailable_count = 0
    need_15m = apply_filters or include_top_movers
    for sym in symbols:
        try:
            klines_by_symbol[sym] = load_klines(sym, KLINE_INTERVAL, start_ms, end_ms)
        except Exception:
            # Some symbols (tokenised stock futures, delisted, unicode names)
            # have no spot kline data. Fall through with empty list — events
            # for that symbol can still fire but trades won't simulate.
            klines_by_symbol[sym] = []
            spot_unavailable_count += 1
        try:
            funding_by_symbol[sym] = load_funding_rates(sym, start_ms, end_ms)
        except Exception:
            funding_by_symbol[sym] = []
        if need_15m:
            try:
                klines_15m_by_symbol[sym] = load_klines(sym, "15m", start_ms, end_ms)
            except Exception:
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
            try:
                ls_by_symbol[sym] = load_top_ls_ratio(sym, start_ms, end_ms)
                if not ls_by_symbol[sym]:
                    ls_missing_count += 1
            except Exception as e:
                notes.append(f"top L/S history unavailable for {sym}: {e}")
                ls_by_symbol[sym] = []
                ls_missing_count += 1
    if spot_unavailable_count > 0:
        notes.append(f"⚠ spot klines unavailable for {spot_unavailable_count}/{len(symbols)} "
                     f"symbols (tokenised stocks, delisted, or unicode names) — "
                     f"events fire but trades skip simulation")
    if apply_filters and oi_missing_count > 0:
        # Binance's openInterestHist endpoint typically retains only ~30d of
        # data. For windows older than that, this filter silently fail-opens.
        # Surface this loud-and-clear so honest gating works.
        notes.append(
            f"⚠ oi_delta fail-open for {oi_missing_count}/{len(symbols)} "
            f"symbols (Binance OI history retention limit, ~30d) — that "
            f"filter effectively bypassed in this window"
        )
    if apply_filters and ls_missing_count > 0:
        # Same retention limit on topLongShortPositionRatio (~30d).
        notes.append(
            f"⚠ top_crowding fail-open for {ls_missing_count}/{len(symbols)} "
            f"symbols (Binance top L/S retention limit, ~30d) — that "
            f"filter effectively bypassed in this window"
        )

    # Realised-vol regime meta-gate: precompute regime per backtest tick
    # using BTC 1h klines as the market-wide proxy. Lazy-load BTC if it
    # isn't already in the universe so the gate always has data.
    btc_klines_for_regime: list[dict] = klines_by_symbol.get("BTC") or []
    if apply_regime_gate and not btc_klines_for_regime:
        try:
            btc_klines_for_regime = load_klines("BTC", KLINE_INTERVAL, start_ms, end_ms)
        except Exception as e:
            notes.append(f"BTC klines for regime gate unavailable: {e}")
            btc_klines_for_regime = []
    if apply_regime_gate:
        notes.append("regime meta-gate ENABLED (RV_7d vs 90d-median, calm<0.6 / hot>1.4)")
    else:
        notes.append("regime meta-gate DISABLED (--no-regime-gate)")

    # Counters for honest reporting
    regime_blocks = 0
    other_blocks = 0
    regime_seen: dict[str, int] = {"calm": 0, "neutral": 0, "hot": 0}

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

    listing_event_count = 0
    if include_listing_pump:
        try:
            listings = load_exchange_listings()
        except Exception as e:
            notes.append(f"listing history unavailable: {e}")
            listings = []
        symbol_set = set(symbols)
        for L in listings:
            sym = L.get("symbol", "")
            ts = int(L.get("listing_date_ms", 0))
            if sym not in symbol_set or ts < start_ms or ts > end_ms:
                continue
            events.append((ts, "listing", sym, {
                "exchange": L.get("exchange", ""),
                "listing_type": L.get("listing_type", ""),
                "listing_date_ms": ts,
            }))
            listing_event_count += 1
        notes.append(f"listing_pump events emitted: {listing_event_count}")

    fgi_event_count = 0
    if include_fgi_contrarian:
        try:
            fgi_history = load_fear_greed_index()
        except Exception as e:
            notes.append(f"fgi history unavailable: {e}")
            fgi_history = []
        # Walk daily; emit fgi_contrarian event when FGI extreme on BTC/ETH
        # (mirrors signal_detector._process_fgi: fgi <=20 → long, >=80 → short)
        if fgi_history:
            day_ms = 86_400_000
            t = (start_ms // day_ms) * day_ms
            while t <= end_ms:
                # CORRECTNESS (audit H3 / lookahead #4): alternative.me's day-N
                # FGI value is timestamped at day-N 00:00 UTC but is computed
                # from end-of-day-(N-1) data and published 00:00-01:00 UTC.
                # Using day-N's value at day-N 00:00 risks 0-60min lookahead.
                # Lag the lookup by 1 day (use yesterday's published value)
                # for an honest no-lookahead replay. FGI is a slow signal —
                # 1d lag costs effectively nothing.
                fgi = get_fgi_at_timestamp(fgi_history, t - day_ms)
                if fgi is None:
                    t += day_ms
                    continue
                if fgi <= 20 or fgi >= 80:
                    for sym in ("BTC", "ETH"):
                        if sym not in symbols:
                            continue
                        side_hint = "long" if fgi <= 20 else "short"
                        events.append((t, "fgi", sym, {
                            "fgi": fgi, "side_hint": side_hint,
                        }))
                        fgi_event_count += 1
                t += day_ms
        notes.append(f"fgi_contrarian events emitted: {fgi_event_count} (1d-lagged for no-lookahead)")

    stable_flow_event_count = 0
    if include_stable_flow:
        try:
            stable_history = load_stablecoin_history()
        except Exception as e:
            notes.append(f"stablecoin history unavailable: {e}")
            stable_history = []
        if stable_history:
            day_ms = 86_400_000
            t = (start_ms // day_ms) * day_ms
            while t <= end_ms:
                # CORRECTNESS (audit lookahead #5): DefiLlama's
                # net_24h_change_usd at day-N stamp = circulating(N) -
                # circulating(N-1). The day-N circulating snapshot is the
                # END-OF-DAY balance, only knowable AFTER day-N. Using it
                # at day-N 00:00 = up to 24h lookahead. Lag by 1 day —
                # today we trade on yesterday's closed flow, which is
                # genuinely knowable at 00:00.
                row = get_stablecoin_flow_at_timestamp(stable_history, t - day_ms)
                if row is not None:
                    net24 = row["net_24h_change_usd"]
                    if net24 > 300_000_000 or net24 < -300_000_000:
                        side_hint = "long" if net24 > 0 else "short"
                        kind = "stable_flow_bull" if net24 > 0 else "stable_flow_bear"
                        for sym in ("BTC", "ETH"):
                            if sym not in symbols:
                                continue
                            events.append((t, "stable_flow", sym, {
                                "stablecoin_net_24h_usd": float(net24),
                                "stablecoin_net_7d_usd": float(row["net_7d_change_usd"]),
                                "stablecoin_total_usd": float(row["total_circulating_usd"]),
                                "side_hint": side_hint,
                                "event_kind": kind,
                            }))
                            stable_flow_event_count += 1
                t += day_ms
        notes.append(f"stable_flow events emitted: {stable_flow_event_count}")

    funding_carry_event_count = 0
    if include_funding_carry:
        try:
            carry_events = reconstruct_funding_carry(
                symbols=symbols, start_ms=start_ms, end_ms=end_ms,
            )
        except Exception as e:
            notes.append(f"funding_carry history unavailable: {e}")
            carry_events = []
        for ce in carry_events:
            events.append((int(ce["ts_ms"]), "funding_carry", ce["symbol"], ce))
            funding_carry_event_count += 1
        notes.append(f"funding_carry events emitted: {funding_carry_event_count}")

    if include_fgi_contrarian:
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
        if kind == "funding":
            rate = float(payload.get("funding_rate", 0.0))
            mark = float(payload.get("mark_price", 0.0))
        elif kind == "funding_carry":
            rate = float(payload.get("rate", 0.0))
            mark = float(payload.get("mark_price", 0.0))
        else:
            rate = 0.0
            mark = float(payload.get("price", 0.0))
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
                # Tell brain about the close so re-entry cooldown applies.
                # CORRECTNESS-CRITICAL (audit C2): without `closed_at`,
                # rule_brain.review_trade falls back to time.time()*1000
                # → cooldown ms_since==0 → blocks every re-entry across the
                # wall-clock window of the run, not the simulated window.
                brain.review_trade({
                    "symbol": pos["trade"].symbol,
                    "pnl_pct": pos["trade"].pnl_pct,
                    "exit": pos["trade"].exit_price,
                    "exit_price": pos["trade"].exit_price,
                    "closed_at": pos["trade"].exit_time_ms,
                    "signal_type": pos["trade"].strategy,
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
        elif kind == "top_mover":
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
        elif kind == "listing":
            listing_ts = int(payload.get("listing_date_ms", ts_ms))
            age_hours = max(0.0, (ts_ms - listing_ts) / 3_600_000)
            exchange = payload.get("exchange", "")
            close_price = float(klines[idx]["close"]) if klines else 0.0
            stop_pct = 0.08 if "coinbase" in exchange.lower() else 0.05
            packet = SignalPacket(
                signal_id=f"bt-list-{sym}-{ts_ms}",
                symbol=sym,
                signal_type="listing_pump",
                priority=3,
                timestamp=float(ts_ms),
                price_usd=close_price,
                source=exchange or "binance_futures",
                reasoning=f"NEW {exchange or 'exchange'} listing detected. Age: {age_hours:.1f}h",
                suggested_side="long",
                suggested_stop_pct=stop_pct,
                suggested_target_pct=0.30,
                data={"listing_age_hours": age_hours, "exchange": exchange},
            )
        elif kind == "stable_flow":
            net24 = float(payload.get("stablecoin_net_24h_usd", 0.0))
            event_kind = payload.get("event_kind", "stable_flow_bull")
            side_hint = payload.get("side_hint", "long")
            close_price = float(klines[idx]["close"]) if klines else 0.0
            packet = SignalPacket(
                signal_id=f"bt-stbl-{sym}-{ts_ms}",
                symbol=sym,
                signal_type=event_kind,
                priority=2,
                timestamp=float(ts_ms),
                price_usd=close_price,
                source="defillama_stablecoins",
                reasoning=f"stablecoin net 24h ${net24/1e6:+.0f}M ({event_kind}) — {side_hint}",
                suggested_side=side_hint,
                suggested_stop_pct=0.06 if side_hint == "long" else 0.05,
                suggested_target_pct=0.12 if side_hint == "long" else 0.08,
                data={
                    "stablecoin_net_24h_usd": net24,
                    "stablecoin_net_7d_usd": float(payload.get("stablecoin_net_7d_usd", 0.0)),
                    "stablecoin_total_usd": float(payload.get("stablecoin_total_usd", 0.0)),
                },
            )
        elif kind == "funding_carry":
            carry_rate = float(payload.get("rate", 0.0))
            carry_rank = float(payload.get("funding_rank_pct", 1.0))
            event_kind = payload.get("event_type", "funding_carry_long")
            side_hint = payload.get("side_hint", "long")
            close_price = float(klines[idx]["close"]) if klines else float(payload.get("mark_price", 0.0))
            packet = SignalPacket(
                signal_id=f"bt-carry-{sym}-{ts_ms}",
                symbol=sym,
                signal_type=event_kind,
                priority=2,
                timestamp=float(ts_ms),
                price_usd=close_price,
                volume_24h=vol_24h,
                price_change_24h=change_24h,
                funding_rate=carry_rate,
                source="binance_funding_xsec",
                reasoning=f"x-sec funding carry rank={carry_rank*100:.0f}% rate={carry_rate*100:+.3f}% — {side_hint}",
                suggested_side=side_hint,
                suggested_stop_pct=0.06,
                suggested_target_pct=0.10,
                data={
                    "acceleration_1h": accel,
                    "funding_rate": carry_rate,
                    "funding_rank_pct": carry_rank,
                    "mark_price": close_price,
                },
            )
        else:  # fgi
            fgi_val = int(payload.get("fgi", 50))
            side_hint = payload.get("side_hint", "long")
            close_price = float(klines[idx]["close"]) if klines else 0.0
            packet = SignalPacket(
                signal_id=f"bt-fgi-{sym}-{ts_ms}",
                symbol=sym,
                signal_type="fgi_contrarian",
                priority=2 if fgi_val <= 20 else 1,
                timestamp=float(ts_ms),
                price_usd=close_price,
                fear_greed_index=fgi_val,
                source="alternative_me",
                reasoning=f"FGI={fgi_val} ({'extreme fear' if fgi_val<=20 else 'extreme greed'}) — contrarian {side_hint}",
                suggested_side=side_hint,
                suggested_stop_pct=0.12 if side_hint == "long" else 0.07,
                suggested_target_pct=0.20 if side_hint == "long" else 0.12,
                data={"value": fgi_val},
            )

        # Refresh brain state and tick
        brain.balance = balance
        brain.funding_rates = {sym: rate}
        if kind == "fgi":
            brain.fgi = int(payload.get("fgi", 50))
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
            # Pull the strategy label from thesis_conditions (set by
            # rule_brain.tick) so the chain can bypass macro signals like
            # stable_flow_bull/bear that the derivatives filters don't apply to.
            d_strategy = ""
            tc = getattr(d, "thesis_conditions", None) or {}
            if isinstance(tc, dict):
                d_strategy = tc.get("strategy", "") or ""
            if not d_strategy:
                d_strategy = getattr(d, "strategy_type", "") or ""
            # Compute the realised-vol regime once per decision using BTC
            # 1h klines as the market-wide proxy. Cheap because the inner
            # functions are O(168 + 90) on a memoised series.
            current_regime = "neutral"
            if apply_regime_gate and btc_klines_for_regime:
                try:
                    current_regime = regime_at_timestamp(
                        d.symbol,
                        btc_klines_for_regime,
                        ts_ms,
                        backtest_start_ms=start_ms,
                        backtest_end_ms=end_ms,
                    )
                except Exception:
                    current_regime = "neutral"
                regime_seen[current_regime] = regime_seen.get(current_regime, 0) + 1
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
                    ls_history=ls_by_symbol.get(d.symbol, []),
                    klines_1h=klines_by_symbol.get(d.symbol, []),
                    signal_type=d_strategy,
                    regime_strategy=d_strategy,
                    current_regime=current_regime if apply_regime_gate else None,
                )
                if not check.allowed:
                    if check.rule == "regime":
                        regime_blocks += 1
                    else:
                        other_blocks += 1
                    continue

            # Apply funding-rate-aware sizing (live executor mirror)
            size_usd = d.size_usd * _funding_size_multiplier(rate, d.side)
            if size_usd > balance * 0.4:
                size_usd = balance * 0.4
            if size_usd < 5:
                continue

            # Apply entry slippage to fill price (adverse to position side).
            entry_slip_bps = 0.0
            if apply_slippage:
                entry_slip_bps = _slippage_bps(
                    d.symbol, size_usd, "entry", d_klines, entry_idx,
                )
                if d.side == "long":
                    entry_price = entry_price * (1 + entry_slip_bps / 10_000.0)
                else:
                    entry_price = entry_price * (1 - entry_slip_bps / 10_000.0)

            exit_idx, exit_price, reason = _simulate_exit(
                d_klines, entry_idx, entry_price, d.side, d.stop_pct, d.target_pct,
                symbol=d.symbol,
            )

            # Apply exit slippage (adverse to closing direction).
            exit_slip_bps = 0.0
            if apply_slippage:
                exit_slip_bps = _slippage_bps(
                    d.symbol, size_usd, "exit", d_klines, exit_idx,
                )
                if d.side == "long":
                    exit_price = exit_price * (1 - exit_slip_bps / 10_000.0)
                else:
                    exit_price = exit_price * (1 + exit_slip_bps / 10_000.0)

            qty = size_usd / entry_price
            gross_pnl = (exit_price - entry_price) * qty if d.side == "long" else (entry_price - exit_price) * qty
            fees = (entry_price * qty * TAKER_FEE_PER_SIDE) + (exit_price * qty * TAKER_FEE_PER_SIDE)
            net_pnl = gross_pnl - fees
            pnl_pct = net_pnl / size_usd * 100.0
            slippage_usd = size_usd * (entry_slip_bps + exit_slip_bps) / 10_000.0

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
                slippage_usd=slippage_usd,
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
    total_slippage_usd = sum(t.slippage_usd for t in trades)

    # Crude Sharpe proxy: mean / stdev of trade-level pct returns (no annualisation)
    sharpe_proxy = 0.0
    if num_trades > 1:
        mean_r = avg_pnl_pct
        var = sum((t.pnl_pct - mean_r) ** 2 for t in trades) / (num_trades - 1)
        std = var ** 0.5
        if std > 0:
            sharpe_proxy = mean_r / std

    if min_score_override is not None:
        # Restore the prod threshold so subsequent calls aren't polluted.
        import src.engine.rule_brain as _rb
        _rb.MIN_SCORE_TO_TRADE = _orig_min_score

    if apply_regime_gate:
        notes.append(
            f"regime gate: blocked={regime_blocks}, other_filter_blocks={other_blocks}, "
            f"regime distribution among gated decisions calm={regime_seen.get('calm',0)} "
            f"neutral={regime_seen.get('neutral',0)} hot={regime_seen.get('hot',0)}"
        )

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
        total_slippage_usd=total_slippage_usd,
        trades=trades,
        notes=notes,
    )
