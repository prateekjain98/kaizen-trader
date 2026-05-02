"""Realized-volatility regime detector for the meta-gate.

Per research, mean-reversion strategies dominate in low-RV regimes while
trend/momentum strategies dominate in high-RV. Currently the bot filters
by ATR but doesn't SWITCH BEHAVIOR; this module supplies a coarse 3-way
classification (calm/neutral/hot) that the replay filter chain consumes
to enable/disable strategy families per regime.

Definition:
    RV_7d  = stdev(log returns of 1h candles, 168 bars) * sqrt(24*365)
    median = median of RV_7d over the trailing 90 days (rolling)
    ratio  = RV_7d / median
        ratio < 0.6 → "calm"  (allow mean-revert, block trend)
        ratio > 1.4 → "hot"   (allow trend, block mean-revert)
        else        → "neutral" (allow all)

BTC 1h klines are used as the market-wide volatility proxy. If the caller
hasn't preloaded BTC, regime_at_timestamp will lazy-load via load_klines
so the gate always has data — required by the spec.
"""

from __future__ import annotations

import math
from typing import Optional

from src.backtesting.data_loader import load_klines


_RV_BARS = 168                  # 7d × 24 1h candles
_BASELINE_DAYS_DEFAULT = 90
_BARS_PER_DAY_1H = 24
_ANNUALISATION = math.sqrt(24 * 365)

CALM_THRESHOLD = 0.6
HOT_THRESHOLD = 1.4

# Lazy-load cache so a single backtest run only fetches BTC klines once.
_btc_klines_cache: dict[tuple[int, int], list[dict]] = {}


def _log_returns(klines_1h: list[dict], start_idx: int, end_idx: int) -> list[float]:
    """log(close[i] / close[i-1]) for i in (start_idx+1 .. end_idx]."""
    out: list[float] = []
    for i in range(start_idx + 1, end_idx + 1):
        prev = float(klines_1h[i - 1]["close"])
        cur = float(klines_1h[i]["close"])
        if prev <= 0 or cur <= 0:
            continue
        out.append(math.log(cur / prev))
    return out


def _stdev(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return var ** 0.5


def _median(xs: list[float]) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    s = sorted(xs)
    if n % 2 == 1:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def compute_rv_7d(klines_1h: list[dict], idx: int) -> float:
    """Annualised realised vol of the last 168 1h bars ending at idx (inclusive).

    Returns 0.0 when not enough history is available — the regime gate
    treats that as "neutral" so the bot doesn't degrade silently.
    """
    if idx < _RV_BARS or idx >= len(klines_1h):
        return 0.0
    rets = _log_returns(klines_1h, idx - _RV_BARS, idx)
    if len(rets) < _RV_BARS // 2:
        return 0.0
    return _stdev(rets) * _ANNUALISATION


def compute_rv_baseline(
    klines_1h: list[dict],
    idx: int,
    lookback_days: int = _BASELINE_DAYS_DEFAULT,
) -> float:
    """Median of RV_7d sampled DAILY over the trailing `lookback_days`.

    Daily sampling (every 24 bars) keeps this O(lookback_days) instead of
    O(lookback_days * 24), which matters when called for every backtest
    tick. The median is robust to a single vol spike skewing the baseline.
    """
    if idx < _RV_BARS:
        return 0.0
    samples: list[float] = []
    step = _BARS_PER_DAY_1H
    earliest = max(_RV_BARS, idx - lookback_days * _BARS_PER_DAY_1H)
    i = earliest
    while i <= idx:
        rv = compute_rv_7d(klines_1h, i)
        if rv > 0:
            samples.append(rv)
        i += step
    return _median(samples)


def _btc_klines(start_ms: int, end_ms: int) -> list[dict]:
    """Memoised BTC 1h kline loader. Pads start by 90+7 days so the
    baseline window has data at the very first tick of the backtest."""
    key = (start_ms, end_ms)
    if key in _btc_klines_cache:
        return _btc_klines_cache[key]
    pad_ms = (_BASELINE_DAYS_DEFAULT + 8) * 86_400_000
    try:
        kl = load_klines("BTC", "1h", start_ms - pad_ms, end_ms)
    except Exception:
        kl = []
    _btc_klines_cache[key] = kl
    return kl


def _idx_at_or_before(klines_1h: list[dict], ts_ms: int) -> Optional[int]:
    if not klines_1h:
        return None
    lo, hi = 0, len(klines_1h) - 1
    if ts_ms < klines_1h[0]["open_time"]:
        return None
    if ts_ms >= klines_1h[hi]["open_time"]:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if klines_1h[mid]["open_time"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return lo


def regime_at_timestamp(
    symbol: str,
    klines_1h: Optional[list[dict]],
    ts_ms: int,
    backtest_start_ms: Optional[int] = None,
    backtest_end_ms: Optional[int] = None,
) -> str:
    """Return "calm" | "neutral" | "hot" for ts_ms.

    `symbol` is accepted for API-symmetry with the rest of replay_filters
    but the regime is computed off BTC as the market-wide proxy. If the
    caller passes empty klines for BTC, we lazy-load via load_klines using
    the backtest window (padded for the 90d baseline lookback).
    """
    btc = klines_1h or []
    if not btc or btc[0]["open_time"] > ts_ms - _RV_BARS * 3_600_000:
        if backtest_start_ms is not None and backtest_end_ms is not None:
            btc = _btc_klines(backtest_start_ms, backtest_end_ms)
    idx = _idx_at_or_before(btc, ts_ms)
    if idx is None or idx < _RV_BARS:
        return "neutral"
    rv = compute_rv_7d(btc, idx)
    base = compute_rv_baseline(btc, idx, _BASELINE_DAYS_DEFAULT)
    if rv <= 0 or base <= 0:
        return "neutral"
    ratio = rv / base
    if ratio < CALM_THRESHOLD:
        return "calm"
    if ratio > HOT_THRESHOLD:
        return "hot"
    return "neutral"
