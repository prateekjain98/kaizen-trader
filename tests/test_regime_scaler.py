"""Tests for proactive regime-based parameter scaling."""
import pytest
from src.indicators.core import OHLCV
from src.risk.regime_scaler import compute_atr_percentile, scale_for_regime, RegimeScaling


class TestATRPercentile:
    def test_returns_none_with_insufficient_history(self):
        candles = [OHLCV(100, 105, 95, 102, 1000, i * 60000) for i in range(10)]
        result = compute_atr_percentile(candles, lookback=90)
        assert result is None

    def test_returns_percentile_between_0_and_1(self):
        candles = []
        for i in range(120):
            h = 100 + (i % 10)
            l = 100 - (i % 10)
            candles.append(OHLCV(100, h, l, 100, 1000, i * 60000))
        result = compute_atr_percentile(candles, lookback=90)
        assert result is not None
        assert 0.0 <= result <= 1.0

    def test_high_vol_candles_give_high_percentile(self):
        candles = [OHLCV(100, 101, 99, 100, 1000, i * 60000) for i in range(110)]
        for i in range(110, 125):
            candles.append(OHLCV(100, 120, 80, 100, 1000, i * 60000))
        result = compute_atr_percentile(candles, lookback=90)
        assert result is not None
        assert result > 0.7


class TestScaleForRegime:
    def test_high_vol_widens_stops(self):
        scaling = scale_for_regime(atr_percentile=0.9)
        assert scaling.stop_multiplier > 1.0

    def test_high_vol_reduces_size(self):
        scaling = scale_for_regime(atr_percentile=0.9)
        assert scaling.size_multiplier < 1.0

    def test_low_vol_tightens_stops(self):
        scaling = scale_for_regime(atr_percentile=0.1)
        assert scaling.stop_multiplier < 1.0

    def test_low_vol_increases_size(self):
        scaling = scale_for_regime(atr_percentile=0.1)
        assert scaling.size_multiplier > 1.0

    def test_normal_vol_returns_neutral(self):
        scaling = scale_for_regime(atr_percentile=0.5)
        assert 0.95 <= scaling.stop_multiplier <= 1.05
        assert 0.95 <= scaling.size_multiplier <= 1.05

    def test_stop_multiplier_has_bounds(self):
        low = scale_for_regime(atr_percentile=0.0)
        high = scale_for_regime(atr_percentile=1.0)
        assert low.stop_multiplier >= 0.7
        assert high.stop_multiplier <= 1.5

    def test_size_multiplier_has_bounds(self):
        low = scale_for_regime(atr_percentile=0.0)
        high = scale_for_regime(atr_percentile=1.0)
        assert high.size_multiplier >= 0.4
        assert low.size_multiplier <= 1.4
