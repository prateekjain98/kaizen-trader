"""Tests for src/indicators/core.py — ATR, EMA, Bollinger, MACD, ADX, RSI, OBV."""

import time
import threading
from unittest.mock import patch

import pytest

from src.indicators.core import (
    OHLCV, IndicatorSnapshot,
    compute_atr, compute_ema, compute_ema_series,
    compute_bollinger_bands, compute_macd, compute_adx, compute_obv, compute_rsi,
    push_tick, get_atr, get_snapshot, get_candles,
    compute_atr_stop, compute_atr_trailing_stop,
    ATR_MULTIPLIERS, DEFAULT_ATR_MULTIPLIER,
    _aggregate_to_htf, get_htf_candles, get_htf_snapshot,
    _candle_buffers, _snapshot_cache, _htf_buffers, _lock, _htf_lock,
)


def _make_candles(prices: list[float], volume: float = 100.0) -> list[OHLCV]:
    """Create candles from a list of close prices with synthetic OHLCV."""
    candles = []
    for i, p in enumerate(prices):
        candles.append(OHLCV(
            open=p * 0.999, high=p * 1.002, low=p * 0.998,
            close=p, volume=volume, ts=i * 60_000,
        ))
    return candles


def _reset_buffers():
    """Clear global state between tests."""
    with _lock:
        _candle_buffers.clear()
        _snapshot_cache.clear()
    with _htf_lock:
        _htf_buffers.clear()


class TestComputeATR:
    def test_returns_none_for_insufficient_candles(self):
        candles = _make_candles([100] * 5)
        assert compute_atr(candles, 14) is None

    def test_computes_atr_with_enough_candles(self):
        prices = [100 + i * 0.5 for i in range(30)]
        candles = _make_candles(prices)
        atr = compute_atr(candles, 14)
        assert atr is not None
        assert atr > 0

    def test_atr_increases_with_volatility(self):
        calm = _make_candles([100 + i * 0.1 for i in range(30)])
        volatile = _make_candles([100 + (i % 2) * 5 for i in range(30)])
        atr_calm = compute_atr(calm, 14)
        atr_volatile = compute_atr(volatile, 14)
        assert atr_volatile > atr_calm


class TestComputeEMA:
    def test_returns_none_for_insufficient_data(self):
        assert compute_ema([1, 2, 3], 20) is None

    def test_ema_of_constant_equals_constant(self):
        values = [50.0] * 30
        ema = compute_ema(values, 20)
        assert abs(ema - 50.0) < 0.01

    def test_ema_tracks_uptrend(self):
        values = list(range(1, 51))
        ema = compute_ema([float(v) for v in values], 10)
        # EMA should be close to recent values (above midpoint)
        assert ema > 25

    def test_ema_series_length(self):
        values = [float(i) for i in range(30)]
        series = compute_ema_series(values, 10)
        assert len(series) == 21  # 30 - 10 + 1


class TestComputeBollingerBands:
    def test_returns_none_for_insufficient_data(self):
        assert compute_bollinger_bands([1, 2, 3], 20) is None

    def test_returns_four_values(self):
        closes = [100 + i * 0.1 for i in range(25)]
        result = compute_bollinger_bands(closes, 20)
        assert result is not None
        upper, middle, lower, width = result
        assert upper > middle > lower
        assert width > 0

    def test_constant_prices_have_zero_width(self):
        closes = [100.0] * 25
        result = compute_bollinger_bands(closes, 20)
        assert result is not None
        upper, middle, lower, width = result
        assert abs(upper - lower) < 0.01
        assert abs(width) < 0.01


class TestComputeMACD:
    def test_returns_none_for_insufficient_data(self):
        assert compute_macd([1, 2, 3]) is None

    def test_returns_three_values(self):
        closes = [100 + i * 0.5 for i in range(50)]
        result = compute_macd(closes)
        assert result is not None
        macd_line, signal, histogram = result
        assert isinstance(macd_line, float)
        assert isinstance(signal, float)
        assert abs(histogram - (macd_line - signal)) < 0.001


class TestComputeADX:
    def test_returns_none_for_insufficient_candles(self):
        candles = _make_candles([100] * 10)
        assert compute_adx(candles, 14) is None

    def test_returns_three_values(self):
        prices = [100 + i * 0.5 for i in range(40)]
        candles = _make_candles(prices)
        result = compute_adx(candles, 14)
        assert result is not None
        adx, plus_di, minus_di = result
        assert adx >= 0
        assert plus_di >= 0
        assert minus_di >= 0


class TestComputeRSI:
    def test_returns_none_for_insufficient_data(self):
        assert compute_rsi([1, 2, 3], 14) is None

    def test_rsi_in_range(self):
        closes = [100 + i * 0.5 for i in range(30)]
        rsi = compute_rsi(closes, 14)
        assert rsi is not None
        assert 0 <= rsi <= 100

    def test_constant_prices_returns_100(self):
        # All gains zero, all losses zero → avg_loss = 0 → RSI = 100
        closes = [100.0] * 20
        rsi = compute_rsi(closes, 14)
        assert rsi == 100.0

    def test_uptrend_rsi_above_50(self):
        closes = [float(100 + i) for i in range(30)]
        rsi = compute_rsi(closes, 14)
        assert rsi > 50


class TestComputeOBV:
    def test_returns_none_for_single_candle(self):
        assert compute_obv(_make_candles([100])) is None

    def test_uptrend_obv_positive(self):
        candles = _make_candles([100 + i for i in range(10)])
        obv = compute_obv(candles)
        assert obv > 0

    def test_downtrend_obv_negative(self):
        candles = _make_candles([100 - i for i in range(10)])
        obv = compute_obv(candles)
        assert obv < 0


class TestPushTickAndSnapshot:
    def setup_method(self):
        _reset_buffers()

    def test_push_tick_creates_candles(self):
        for i in range(20):
            push_tick("BTC", 50000 + i, 1.0)
        candles = get_candles("BTC")
        assert len(candles) >= 1

    def test_get_atr_returns_none_without_data(self):
        assert get_atr("UNKNOWN") is None

    def test_get_snapshot_returns_none_without_data(self):
        assert get_snapshot("UNKNOWN") is None


class TestATRStop:
    def setup_method(self):
        _reset_buffers()

    @patch("src.indicators.core.get_atr", return_value=500.0)
    def test_long_stop_below_entry(self, mock_atr):
        stop, trail = compute_atr_stop("BTC", 50000, "long", "momentum_swing")
        assert stop < 50000
        assert 0.01 <= trail <= 0.25

    @patch("src.indicators.core.get_atr", return_value=500.0)
    def test_short_stop_above_entry(self, mock_atr):
        stop, trail = compute_atr_stop("BTC", 50000, "short", "momentum_swing")
        assert stop > 50000

    @patch("src.indicators.core.get_atr", return_value=None)
    def test_fallback_to_fixed_pct(self, mock_atr):
        stop, trail = compute_atr_stop("BTC", 50000, "long", "momentum_swing", fallback_trail_pct=0.05)
        assert trail == 0.05
        assert abs(stop - 50000 * 0.95) < 0.01

    @patch("src.indicators.core.get_atr", return_value=500.0)
    def test_trailing_stop_only_tightens_long(self, mock_atr):
        current_stop = 49000
        new_stop = compute_atr_trailing_stop("BTC", 51000, "long", "momentum_swing", current_stop)
        assert new_stop >= current_stop  # must not widen

    @patch("src.indicators.core.get_atr", return_value=500.0)
    def test_trailing_stop_only_tightens_short(self, mock_atr):
        current_stop = 52000
        new_stop = compute_atr_trailing_stop("BTC", 49000, "short", "momentum_swing", current_stop)
        assert new_stop <= current_stop  # must not widen


class TestATRMultipliers:
    def test_all_strategies_have_multipliers(self):
        expected = [
            "momentum_swing", "momentum_scalp", "mean_reversion",
            "funding_extreme", "liquidation_cascade", "orderbook_imbalance",
        ]
        for s in expected:
            assert s in ATR_MULTIPLIERS

    def test_default_multiplier(self):
        assert DEFAULT_ATR_MULTIPLIER == 2.0


class TestMultiTimeframe:
    def setup_method(self):
        _reset_buffers()

    def test_htf_candles_empty_initially(self):
        assert get_htf_candles("BTC", "1h") == []

    def test_htf_snapshot_returns_none_without_data(self):
        assert get_htf_snapshot("BTC", "1h") is None
