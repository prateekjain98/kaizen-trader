"""Tests for mean reversion strategy — VWAP and RSI calculations."""

import pytest

from src.strategies.mean_reversion import (
    _compute_vwap, _compute_rsi_from_samples as _compute_rsi, OHLCVSample,
)


# ── VWAP ───────────────────────────────────────────────────────────────────

class TestComputeVwap:
    def test_empty(self):
        assert _compute_vwap([]) is None

    def test_single_sample(self):
        samples = [OHLCVSample(close=100, volume=10, ts=0)]
        assert _compute_vwap(samples) == 100

    def test_equal_volume(self):
        samples = [
            OHLCVSample(close=100, volume=10, ts=0),
            OHLCVSample(close=200, volume=10, ts=1),
        ]
        assert _compute_vwap(samples) == 150  # equal volume = simple average

    def test_volume_weighted(self):
        samples = [
            OHLCVSample(close=100, volume=30, ts=0),
            OHLCVSample(close=200, volume=10, ts=1),
        ]
        # VWAP = (100*30 + 200*10) / (30+10) = 5000/40 = 125
        assert _compute_vwap(samples) == 125

    def test_zero_volume(self):
        samples = [
            OHLCVSample(close=100, volume=0, ts=0),
            OHLCVSample(close=200, volume=0, ts=1),
        ]
        assert _compute_vwap(samples) is None


# ── RSI ────────────────────────────────────────────────────────────────────

class TestComputeRsi:
    def test_insufficient_data(self):
        samples = [OHLCVSample(close=i, volume=1, ts=i) for i in range(10)]
        assert _compute_rsi(samples, period=14) is None

    def test_all_gains(self):
        # 15 samples with steadily increasing prices
        samples = [OHLCVSample(close=100 + i, volume=1, ts=i) for i in range(16)]
        rsi = _compute_rsi(samples, period=14)
        assert rsi == 100.0  # all gains, no losses

    def test_all_losses(self):
        # 15 samples with steadily decreasing prices
        samples = [OHLCVSample(close=200 - i, volume=1, ts=i) for i in range(16)]
        rsi = _compute_rsi(samples, period=14)
        assert rsi == 0.0  # all losses

    def test_equal_gains_losses(self):
        # alternating up/down of equal magnitude
        prices = []
        for i in range(16):
            prices.append(100 + (1 if i % 2 == 1 else 0))
        samples = [OHLCVSample(close=p, volume=1, ts=i) for i, p in enumerate(prices)]
        rsi = _compute_rsi(samples, period=14)
        # gains and losses should be roughly equal, RSI ~ 50
        assert rsi is not None
        assert 40 < rsi < 60

    def test_rsi_range(self):
        """RSI should always be in [0, 100]."""
        import random
        random.seed(42)
        prices = [100]
        for _ in range(50):
            prices.append(prices[-1] + random.uniform(-5, 5))
        samples = [OHLCVSample(close=p, volume=1, ts=i) for i, p in enumerate(prices)]
        rsi = _compute_rsi(samples, period=14)
        assert rsi is not None
        assert 0 <= rsi <= 100

    def test_custom_period(self):
        # Use period=5, need at least 6 samples
        samples = [OHLCVSample(close=100 + i * 2, volume=1, ts=i) for i in range(7)]
        rsi = _compute_rsi(samples, period=5)
        assert rsi == 100.0  # all gains

    def test_period_plus_one_minimum(self):
        # Exactly period+1 samples should work
        samples = [OHLCVSample(close=100 + i, volume=1, ts=i) for i in range(15)]
        rsi = _compute_rsi(samples, period=14)
        assert rsi is not None

        # period samples should not work
        samples = [OHLCVSample(close=100 + i, volume=1, ts=i) for i in range(14)]
        rsi = _compute_rsi(samples, period=14)
        assert rsi is None
