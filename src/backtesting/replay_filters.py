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

Skipped filters (DECLARED, no historical analogue available offline):
    * basis_filter         (needs synchronised perp/spot historical pair)
    * cvd_flow_filter      (needs full aggTrade tape replay — too expensive)
    * top_trader_crowding  (no public historical endpoint for the
                             topLongShortPositionRatio metric)
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


REPLAYABLE = ["time_of_day", "correlation", "volatility", "oi_delta", "basis"]
SKIPPED = ["cvd_flow", "top_trader_crowding", "liquidation_cascade"]


# Basis filter constants (parity with src.engine.entry_filters.basis_filter)
_BASIS_LONG_BLOCK = 0.001    # perp >0.1% over spot blocks new longs
_BASIS_SHORT_BLOCK = -0.001  # perp <-0.1% under spot blocks new shorts


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
) -> FilterCheck:
    """Run the replayable subset of the prod filter chain. Returns the
    first BLOCK or a final allow if nothing blocks."""
    checks = [
        time_of_day_check(symbol, ts_ms, funding_rate),
        correlation_check(symbol, open_position_symbols),
        volatility_check(symbol, klines_15m or [], ts_ms),
        oi_delta_check(symbol, side, ts_ms, oi_history or []),
        basis_check(symbol, side, ts_ms, spot_klines_1h or [], futures_klines_1h or []),
    ]
    for c in checks:
        if not c.allowed:
            return c
    return FilterCheck(True, "all_replayable_passed",
                       f"{symbol} {side} cleared time/corr/vol/oi/basis (cvd/ls/liq SKIPPED)")
