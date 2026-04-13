"""Core technical indicators — ATR, EMA, Bollinger Bands, MACD, ADX, OBV."""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from src.utils.safe_math import safe_ratio


# ─── Data types ──────────────────────────────────────────────────────────────

@dataclass
class OHLCV:
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts: float


@dataclass
class IndicatorSnapshot:
    """All computed indicators for a symbol at a point in time."""
    symbol: str
    ts: float
    atr_14: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width: Optional[float] = None
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    obv: Optional[float] = None
    rsi_14: Optional[float] = None


# ─── Indicator computation functions ─────────────────────────────────────────

def compute_atr(candles: list[OHLCV], period: int = 14) -> Optional[float]:
    """Average True Range. Requires at least period+1 candles."""
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    # Wilder's smoothed ATR
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return safe_ratio(atr)


def compute_ema(values: list[float], period: int) -> Optional[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period  # SMA seed
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return safe_ratio(ema)


def compute_ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series (for MACD which needs EMA of EMA)."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    result = [ema]
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        result.append(ema)
    return result


def compute_bollinger_bands(
    closes: list[float], period: int = 20, num_std: float = 2.0,
) -> Optional[tuple[float, float, float, float]]:
    """Returns (upper, middle, lower, width) or None."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    if middle == 0:
        return None
    variance = sum((c - middle) ** 2 for c in window) / period
    std = math.sqrt(variance)
    upper = middle + num_std * std
    lower = middle - num_std * std
    width = (upper - lower) / middle
    return upper, middle, lower, safe_ratio(width)


def compute_macd(
    closes: list[float],
    fast: int = 12, slow: int = 26, signal_period: int = 9,
) -> Optional[tuple[float, float, float]]:
    """Returns (macd_line, signal_line, histogram) or None."""
    if len(closes) < slow + signal_period:
        return None
    fast_ema = compute_ema_series(closes, fast)
    slow_ema = compute_ema_series(closes, slow)
    if not fast_ema or not slow_ema:
        return None
    # Align lengths: fast_ema starts earlier than slow_ema
    offset = len(fast_ema) - len(slow_ema)
    macd_values = [f - s for f, s in zip(fast_ema[offset:], slow_ema)]
    if len(macd_values) < signal_period:
        return None
    signal_ema = compute_ema_series(macd_values, signal_period)
    if not signal_ema:
        return None
    macd_line = macd_values[-1]
    signal_line = signal_ema[-1]
    histogram = macd_line - signal_line
    return safe_ratio(macd_line), safe_ratio(signal_line), safe_ratio(histogram)


def compute_adx(candles: list[OHLCV], period: int = 14) -> Optional[tuple[float, float, float]]:
    """Returns (ADX, +DI, -DI) or None. Requires 2*period + 1 candles."""
    if len(candles) < 2 * period + 1:
        return None

    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []
    tr_list: list[float] = []

    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_high = candles[i - 1].high
        prev_low = candles[i - 1].low
        prev_close = candles[i - 1].close

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    # Wilder smoothing — seed with AVERAGE (not sum) per Wilder's original formula
    def _wilder_smooth(values: list[float], p: int) -> list[float]:
        smoothed = [sum(values[:p]) / p]
        for v in values[p:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / p + v)
        return smoothed

    smoothed_tr = _wilder_smooth(tr_list, period)
    smoothed_plus = _wilder_smooth(plus_dm_list, period)
    smoothed_minus = _wilder_smooth(minus_dm_list, period)

    dx_list: list[float] = []
    for i in range(len(smoothed_tr)):
        tr_val = smoothed_tr[i]
        if tr_val == 0:
            continue
        plus_di = 100 * smoothed_plus[i] / tr_val
        minus_di = 100 * smoothed_minus[i] / tr_val
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0)
        else:
            dx_list.append(100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_list) < period:
        return None

    adx_smoothed = _wilder_smooth(dx_list, period)
    adx = adx_smoothed[-1] if adx_smoothed else 0

    last_tr = smoothed_tr[-1]
    if last_tr == 0:
        return None
    plus_di = 100 * smoothed_plus[-1] / last_tr
    minus_di = 100 * smoothed_minus[-1] / last_tr

    return safe_ratio(adx), safe_ratio(plus_di), safe_ratio(minus_di)


def compute_obv(candles: list[OHLCV]) -> Optional[float]:
    """On-Balance Volume."""
    if len(candles) < 2:
        return None
    obv = 0.0
    for i in range(1, len(candles)):
        if candles[i].close > candles[i - 1].close:
            obv += candles[i].volume
        elif candles[i].close < candles[i - 1].close:
            obv -= candles[i].volume
    return obv


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """RSI using Wilder smoothing."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing for remaining
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            avg_gain = (avg_gain * (period - 1) + diff) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) - diff) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return safe_ratio(100 - 100 / (1 + rs))


def compute_vwap(closes: list[float], volumes: list[float]) -> Optional[float]:
    """Compute volume-weighted average price from parallel close/volume lists."""
    if not closes or len(closes) != len(volumes):
        return None
    sum_pv = sum(c * v for c, v in zip(closes, volumes))
    sum_v = sum(volumes)
    return sum_pv / sum_v if sum_v > 0 else None


# ─── Per-symbol candle buffer + snapshot cache ───────────────────────────────

_MAX_SYMBOLS = 500
_MAX_CANDLES = 250  # enough for EMA200 + some margin
_CANDLE_INTERVAL_MS = 60_000  # 1-minute candles

_lock = threading.Lock()
_candle_buffers: dict[str, deque[OHLCV]] = {}
_snapshot_cache: dict[str, IndicatorSnapshot] = {}
_snapshot_ttl_ms = 5_000  # recompute at most every 5 seconds


def push_tick(symbol: str, price: float, volume: float) -> None:
    """Aggregate ticks into 1-minute OHLCV candles."""
    now = time.time() * 1000
    with _lock:
        # Evict oldest symbol if at capacity
        if symbol not in _candle_buffers and len(_candle_buffers) >= _MAX_SYMBOLS:
            oldest = min(_candle_buffers, key=lambda k: _candle_buffers[k][-1].ts if _candle_buffers[k] else 0)
            del _candle_buffers[oldest]
            _snapshot_cache.pop(oldest, None)

        buf = _candle_buffers.setdefault(symbol, deque(maxlen=_MAX_CANDLES))

        if buf and (now - buf[-1].ts) < _CANDLE_INTERVAL_MS:
            # Update current candle
            c = buf[-1]
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += volume
        else:
            # New candle
            buf.append(OHLCV(
                open=price, high=price, low=price, close=price,
                volume=volume, ts=now,
            ))


def get_atr(symbol: str, period: int = 14) -> Optional[float]:
    """Get ATR for a symbol from the candle buffer, excluding the incomplete current candle."""
    now = time.time() * 1000
    with _lock:
        raw = _candle_buffers.get(symbol)
        buf = list(raw) if raw else None
    if not buf:
        return None
    # Exclude the last candle if it is still forming (within the current candle interval)
    if len(buf) > 1 and (now - buf[-1].ts) < _CANDLE_INTERVAL_MS:
        buf = buf[:-1]
    return compute_atr(buf, period)


def get_snapshot(symbol: str) -> Optional[IndicatorSnapshot]:
    """Get a full indicator snapshot, using cache if fresh."""
    now = time.time() * 1000
    with _lock:
        cached = _snapshot_cache.get(symbol)
        if cached and (now - cached.ts) < _snapshot_ttl_ms:
            return cached
        buf = list(_candle_buffers.get(symbol, []))

    if len(buf) < 15:
        return None

    closes = [c.close for c in buf]
    snap = IndicatorSnapshot(symbol=symbol, ts=now)

    snap.atr_14 = compute_atr(buf, 14)
    snap.ema_20 = compute_ema(closes, 20)
    snap.ema_50 = compute_ema(closes, 50)
    snap.ema_200 = compute_ema(closes, 200)
    snap.rsi_14 = compute_rsi(closes, 14)
    snap.obv = compute_obv(buf)

    bb = compute_bollinger_bands(closes, 20, 2.0)
    if bb:
        snap.bb_upper, snap.bb_middle, snap.bb_lower, snap.bb_width = bb

    macd = compute_macd(closes)
    if macd:
        snap.macd_line, snap.macd_signal, snap.macd_histogram = macd

    adx = compute_adx(buf, 14)
    if adx:
        snap.adx, snap.plus_di, snap.minus_di = adx

    with _lock:
        _snapshot_cache[symbol] = snap

    return snap


def get_candles(symbol: str) -> list[OHLCV]:
    """Get a copy of the candle buffer for a symbol."""
    with _lock:
        return list(_candle_buffers.get(symbol, []))


# ─── ATR-based stop calculation ──────────────────────────────────────────────

# Strategy-specific ATR multipliers for trailing stops
ATR_MULTIPLIERS: dict[str, float] = {
    "momentum_swing": 2.0,
    "momentum_scalp": 1.5,
    "mean_reversion": 2.5,
    "funding_extreme": 2.0,
    "liquidation_cascade": 1.5,
    "orderbook_imbalance": 1.5,
    "whale_accumulation": 2.0,
    "correlation_break": 2.0,
    "narrative_momentum": 2.0,
    "protocol_revenue": 2.5,
    "fear_greed_contrarian": 4.0,
    "cross_exchange_divergence": 3.0,
    "listing_pump": 1.5,
}

DEFAULT_ATR_MULTIPLIER = 2.0


def compute_atr_stop(
    symbol: str, entry_price: float, side: str, strategy: str,
    fallback_trail_pct: float = 0.07,
) -> tuple[float, float]:
    """Compute ATR-based stop price and effective trail percentage.

    Returns (stop_price, effective_trail_pct).
    Falls back to fixed percentage if ATR is unavailable.
    """
    atr = get_atr(symbol)
    multiplier = ATR_MULTIPLIERS.get(strategy, DEFAULT_ATR_MULTIPLIER)

    if atr and atr > 0 and entry_price > 0:
        stop_distance = atr * multiplier
        effective_trail_pct = stop_distance / entry_price
        # Clamp trail to reasonable bounds (3% - 25%)
        # 1% was too tight — BTC fluctuates 1% within minutes, triggering premature exits
        effective_trail_pct = max(0.03, min(0.25, effective_trail_pct))
    else:
        effective_trail_pct = fallback_trail_pct

    if side == "long":
        stop_price = entry_price * (1 - effective_trail_pct)
    else:
        stop_price = entry_price * (1 + effective_trail_pct)

    return stop_price, effective_trail_pct


def compute_atr_trailing_stop(
    symbol: str, watermark: float, side: str, strategy: str,
    current_stop: float, fallback_trail_pct: float = 0.07,
) -> float:
    """Update trailing stop using ATR. Only tightens, never widens."""
    atr = get_atr(symbol)
    multiplier = ATR_MULTIPLIERS.get(strategy, DEFAULT_ATR_MULTIPLIER)

    if atr and atr > 0 and watermark > 0:
        stop_distance = atr * multiplier
        effective_trail_pct = max(0.03, min(0.25, stop_distance / watermark))
    else:
        effective_trail_pct = fallback_trail_pct

    if side == "long":
        new_stop = watermark * (1 - effective_trail_pct)
        return max(new_stop, current_stop)  # only tighten
    else:
        new_stop = watermark * (1 + effective_trail_pct)
        return min(new_stop, current_stop) if current_stop > 0 else new_stop


# ─── Multi-timeframe candle aggregation ──────────────────────────────────────

_HTF_INTERVALS = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_MAX_HTF_CANDLES = 200

_htf_lock = threading.Lock()
_htf_buffers: dict[str, dict[str, deque[OHLCV]]] = {}  # symbol -> timeframe -> candles


def push_htf_candle(symbol: str, timeframe: str, candle: OHLCV) -> None:
    """Push a completed candle to a higher-timeframe buffer."""
    with _htf_lock:
        if symbol not in _htf_buffers:
            _htf_buffers[symbol] = {}
        buf = _htf_buffers[symbol].setdefault(timeframe, deque(maxlen=_MAX_HTF_CANDLES))
        buf.append(candle)


def _aggregate_to_htf(symbol: str) -> None:
    """Aggregate 1-minute candles into higher timeframes.

    Called periodically to roll up minute candles into 1h/4h/1d.
    """
    with _lock:
        minute_candles = list(_candle_buffers.get(symbol, []))
    if len(minute_candles) < 2:
        return

    for tf_name, interval_ms in _HTF_INTERVALS.items():
        with _htf_lock:
            existing = _htf_buffers.get(symbol, {}).get(tf_name, [])
            last_ts = existing[-1].ts if existing else 0

        # Find minute candles that belong to a new HTF period
        new_candles: dict[int, list[OHLCV]] = {}
        for c in minute_candles:
            if c.ts <= last_ts:
                continue
            bucket = int(c.ts // interval_ms)
            new_candles.setdefault(bucket, []).append(c)

        for bucket in sorted(new_candles.keys()):
            group = new_candles[bucket]
            htf_candle = OHLCV(
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                ts=bucket * interval_ms,
            )
            push_htf_candle(symbol, tf_name, htf_candle)


def get_htf_candles(symbol: str, timeframe: str) -> list[OHLCV]:
    """Get candles for a higher timeframe (1h, 4h, 1d)."""
    with _htf_lock:
        return list(_htf_buffers.get(symbol, {}).get(timeframe, []))


def get_htf_snapshot(symbol: str, timeframe: str) -> Optional[IndicatorSnapshot]:
    """Compute indicators on a higher timeframe."""
    candles = get_htf_candles(symbol, timeframe)
    if len(candles) < 15:
        return None

    closes = [c.close for c in candles]
    snap = IndicatorSnapshot(symbol=symbol, ts=time.time() * 1000)

    snap.atr_14 = compute_atr(candles, 14)
    snap.ema_20 = compute_ema(closes, 20)
    snap.ema_50 = compute_ema(closes, 50)
    snap.rsi_14 = compute_rsi(closes, 14)

    bb = compute_bollinger_bands(closes, 20, 2.0)
    if bb:
        snap.bb_upper, snap.bb_middle, snap.bb_lower, snap.bb_width = bb

    macd = compute_macd(closes)
    if macd:
        snap.macd_line, snap.macd_signal, snap.macd_histogram = macd

    adx = compute_adx(candles, 14)
    if adx:
        snap.adx, snap.plus_di, snap.minus_di = adx

    return snap
