"""Offline replay of the live `entry_filters` chain on historical data.

Mirrors the rules in `src.engine.entry_filters` but reads from cached
historical data instead of hitting live Binance endpoints. This lets the
backtest measure the chain's edge without polluting results with live
fail-open behaviour.

Replayed filters (PARITY with prod logic):
    1. time_of_day_filter — uses signal timestamp's UTC hour
    2. correlation_filter — uses simulator's open_positions list
    3. volatility_filter — computes 15m ATR from kline series
    4. oi_delta_filter   — uses historical openInterestHist via oi_loader
    5. basis_filter      — uses synchronised perp/spot 1h closes
    6. top_trader_crowding — uses topLongShortPositionRatio history (5m,
                              ~30d Binance retention — older windows
                              fail-open like oi_delta)
    7. cvd_flow            — approximates CVD from kline takerBuyBase
                              (col 9) over a 4h window; no aggTrade replay
                              needed. Falls open on stale caches lacking
                              the taker-buy column.

Skipped filters (DECLARED, no historical analogue available offline):
    * liquidation_cascade  (no public historical forceOrder dump)

Each backtest run that calls `run_offline_filters` records the list of
SKIPPED filters in the `notes` section of the result, so honest gating
keeps working — a positive PnL on this chain proves the REPLAYABLE
sub-chain has edge, not the full prod chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.backtesting.oi_loader import load_open_interest


# ─── Constants kept in lock-step with src.engine.entry_filters ───────────

_MAJOR_SYMBOLS = frozenset({"BTC", "ETH", "SOL", "BNB", "XRP"})
_THIN_HOUR_START = 2
_THIN_HOUR_END = 7
_EXTREME_FUNDING_BYPASS = 0.0020  # |rate| > 0.20% bypasses thin-hour skip

_TIER_MAJORS = frozenset({"BTC", "ETH"})
_TIER_LARGE = frozenset({"SOL", "BNB", "XRP", "AVAX", "MATIC", "LINK", "DOT", "UNI", "ATOM"})

_ATR_LOW = 0.005   # 0.5%
_ATR_HIGH = 0.08   # 8%
_ATR_LOOKBACK_15M = 14

_OI_FLAT_THRESHOLD = 0.03  # |Δ| < 3% over 1h is "flat"


REPLAYABLE = ["regime", "time_of_day", "correlation", "volatility", "oi_delta", "basis", "top_crowding", "cvd_flow"]
SKIPPED = ["liquidation_cascade"]

# Regime-switch meta-gate. In CALM regimes (low realised vol) only
# mean-reversion plays survive; in HOT regimes only trend/momentum.
# NEUTRAL allows everything (default posture). See regime_detector.py.
_MEAN_REVERT_TYPES = frozenset({
    "funding_squeeze",
    "fgi_contrarian",
    "stable_flow_bull",
    "stable_flow_bear",
    # Cross-sectional funding carry is a REVERSION trade — extreme funding
    # rates revert toward the median over the next funding window. Belongs
    # with funding_squeeze / fgi_contrarian in the mean-revert bucket.
    "funding_carry",
    "funding_carry_long",
    "funding_carry_short",
})
_TREND_TYPES = frozenset({
    "large_move",
    "major_pump",
    "listing_pump",
})

# CVD flow thresholds — kept in lock-step with src.engine.entry_filters.
# Prod accumulates aggTrade-level signed USD flow over a 15min window and
# blocks longs below -25k / shorts above +25k. Offline we approximate with
# (taker_buy - taker_sell) base-volume summed across a 4-bar (4h) sliding
# window of 1h klines, then convert to USD via the latest close. The 4h
# window smooths the coarser bar resolution; thresholds are the same.
_CVD_LONG_BLOCK_BELOW = -25_000.0
_CVD_SHORT_BLOCK_ABOVE = 25_000.0
_CVD_WINDOW_BARS = 4


# Basis filter constants (parity with src.engine.entry_filters.basis_filter)
_BASIS_LONG_BLOCK = 0.001    # perp >0.1% over spot blocks new longs
_BASIS_SHORT_BLOCK = -0.001  # perp <-0.1% under spot blocks new shorts

# Top-trader crowding constants (parity with src.engine.entry_filters)
_TOP_RATIO_LONG_BLOCK = 2.5    # ratio > 2.5 → 71%+ long, refuse new longs
_TOP_RATIO_SHORT_BLOCK = 0.5   # ratio < 0.5 → 67%+ short, refuse new shorts


@dataclass
class FilterCheck:
    allowed: bool
    rule: str
    reason: str = ""


def _tier_of(symbol: str) -> str:
    s = (symbol or "").upper()
    if s in _TIER_MAJORS:
        return "major"
    if s in _TIER_LARGE:
        return "large"
    return "small_alt"


# ─── Replayable filter logic ─────────────────────────────────────────────

def time_of_day_check(symbol: str, ts_ms: int, funding_rate: float) -> FilterCheck:
    """Block alt entries during thin UTC hours unless funding is extreme.
    Mirrors `entry_filters.time_of_day_filter` exactly."""
    hour = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour
    sym = symbol.upper()
    if _THIN_HOUR_START <= hour < _THIN_HOUR_END:
        if sym not in _MAJOR_SYMBOLS:
            if abs(funding_rate) > _EXTREME_FUNDING_BYPASS:
                return FilterCheck(True, "time_of_day",
                                   f"thin-hour bypass — extreme funding {funding_rate*100:+.3f}%")
            return FilterCheck(False, "time_of_day",
                               f"alt {sym} skipped during thin hour {hour:02d}:00 UTC")
    return FilterCheck(True, "time_of_day")


def correlation_check(symbol: str, open_position_symbols: list[str]) -> FilterCheck:
    """Block when opening would create concentration in small-alt tier."""
    new_tier = _tier_of(symbol)
    if new_tier != "small_alt":
        return FilterCheck(True, "correlation")
    same_tier_open = sum(1 for s in open_position_symbols if _tier_of(s) == "small_alt")
    if same_tier_open >= 1:
        return FilterCheck(False, "correlation",
                           f"already holding {same_tier_open} small-alt; refusing further concentration into {symbol}")
    return FilterCheck(True, "correlation")


def _atr_pct_from_klines(klines_15m: list[dict], idx_15m: int) -> Optional[float]:
    """Compute ATR%(15m) using the 14 candles ending at idx_15m."""
    if idx_15m < _ATR_LOOKBACK_15M:
        return None
    trs: list[float] = []
    for i in range(idx_15m - _ATR_LOOKBACK_15M + 1, idx_15m + 1):
        high = float(klines_15m[i]["high"])
        low = float(klines_15m[i]["low"])
        prev_close = float(klines_15m[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atr = sum(trs) / len(trs)
    last_close = float(klines_15m[idx_15m]["close"])
    if last_close <= 0:
        return None
    return atr / last_close


def volatility_check(symbol: str, klines_15m: list[dict], ts_ms: int) -> FilterCheck:
    """Block when 15m ATR is dead-quiet (<0.5%) or chaotic (>8%)."""
    if not klines_15m:
        return FilterCheck(True, "volatility", "no klines — fail-open")
    # locate the 15m candle covering ts_ms
    idx = None
    for i, k in enumerate(klines_15m):
        if k["open_time"] <= ts_ms <= k["close_time"]:
            idx = i
            break
    if idx is None:
        # fallback: nearest preceding
        for i in range(len(klines_15m) - 1, -1, -1):
            if klines_15m[i]["close_time"] <= ts_ms:
                idx = i
                break
    if idx is None:
        return FilterCheck(True, "volatility", "no covering candle — fail-open")
    atr = _atr_pct_from_klines(klines_15m, idx)
    if atr is None:
        return FilterCheck(True, "volatility", "atr unavailable — fail-open")
    if atr < _ATR_LOW:
        return FilterCheck(False, "volatility",
                           f"{symbol} ATR(15m) {atr*100:.2f}% — too quiet")
    if atr > _ATR_HIGH:
        return FilterCheck(False, "volatility",
                           f"{symbol} ATR(15m) {atr*100:.2f}% — too chaotic")
    return FilterCheck(True, "volatility", f"{symbol} ATR(15m) {atr*100:.2f}% — within band")


def oi_delta_check(
    symbol: str,
    side: str,
    ts_ms: int,
    oi_history: list[dict],
) -> FilterCheck:
    """Mirror prod `oi_delta_filter`: require >=3% |Δ| over last 1h AND
    direction-supportive change. oi_history pre-loaded for the symbol."""
    if not oi_history:
        return FilterCheck(True, "oi_delta", "oi unavailable — fail-open")
    # Find OI value at ts_ms and ts_ms - 1h
    one_hour_ago = ts_ms - 3_600_000
    cur = past = None
    for r in oi_history:
        t = r.get("timestamp", 0)
        if t <= one_hour_ago:
            past = r
        if t <= ts_ms:
            cur = r
        else:
            break
    if cur is None or past is None:
        return FilterCheck(True, "oi_delta", "oi history thin — fail-open")
    cur_oi = float(cur.get("sum_open_interest", 0))
    past_oi = float(past.get("sum_open_interest", 0))
    if past_oi <= 0:
        return FilterCheck(True, "oi_delta", "past oi zero — fail-open")
    change = (cur_oi - past_oi) / past_oi
    if abs(change) < _OI_FLAT_THRESHOLD:
        return FilterCheck(False, "oi_delta",
                           f"{symbol} OI flat ({change*100:+.1f}% / 1h) — no fresh positioning")
    if side == "long" and change < 0:
        return FilterCheck(False, "oi_delta",
                           f"{symbol} OI falling ({change*100:+.1f}%) — squeeze fuel spent, skip long")
    if side == "short" and change < 0:
        return FilterCheck(False, "oi_delta",
                           f"{symbol} OI falling ({change*100:+.1f}%) — no fresh longs, skip short")
    return FilterCheck(True, "oi_delta",
                       f"{symbol} OI {change*100:+.1f}% / 1h supports {side}")


def basis_check(
    symbol: str,
    side: str,
    ts_ms: int,
    spot_klines_1h: list[dict],
    futures_klines_1h: list[dict],
) -> FilterCheck:
    """Mirror prod basis_filter using historical spot+futures 1h closes
    (offline analogue of the live premiumIndex endpoint).

    For longs: block if perp > spot by >0.1% (already extended).
    For shorts: block if perp < spot by >0.1% (already discounted).
    """
    if not spot_klines_1h or not futures_klines_1h:
        return FilterCheck(True, "basis", "spot/fut klines unavailable — fail-open")
    # Find the kline whose open_time covers ts_ms in each series
    s_close = f_close = None
    for k in reversed(spot_klines_1h):
        if k["open_time"] <= ts_ms:
            s_close = float(k["close"])
            break
    for k in reversed(futures_klines_1h):
        if k["open_time"] <= ts_ms:
            f_close = float(k["close"])
            break
    if s_close is None or f_close is None or s_close <= 0:
        return FilterCheck(True, "basis", "no covering kline — fail-open")
    basis = (f_close - s_close) / s_close
    if side == "long" and basis > _BASIS_LONG_BLOCK:
        return FilterCheck(False, "basis",
                           f"{symbol} perp +{basis*100:.2f}% over spot — already extended, skip long")
    if side == "short" and basis < _BASIS_SHORT_BLOCK:
        return FilterCheck(False, "basis",
                           f"{symbol} perp {basis*100:.2f}% under spot — already discounted, skip short")
    return FilterCheck(True, "basis",
                       f"{symbol} basis {basis*100:+.2f}% supports {side}")


def top_ls_check(
    symbol: str,
    side: str,
    ts_ms: int,
    ls_history: list[dict],
) -> FilterCheck:
    """Mirror prod `top_trader_crowding_filter`: refuse longs when top traders
    are >71% long (ratio > 2.5) and refuse shorts when >67% short
    (ratio < 0.5). Uses the most recent ratio at-or-before ts_ms (live filter
    polls the latest 5m bucket; we read the same).

    Falls open when no history is available — same posture as prod, which
    fail-opens on fetch error so a Binance hiccup doesn't block all trades.
    """
    if not ls_history:
        return FilterCheck(True, "top_crowding", "ls history unavailable — fail-open")
    cur = None
    for r in ls_history:
        t = r.get("timestamp", 0)
        if t <= ts_ms:
            cur = r
        else:
            break
    if cur is None:
        return FilterCheck(True, "top_crowding", "ls history thin — fail-open")
    ratio = float(cur.get("long_short_ratio", 0))
    if ratio <= 0:
        return FilterCheck(True, "top_crowding", "ratio zero — fail-open")
    long_pct = ratio / (1 + ratio) * 100
    if side == "long" and ratio > _TOP_RATIO_LONG_BLOCK:
        return FilterCheck(False, "top_crowding",
                           f"{symbol} top traders {long_pct:.0f}% long (ratio {ratio:.2f}) — refuse to add to crowd")
    if side == "short" and ratio < _TOP_RATIO_SHORT_BLOCK:
        return FilterCheck(False, "top_crowding",
                           f"{symbol} top traders {100-long_pct:.0f}% short (ratio {ratio:.2f}) — refuse to add to crowd")
    return FilterCheck(True, "top_crowding",
                       f"{symbol} top L/S ratio {ratio:.2f} — {side} OK")


def cvd_check(
    symbol: str,
    side: str,
    ts_ms: int,
    klines_1h: list[dict],
) -> FilterCheck:
    """Offline analogue of `entry_filters.cvd_flow_filter`.

    Approximates cumulative volume delta from Binance kline column 9
    (takerBuyBase): per bar, signed flow ≈ (taker_buy - taker_sell) * close.
    Sum across the last 4 bars (4h window on 1h klines) ending at-or-before
    ts_ms. Block longs when window CVD < -25k USD, shorts when > +25k USD.
    Falls open when klines or taker-buy data are unavailable (stale cache,
    short series), matching prod's fail-open posture.
    """
    if not klines_1h:
        return FilterCheck(True, "cvd_flow", "klines unavailable — fail-open")
    # Locate last bar at-or-before ts_ms
    idx = None
    for i in range(len(klines_1h) - 1, -1, -1):
        if klines_1h[i]["open_time"] <= ts_ms:
            idx = i
            break
    if idx is None or idx < _CVD_WINDOW_BARS - 1:
        return FilterCheck(True, "cvd_flow", "window thin — fail-open")
    window = klines_1h[idx - _CVD_WINDOW_BARS + 1: idx + 1]
    cvd_usd = 0.0
    saw_taker = False
    for k in window:
        tb = float(k.get("taker_buy_volume", 0.0) or 0.0)
        ts = float(k.get("taker_sell_volume", 0.0) or 0.0)
        if tb > 0 or ts > 0:
            saw_taker = True
        cvd_usd += (tb - ts) * float(k["close"])
    if not saw_taker:
        return FilterCheck(True, "cvd_flow", "stale cache lacks taker-buy — fail-open")
    if side == "long" and cvd_usd < _CVD_LONG_BLOCK_BELOW:
        return FilterCheck(False, "cvd_flow",
                           f"{symbol} 4h CVD ${cvd_usd:,.0f} — sell tape, refuse long entry")
    if side == "short" and cvd_usd > _CVD_SHORT_BLOCK_ABOVE:
        return FilterCheck(False, "cvd_flow",
                           f"{symbol} 4h CVD ${cvd_usd:,.0f} — buy tape, refuse short entry")
    return FilterCheck(True, "cvd_flow",
                       f"{symbol} 4h CVD ${cvd_usd:,.0f} — {side} OK")


# ─── Regime meta-gate ────────────────────────────────────────────────────

def regime_check(strategy_type: str, regime: Optional[str]) -> FilterCheck:
    """Meta-gate that switches strategy families on/off by RV regime.

    CALM:    block trend/momentum (large_move, major_pump, listing_pump,
             funding_carry*) — mean-revert dominates in low-vol.
    HOT:     block mean-revert (funding_squeeze, fgi_contrarian,
             stable_flow_*) — trend dominates in high-vol.
    NEUTRAL: pass everything.

    Falls open when regime is None / unknown (e.g. the BTC baseline window
    isn't ready yet) so the gate degrades to today's behaviour.
    """
    if not regime or regime == "neutral":
        return FilterCheck(True, "regime")
    st = (strategy_type or "").lower()
    if regime == "calm":
        # Block trend types; allow mean-revert (incl. funding_carry).
        if st in _TREND_TYPES:
            return FilterCheck(False, "regime",
                               f"calm regime — {st} is trend/momentum, blocked")
        return FilterCheck(True, "regime", f"calm regime — {st} ok")
    if regime == "hot":
        if (st in _MEAN_REVERT_TYPES or st.startswith("stable_flow")
                or st.startswith("funding_carry")):
            return FilterCheck(False, "regime",
                               f"hot regime — {st} is mean-revert, blocked")
        return FilterCheck(True, "regime", f"hot regime — {st} ok")
    return FilterCheck(True, "regime")


# ─── Chain runner ────────────────────────────────────────────────────────

def run_offline_filters(
    symbol: str,
    side: str,
    ts_ms: int,
    funding_rate: float,
    open_position_symbols: list[str],
    klines_15m: Optional[list[dict]] = None,
    oi_history: Optional[list[dict]] = None,
    spot_klines_1h: Optional[list[dict]] = None,
    futures_klines_1h: Optional[list[dict]] = None,
    ls_history: Optional[list[dict]] = None,
    klines_1h: Optional[list[dict]] = None,
    signal_type: str = "",
    regime_strategy: Optional[str] = None,
    current_regime: Optional[str] = None,
) -> FilterCheck:
    """Run the replayable subset of the prod filter chain. Returns the
    first BLOCK or a final allow if nothing blocks.

    Stable_flow signals are MACRO bets on BTC/ETH driven by stablecoin
    mint/burn flows — the derivatives-microstructure filters
    (oi_delta, cvd_flow, top_crowding, basis, volatility) were calibrated
    for small-alt squeeze setups and shouldn't gate a macro thesis. Mirrors
    the extreme-funding bypass already used in time_of_day / oi_delta:
    when the upstream signal is strong and the chain's heuristics are
    out-of-domain, let the trade through rather than silently swallowing it.
    """
    # Regime meta-gate runs FIRST so trend/mean-revert mismatches get
    # rejected before we burn cycles on the per-symbol microstructure
    # checks. regime_strategy falls back to signal_type when not given.
    rg = regime_check(regime_strategy or signal_type, current_regime)
    if not rg.allowed:
        return rg
    if signal_type and signal_type.startswith("stable_flow"):
        return FilterCheck(
            True, "stable_flow_bypass",
            "macro signal — derivatives filters do not apply",
        )
    # Cross-sectional funding carry is ranked-relative — the alpha is in
    # the rank itself, not the absolute funding/OI/CVD posture the prod
    # filters were calibrated for. Without this bypass, the LONG side
    # (most-negative funding) would systematically fail oi_delta (books
    # falling, no fresh longs) and basis (perp under spot), and SHORTs
    # would fail basis (perp over spot, "extended"). Those are exactly
    # the trades carry WANTS. Same posture as the stable_flow_bypass and
    # the extreme-funding bypass in time_of_day_filter.
    if signal_type and signal_type.startswith("funding_carry"):
        return FilterCheck(
            True, "funding_carry_bypass",
            "cross-sectional carry — ranking is the signal, derivatives filters do not apply",
        )
    checks = [
        time_of_day_check(symbol, ts_ms, funding_rate),
        correlation_check(symbol, open_position_symbols),
        volatility_check(symbol, klines_15m or [], ts_ms),
        oi_delta_check(symbol, side, ts_ms, oi_history or []),
        basis_check(symbol, side, ts_ms, spot_klines_1h or [], futures_klines_1h or []),
        top_ls_check(symbol, side, ts_ms, ls_history or []),
        cvd_check(symbol, side, ts_ms, klines_1h or spot_klines_1h or []),
    ]
    for c in checks:
        if not c.allowed:
            return c
    return FilterCheck(True, "all_replayable_passed",
                       f"{symbol} {side} cleared time/corr/vol/oi/basis/top_ls/cvd (liq SKIPPED)")
