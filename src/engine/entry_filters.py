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
    """Block alt entries during the thinnest UTC hours."""
    now = datetime.now(timezone.utc)
    hour = now.hour
    if _THIN_HOUR_START <= hour < _THIN_HOUR_END:
        sym = (decision.symbol or "").upper()
        if sym not in _MAJOR_SYMBOLS:
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
    (fresh longs to liquidate). Without this, we enter after the move."""
    sym = (decision.symbol or "").upper()
    binance_sym = f"{sym}USDT"
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


# ─── 6. CVD divergence gate ────────────────────────────────────────────────

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
# Requirement: at least $50k of opposite-side liquidations in the last 5min,
# and the dominant side must align with our direction. Numbers tuned for
# small/mid-cap alts; majors typically see 10-100x larger cascades.

_LIQ_USD_MIN = 50_000           # minimum cascade size to consider
_LIQ_DOMINANCE_RATIO = 1.5      # dominant side must be 1.5x the other


def liquidation_cascade_filter(decision, ctx: dict) -> FilterVerdict:
    """For longs: require recent SHORT liquidations (squeeze fuel).
    For shorts: require recent LONG liquidations (capitulation fuel).
    Falls open if tracker isn't running yet (first 5 min after restart)."""
    try:
        from src.engine.liquidation_tracker import get_tracker
        tracker = get_tracker()
    except Exception:
        return FilterVerdict(allowed=True, rule="liq_cascade", reason="tracker unavailable")
    if tracker.status != "connected":
        return FilterVerdict(allowed=True, rule="liq_cascade", reason="tracker not connected")
    summary = tracker.cascade_score(decision.symbol, window_seconds=300)
    long_usd = summary["long_liq_usd_5m"]
    short_usd = summary["short_liq_usd_5m"]
    total = long_usd + short_usd
    if total < _LIQ_USD_MIN:
        return FilterVerdict(
            allowed=False, rule="liq_cascade",
            reason=f"{decision.symbol} low liq activity 5m (${total:,.0f}) — no cascade to ride",
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
