"""Stackable entry-quality gates for the trading bot.

Each filter is a pure function that decides whether to ALLOW a trade entry,
given a decision and current market context. They run AFTER the brain says
"buy/sell" but BEFORE the executor commits capital.

Designed to layer on top of the Claude brain so we don't fight its decisions
— we just refuse the lowest-quality ones the brain wants to take. Order
matters: cheapest checks first (no API call), then network-dependent ones.

Each filter logs its decision and reason so calibration is auditable from
journalctl.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from src.engine.log import log


@dataclass
class FilterVerdict:
    allowed: bool
    rule: str = ""
    reason: str = ""


# ─── 1. Time-of-day gate ────────────────────────────────────────────────────

# Empirically (per research notes): Asia hours (00:00-08:00 UTC) have the
# thinnest order books, the most aggressive funding-rate manipulation by
# whales, and the worst slippage on small alts. The US/EU overlap
# (13:00-21:00 UTC) is where squeezes have follow-through volume.
#
# We don't hard-block Asia hours entirely — instead require BTC dominance
# to be trending (set externally) or the symbol to be top-tier (BTC/ETH/SOL).
# For the first cut: hard skip 02:00-07:00 UTC for everything except majors.

_MAJOR_SYMBOLS = frozenset({"BTC", "ETH", "SOL", "BNB", "XRP"})
_THIN_HOUR_START = 2   # 02:00 UTC
_THIN_HOUR_END = 7     # 07:00 UTC


def time_of_day_filter(decision, ctx: dict) -> FilterVerdict:
    """Block alt entries during the thinnest UTC hours.
    Bypassed for extreme funding setups (|rate|>0.20%) — the squeeze edge
    on funding is large enough to absorb the worse Asia-hours slippage."""
    now = datetime.now(timezone.utc)
    hour = now.hour
    if _THIN_HOUR_START <= hour < _THIN_HOUR_END:
        sym = (decision.symbol or "").upper()
        if sym not in _MAJOR_SYMBOLS:
            funding = _funding_rate_from_reasoning(decision.reasoning)
            if funding is not None and abs(funding) > 0.0020:
                return FilterVerdict(
                    allowed=True, rule="time_of_day",
                    reason=f"{sym} thin-hour bypass — extreme funding {funding*100:+.3f}%",
                )
            return FilterVerdict(
                allowed=False, rule="time_of_day",
                reason=f"alt {sym} skipped during thin hours {hour:02d}:00 UTC (Asia lull)",
            )
    return FilterVerdict(allowed=True)


# ─── 2. OI-delta confirmation gate ──────────────────────────────────────────

# Funding rate alone is lagging. The leading indicator is OI direction:
# when funding is negative AND OI is RISING, fresh shorts are piling in →
# real squeeze setup. When funding is negative but OI is FALLING, longs
# already got liquidated → fuel spent, no follow-through.
#
# Binance: GET /fapi/v1/openInterestHist?symbol=...&period=5m&limit=12 → 1h.
# Free, no auth, ~10ms latency.

_OI_HIST_URL = "https://fapi.binance.com/futures/data/openInterestHist"


def _fetch_oi_change_pct(binance_symbol: str, lookback_minutes: int = 60) -> Optional[float]:
    """Return OI %-change over the last N minutes, or None on error."""
    try:
        period = "5m"
        limit = max(2, lookback_minutes // 5)
        resp = requests.get(
            _OI_HIST_URL,
            params={"symbol": binance_symbol, "period": period, "limit": limit},
            timeout=5,
        )
        resp.raise_for_status()
        rows = resp.json()
        if len(rows) < 2:
            return None
        oldest = float(rows[0]["sumOpenInterest"])
        newest = float(rows[-1]["sumOpenInterest"])
        if oldest <= 0:
            return None
        return (newest - oldest) / oldest
    except Exception as e:
        log("warn", f"OI fetch failed for {binance_symbol}: {e}")
        return None


def oi_delta_filter(decision, ctx: dict) -> FilterVerdict:
    """For long entries on negative funding, require OI to be RISING (fresh
    shorts to squeeze). For shorts on positive funding, require OI rising
    (fresh longs to liquidate). Without this, we enter after the move.

    Bypassed when funding rate is extreme (|rate|>0.20%) — the squeeze edge
    on funding alone is large enough that we don't also need OI confirmation.
    Mirrors the existing time_of_day extreme-funding bypass. Without this,
    the bot can be paralysed during real squeeze events: brain qualifies
    BUYs on -0.30% funding but oi_delta blocks every single one because OI
    is moving 1-2% (below the 3% threshold), even though the funding
    extreme IS the squeeze signal."""
    sym = (decision.symbol or "").upper()
    binance_sym = f"{sym}USDT"
    funding = _funding_rate_from_reasoning(decision.reasoning)
    if funding is not None and abs(funding) > 0.0020 and sym not in _MAJOR_SYMBOLS:
        return FilterVerdict(
            allowed=True, rule="oi_delta",
            reason=f"{sym} OI bypass — extreme funding {funding*100:+.3f}% is the signal",
        )
    oi_change = _fetch_oi_change_pct(binance_sym, lookback_minutes=60)
    if oi_change is None:
        # Can't verify → don't block (fail-open). Better to lose a filter
        # check than block all trades if Binance flaps.
        return FilterVerdict(allowed=True, rule="oi_delta", reason="oi fetch unavailable")
    # 3% threshold: OI moved meaningfully in the last hour. Below this is noise.
    if abs(oi_change) < 0.03:
        return FilterVerdict(
            allowed=False, rule="oi_delta",
            reason=f"{sym} OI flat ({oi_change*100:+.1f}% / 1h) — no fresh positioning to squeeze",
        )
    # Direction check: longs want rising OI (more shorts to squeeze).
    if decision.side == "long" and oi_change < 0:
        return FilterVerdict(
            allowed=False, rule="oi_delta",
            reason=f"{sym} OI falling ({oi_change*100:+.1f}%) — squeeze fuel spent, skip long",
        )
    if decision.side == "short" and oi_change < 0:
        return FilterVerdict(
            allowed=False, rule="oi_delta",
            reason=f"{sym} OI falling ({oi_change*100:+.1f}%) — no fresh longs to dump, skip short",
        )
    return FilterVerdict(
        allowed=True, rule="oi_delta",
        reason=f"{sym} OI {oi_change*100:+.1f}% / 1h supports {decision.side}",
    )


# ─── 3. Perp-spot basis gate ────────────────────────────────────────────────

# When perpetual mark price diverges from spot index by >0.3%, that's an
# unsustainable dislocation that mean-reverts in minutes — strong tradeable
# signal. Below 0.1% the basis is just funding-rate noise.
#
# Binance: GET /fapi/v1/premiumIndex?symbol=...
# Returns markPrice + indexPrice (proxy for spot). Free, ~10ms.

_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


def _fetch_basis_pct(binance_symbol: str) -> Optional[float]:
    """Return (mark - index) / index as a fraction, or None on error."""
    try:
        resp = requests.get(_PREMIUM_URL, params={"symbol": binance_symbol}, timeout=5)
        resp.raise_for_status()
        d = resp.json()
        mark = float(d.get("markPrice", 0))
        index = float(d.get("indexPrice", 0))
        if index <= 0:
            return None
        return (mark - index) / index
    except Exception as e:
        log("warn", f"Basis fetch failed for {binance_symbol}: {e}")
        return None


def basis_filter(decision, ctx: dict) -> FilterVerdict:
    """For long entries (negative-funding squeeze setup), prefer perp trading
    BELOW spot (basis < 0). For short entries, prefer perp ABOVE spot.
    Confirms the directional edge from a price-level signal that's independent
    of funding rate."""
    sym = (decision.symbol or "").upper()
    binance_sym = f"{sym}USDT"
    basis = _fetch_basis_pct(binance_sym)
    if basis is None:
        return FilterVerdict(allowed=True, rule="basis", reason="basis fetch unavailable")
    if decision.side == "long" and basis > 0.001:
        return FilterVerdict(
            allowed=False, rule="basis",
            reason=f"{sym} perp +{basis*100:.2f}% over spot — already extended, skip long",
        )
    if decision.side == "short" and basis < -0.001:
        return FilterVerdict(
            allowed=False, rule="basis",
            reason=f"{sym} perp {basis*100:.2f}% under spot — already discounted, skip short",
        )
    return FilterVerdict(
        allowed=True, rule="basis",
        reason=f"{sym} basis {basis*100:+.2f}% supports {decision.side}",
    )


# ─── 4. Correlation gate ────────────────────────────────────────────────────

# At small capital, holding 3 alts simultaneously is functionally one
# concentrated bet — when alts dump, all three lose. Block opens that would
# create a same-tier concentration.
#
# Cheap proxy: tier symbols by category. Don't allow 2+ "small alt" longs
# simultaneously. This is coarser than 1h-return correlation but free and
# works at small N.

_TIER_MAJORS = frozenset({"BTC", "ETH"})
_TIER_LARGE = frozenset({"SOL", "BNB", "XRP", "AVAX", "MATIC", "LINK", "DOT", "UNI", "ATOM"})


def _tier_of(symbol: str) -> str:
    s = (symbol or "").upper()
    if s in _TIER_MAJORS:
        return "major"
    if s in _TIER_LARGE:
        return "large"
    return "small_alt"


def correlation_filter(decision, ctx: dict) -> FilterVerdict:
    """Block when opening would create concentration in the small-alt tier."""
    open_positions = ctx.get("open_positions", []) or []
    new_tier = _tier_of(decision.symbol)
    if new_tier != "small_alt":
        return FilterVerdict(allowed=True)
    # Allow at most 1 concurrent small-alt position. Brain can still rotate.
    same_tier_open = sum(1 for p in open_positions if _tier_of(p.symbol) == "small_alt")
    if same_tier_open >= 1:
        return FilterVerdict(
            allowed=False, rule="correlation",
            reason=f"already holding {same_tier_open} small-alt position; "
                   f"refusing to concentrate further into {decision.symbol}",
        )
    return FilterVerdict(allowed=True)


# ─── 5. Volatility gate (ATR sanity) ────────────────────────────────────────

# Skip entries when 15m ATR is extreme — either dead-quiet (no edge available)
# or chaotically wild (slippage will eat us). Use 0.5%-8% as the sane band
# for 15m ATR. Computed from Binance kline.

_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"


def _fetch_atr_pct(binance_symbol: str, interval: str = "15m", lookback: int = 14) -> Optional[float]:
    """Return ATR as a % of close, computed from `lookback` 15m candles."""
    try:
        resp = requests.get(
            _KLINE_URL,
            params={"symbol": binance_symbol, "interval": interval, "limit": lookback + 1},
            timeout=5,
        )
        resp.raise_for_status()
        klines = resp.json()
        if len(klines) < lookback + 1:
            return None
        # kline = [openTime, o, h, l, c, v, ...]
        trs = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        atr = sum(trs) / len(trs)
        last_close = float(klines[-1][4])
        if last_close <= 0:
            return None
        return atr / last_close
    except Exception as e:
        log("warn", f"ATR fetch failed for {binance_symbol}: {e}")
        return None


def volatility_filter(decision, ctx: dict) -> FilterVerdict:
    """Reject extremely quiet (no opportunity) or extremely wild (no edge)
    setups. Stash the computed ATR back into ctx so a downstream sizing rule
    or stop-setter can reuse it."""
    sym = (decision.symbol or "").upper()
    binance_sym = f"{sym}USDT"
    atr_pct = _fetch_atr_pct(binance_sym)
    if atr_pct is None:
        return FilterVerdict(allowed=True, rule="volatility", reason="atr fetch unavailable")
    ctx["atr_pct_15m"] = atr_pct  # downstream may consume
    if atr_pct < 0.005:
        return FilterVerdict(
            allowed=False, rule="volatility",
            reason=f"{sym} ATR(15m) {atr_pct*100:.2f}% — too quiet to recover fees",
        )
    if atr_pct > 0.08:
        return FilterVerdict(
            allowed=False, rule="volatility",
            reason=f"{sym} ATR(15m) {atr_pct*100:.2f}% — too chaotic, slippage risk",
        )
    return FilterVerdict(
        allowed=True, rule="volatility",
        reason=f"{sym} ATR(15m) {atr_pct*100:.2f}% — within 0.5-8% band",
    )


# ─── 6. Top-trader long/short crowding gate ────────────────────────────────

# Binance publishes the long/short position ratio of the top 20% of traders
# by margin balance. Extreme crowding is a contrarian signal: when smart
# money is 70%+ one direction, a squeeze/reversal is more likely than
# continuation. Don't ADD to the crowd; let it unwind instead.
#
# Endpoint: GET /futures/data/topLongShortPositionRatio?symbol=...&period=5m
# Returns: longShortRatio (long/short), longAccount (% long), shortAccount.
# Free, no auth, ~50ms latency. 5min update cadence.
#
# Thresholds: longShortRatio > 2.5 → top traders are 71%+ long (refuse adds)
#             longShortRatio < 0.5 → top traders are 67%+ short (refuse adds)

_TOP_RATIO_URL = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
_TOP_RATIO_LONG_BLOCK = 2.5
_TOP_RATIO_SHORT_BLOCK = 0.5
# Per-symbol cache so we don't hit the endpoint on every brain decision.
# 5min TTL — matches the endpoint's update cadence.
_top_ratio_cache: dict = {}  # symbol → (timestamp, ratio)
_TOP_RATIO_TTL_SEC = 300


def _fetch_top_ls_ratio(binance_symbol: str) -> Optional[float]:
    """Return the latest top-trader long/short position ratio, or None on
    error. Cached for 5 minutes per symbol."""
    now = time.time()
    cached = _top_ratio_cache.get(binance_symbol)
    if cached and now - cached[0] < _TOP_RATIO_TTL_SEC:
        return cached[1]
    try:
        # 2s timeout — this runs on the brain-tick thread which has a 60s
        # interval. With up to 10 symbols per tick, 5s × 10 = 50s worst case
        # consumes nearly the entire tick window. 2s caps the worst case to
        # 20s and the 5-min TTL means the next tick will hit cache anyway.
        resp = requests.get(
            _TOP_RATIO_URL,
            params={"symbol": binance_symbol, "period": "5m", "limit": 1},
            timeout=2,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        ratio = float(rows[0].get("longShortRatio", 0))
        if ratio <= 0:
            return None
        _top_ratio_cache[binance_symbol] = (now, ratio)
        return ratio
    except Exception as e:
        log("warn", f"top L/S ratio fetch failed for {binance_symbol}: {e}")
        # Defensive: evict any stale entry so we can't accidentally serve it
        # back during the same brain tick if a TTL race ever exposed one.
        _top_ratio_cache.pop(binance_symbol, None)
        return None


def top_trader_crowding_filter(decision, ctx: dict) -> FilterVerdict:
    """Block entries that would add to an already-crowded top-trader
    position. Refuses longs when top traders are >71% long; refuses shorts
    when >67% short. Falls open on fetch error so a Binance hiccup doesn't
    block all trades."""
    sym = (decision.symbol or "").upper()
    binance_sym = f"{sym}USDT"
    ratio = _fetch_top_ls_ratio(binance_sym)
    if ratio is None:
        return FilterVerdict(allowed=True, rule="top_crowding", reason="fetch unavailable")
    ctx["top_long_short_ratio"] = ratio
    long_pct = ratio / (1 + ratio) * 100  # proportion long
    if decision.side == "long" and ratio > _TOP_RATIO_LONG_BLOCK:
        return FilterVerdict(
            allowed=False, rule="top_crowding",
            reason=f"{sym} top traders {long_pct:.0f}% long (ratio {ratio:.2f}) — refuse to add to crowd",
        )
    if decision.side == "short" and ratio < _TOP_RATIO_SHORT_BLOCK:
        return FilterVerdict(
            allowed=False, rule="top_crowding",
            reason=f"{sym} top traders {100-long_pct:.0f}% short (ratio {ratio:.2f}) — refuse to add to crowd",
        )
    return FilterVerdict(
        allowed=True, rule="top_crowding",
        reason=f"{sym} top L/S ratio {ratio:.2f} — {decision.side} OK",
    )


# ─── 7. CVD divergence gate ────────────────────────────────────────────────

# CVD = cumulative volume delta (buyer-initiated minus seller-initiated $).
# For longs we don't want to enter into a sell tape (CVD strongly negative);
# for shorts we don't want to enter into a buy tape (CVD strongly positive).
# This is a "don't fight the flow" filter — bullish/bearish divergence is a
# stronger entry signal but harder to formalize as a gate, so we use the
# weaker but reliable "flow at least neutral in our direction" rule.

# Two named thresholds (one positive, one negative) to remove the
# double-negation maintenance trap. A future maintainer can change either
# without inverting the other's meaning.
_CVD_LONG_BLOCK_BELOW = -25_000   # block longs when 15m CVD is more negative
_CVD_SHORT_BLOCK_ABOVE = 25_000   # block shorts when 15m CVD exceeds


def cvd_flow_filter(decision, ctx: dict) -> FilterVerdict:
    """For longs: CVD over the last 15min must not be deeply negative
    (meaning: don't long into a sustained sell tape). For shorts: must not
    be deeply positive. Falls open if the tracker isn't yet running or has
    no history.

    Does NOT auto-subscribe on miss — that would leak subscriptions for
    symbols the bot considers but never enters. The Executor.open_position
    path already subscribes on actual entry, so the next time this symbol
    appears in a decision (after history accumulates) the filter has data."""
    try:
        from src.engine.cvd_tracker import get_tracker
        tracker = get_tracker()
    except Exception:
        return FilterVerdict(allowed=True, rule="cvd_flow", reason="tracker unavailable")
    if tracker.status != "connected":
        return FilterVerdict(allowed=True, rule="cvd_flow", reason="tracker not connected")
    cvd = tracker.cvd(decision.symbol, window_seconds=900)  # 15min
    if cvd is None:
        return FilterVerdict(allowed=True, rule="cvd_flow",
                             reason=f"{decision.symbol} cvd not subscribed — fail-open")
    # Stash unconditionally so any downstream consumer (logging, analytics,
    # sizing) can read it whether we pass or block.
    ctx["cvd_15m_usd"] = cvd
    if decision.side == "long" and cvd < _CVD_LONG_BLOCK_BELOW:
        return FilterVerdict(
            allowed=False, rule="cvd_flow",
            reason=f"{decision.symbol} 15m CVD ${cvd:,.0f} — sell tape, refuse long entry",
        )
    if decision.side == "short" and cvd > _CVD_SHORT_BLOCK_ABOVE:
        return FilterVerdict(
            allowed=False, rule="cvd_flow",
            reason=f"{decision.symbol} 15m CVD ${cvd:,.0f} — buy tape, refuse short entry",
        )
    return FilterVerdict(
        allowed=True, rule="cvd_flow",
        reason=f"{decision.symbol} 15m CVD ${cvd:,.0f} — {decision.side} OK",
    )


# ─── 7. Liquidation cascade gate ────────────────────────────────────────────

# The strongest 5-30min directional signal in crypto perps: when shorts get
# liquidated en masse, longs ride the forced-buy follow-through. Conversely,
# a long-liquidation cascade fuels short continuation.
#
# Requirement: opposite-side liquidations must dominate the direction we
# want to trade. Threshold scales by symbol category — majors typically see
# 10-100x larger cascades than small alts. Previously used a flat $50k
# threshold which filtered out 100% of small-alt entries (the bot's main
# universe via funding-squeeze setups).
#
# Bypass: extreme funding rate (|rate| > 0.25%) means the squeeze setup is
# already strong on the funding signal alone — accept even without a
# cascade, since cascade is "early/ongoing squeeze" but extreme funding is
# "primed but not yet fired".

_LIQ_THRESHOLDS = {
    "major": 250_000,    # BTC, ETH, SOL, BNB, XRP — need a real cascade
    "large": 50_000,     # AVAX/MATIC/LINK/DOT/UNI/ATOM tier
    "small_alt": 5_000,  # everything else — much smaller real cascades
}
_LIQ_DOMINANCE_RATIO = 1.5      # dominant side must be 1.5x the other
# When funding is this extreme, the squeeze setup is strong on its own —
# don't wait for an in-progress cascade that may never materialize on
# obscure alts. Avoids the failure mode observed 2026-04-29→30 where 22/22
# brain decisions on MOVR/API3/CHIP/SOLV got blocked despite -0.2% funding.
_LIQ_BYPASS_FUNDING_PCT = 0.0020  # |rate| > 0.20% per 8h


def _funding_rate_from_reasoning(reasoning: str) -> Optional[float]:
    """RuleBrain emits reasoning like 'extreme neg funding -0.220% +40'.
    Parse the % number. Returns None if not parseable."""
    import re
    m = re.search(r"funding\s+([+-]?\d+\.?\d*)%", reasoning or "", re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1)) / 100.0
    except ValueError:
        return None


def _liq_threshold_for(symbol: str) -> float:
    """Per-tier liquidation threshold. Reuses the tier-of helper from the
    correlation filter so the categorization is shared."""
    return _LIQ_THRESHOLDS[_tier_of(symbol)]


def liquidation_cascade_filter(decision, ctx: dict) -> FilterVerdict:
    """For longs: require recent SHORT liquidations (squeeze fuel).
    For shorts: require recent LONG liquidations (capitulation fuel).
    Falls open if tracker isn't running yet (first 5 min after restart).

    Bypass: if the brain's reasoning indicates extreme funding
    (|rate|>0.20%/8h), allow even with no cascade — the funding signal is
    strong enough that waiting for a cascade often costs us the entry.
    """
    try:
        from src.engine.liquidation_tracker import get_tracker
        tracker = get_tracker()
    except Exception:
        return FilterVerdict(allowed=True, rule="liq_cascade", reason="tracker unavailable")
    if tracker.status != "connected":
        return FilterVerdict(allowed=True, rule="liq_cascade", reason="tracker not connected")

    # Extreme-funding bypass — parse from brain's reasoning string.
    funding = _funding_rate_from_reasoning(decision.reasoning)
    if funding is not None and abs(funding) > _LIQ_BYPASS_FUNDING_PCT:
        ctx["funding_rate"] = funding
        return FilterVerdict(
            allowed=True, rule="liq_cascade",
            reason=f"{decision.symbol} bypass — extreme funding {funding*100:+.3f}% "
                   f"(|>{_LIQ_BYPASS_FUNDING_PCT*100:.2f}%|)",
        )

    summary = tracker.cascade_score(decision.symbol, window_seconds=300)
    long_usd = summary["long_liq_usd_5m"]
    short_usd = summary["short_liq_usd_5m"]
    total = long_usd + short_usd
    threshold = _liq_threshold_for(decision.symbol)
    if total < threshold:
        return FilterVerdict(
            allowed=False, rule="liq_cascade",
            reason=f"{decision.symbol} ({_tier_of(decision.symbol)}) low liq activity 5m "
                   f"(${total:,.0f} < ${threshold:,.0f}) — no cascade to ride",
        )
    needed_side = "short" if decision.side == "long" else "long"
    needed_usd = short_usd if needed_side == "short" else long_usd
    other_usd = long_usd if needed_side == "short" else short_usd
    if needed_usd < other_usd * _LIQ_DOMINANCE_RATIO:
        return FilterVerdict(
            allowed=False, rule="liq_cascade",
            reason=f"{decision.symbol} {decision.side}: need {needed_side}-liqs to dominate, "
                   f"got long=${long_usd:,.0f} short=${short_usd:,.0f}",
        )
    # Stash for downstream sizing/exits
    ctx["liq_cascade_usd"] = needed_usd
    return FilterVerdict(
        allowed=True, rule="liq_cascade",
        reason=f"{decision.symbol} {needed_side}-liqs ${needed_usd:,.0f}/5m supports {decision.side}",
    )


# ─── Filter chain runner ────────────────────────────────────────────────────

# Order = cheapest-first: time_of_day (no API), then 4 API-bound checks.
# All filter functions share the same signature (decision, ctx) -> verdict.
DEFAULT_FILTERS: list[Callable] = [
    time_of_day_filter,
    correlation_filter,
    volatility_filter,
    cvd_flow_filter,              # WS-fed, sub-second; "don't fight flow"
    liquidation_cascade_filter,   # WS-fed, sub-second; high-edge
    top_trader_crowding_filter,   # cached HTTP; 5min TTL; contrarian gate
    basis_filter,
    oi_delta_filter,
]


def run_filters(decision, ctx: dict, filters: Optional[list] = None) -> FilterVerdict:
    """Run filter chain, short-circuit on first block. Returns the blocking
    verdict, or an allow if all pass."""
    chain = filters if filters is not None else DEFAULT_FILTERS
    for f in chain:
        v = f(decision, ctx)
        if not v.allowed:
            log("info", f"Entry filter BLOCK [{v.rule}]: {v.reason}")
            return v
    return FilterVerdict(allowed=True, rule="all", reason="all filters passed")
