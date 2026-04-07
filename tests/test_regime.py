"""Tests for src/indicators/regime.py — Market regime detection."""

from unittest.mock import patch, MagicMock

import pytest

from src.indicators.core import IndicatorSnapshot
from src.indicators.regime import (
    classify_regime, RegimeSnapshot,
    is_trending, is_high_volatility, is_squeeze, get_regime_score,
    _classify_ema_alignment, _classify_wyckoff_phase, _compute_regime_score,
    _regime_cache, _lock,
)


def _reset():
    with _lock:
        _regime_cache.clear()


def _mock_snapshot(**overrides) -> IndicatorSnapshot:
    """Create a mock IndicatorSnapshot with defaults."""
    defaults = dict(
        symbol="BTC", ts=1000,
        atr_14=500, ema_20=51000, ema_50=50000, ema_200=48000,
        bb_upper=52000, bb_middle=50000, bb_lower=48000, bb_width=0.04,
        macd_line=100, macd_signal=80, macd_histogram=20,
        adx=30, plus_di=25, minus_di=15,
        obv=1000, rsi_14=55,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


class TestEMAAlignment:
    def test_bullish_alignment(self):
        snap = _mock_snapshot(ema_20=52000, ema_50=50000, ema_200=48000)
        assert _classify_ema_alignment(snap) == "bullish"

    def test_bearish_alignment(self):
        snap = _mock_snapshot(ema_20=46000, ema_50=48000, ema_200=50000)
        assert _classify_ema_alignment(snap) == "bearish"

    def test_neutral_without_ema200(self):
        snap = _mock_snapshot(ema_20=50500, ema_50=50000, ema_200=None)
        assert _classify_ema_alignment(snap) == "bullish"

    def test_neutral_without_data(self):
        snap = _mock_snapshot(ema_20=None, ema_50=None)
        assert _classify_ema_alignment(snap) == "neutral"


class TestWyckoffPhase:
    def test_accumulation(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", volatility="low_vol", rsi_zone="oversold")
        snap = _mock_snapshot()
        assert _classify_wyckoff_phase(regime, snap) == "accumulation"

    def test_markup(self):
        regime = RegimeSnapshot(ts=0, trend="trending_up", ema_alignment="bullish")
        snap = _mock_snapshot()
        assert _classify_wyckoff_phase(regime, snap) == "markup"

    def test_distribution(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", rsi_zone="overbought")
        snap = _mock_snapshot()
        assert _classify_wyckoff_phase(regime, snap) == "distribution"

    def test_markdown(self):
        regime = RegimeSnapshot(ts=0, trend="trending_down", ema_alignment="bearish")
        snap = _mock_snapshot()
        assert _classify_wyckoff_phase(regime, snap) == "markdown"


class TestRegimeScore:
    def test_bullish_regime_positive_score(self):
        regime = RegimeSnapshot(
            ts=0, trend="trending_up", ema_alignment="bullish",
            macd_signal="bullish", fear_greed=75,
        )
        snap = _mock_snapshot(rsi_14=60, obv=1000)
        score = _compute_regime_score(regime, snap)
        assert score > 0

    def test_bearish_regime_negative_score(self):
        regime = RegimeSnapshot(
            ts=0, trend="trending_down", ema_alignment="bearish",
            macd_signal="bearish", fear_greed=20,
        )
        snap = _mock_snapshot(rsi_14=35, obv=-500)
        score = _compute_regime_score(regime, snap)
        assert score < 0

    def test_score_clamped_to_range(self):
        regime = RegimeSnapshot(
            ts=0, trend="trending_up", ema_alignment="bullish",
            macd_signal="bullish", fear_greed=100,
        )
        snap = _mock_snapshot(rsi_14=95, obv=10000)
        score = _compute_regime_score(regime, snap)
        assert -100 <= score <= 100


class TestClassifyRegime:
    def setup_method(self):
        _reset()

    @patch("src.indicators.regime.get_snapshot")
    @patch("src.indicators.regime.fetch_fear_greed")
    def test_trending_up_market(self, mock_fg, mock_snap):
        mock_fg.return_value = None
        mock_snap.return_value = _mock_snapshot(adx=35, plus_di=30, minus_di=15)
        regime = classify_regime("BTC")
        assert regime.trend == "trending_up"
        assert regime.trend_strength == 35

    @patch("src.indicators.regime.get_snapshot")
    @patch("src.indicators.regime.fetch_fear_greed")
    def test_ranging_market(self, mock_fg, mock_snap):
        mock_fg.return_value = None
        mock_snap.return_value = _mock_snapshot(adx=15)
        regime = classify_regime("BTC")
        assert regime.trend == "ranging"

    @patch("src.indicators.regime.get_snapshot")
    @patch("src.indicators.regime.fetch_fear_greed")
    def test_bb_squeeze_detection(self, mock_fg, mock_snap):
        mock_fg.return_value = None
        mock_snap.return_value = _mock_snapshot(bb_width=0.02)
        regime = classify_regime("BTC")
        assert regime.bb_squeeze is True
        assert regime.volatility == "low_vol"

    @patch("src.indicators.regime.get_snapshot", return_value=None)
    @patch("src.indicators.regime.fetch_fear_greed", return_value=None)
    def test_unknown_without_data(self, mock_fg, mock_snap):
        regime = classify_regime("UNKNOWN")
        assert regime.trend == "unknown"


class TestConvenienceFunctions:
    def setup_method(self):
        _reset()

    @patch("src.indicators.regime.classify_regime")
    def test_is_trending(self, mock_classify):
        mock_classify.return_value = RegimeSnapshot(ts=0, trend="trending_up")
        assert is_trending("BTC") is True

    @patch("src.indicators.regime.classify_regime")
    def test_is_not_trending(self, mock_classify):
        mock_classify.return_value = RegimeSnapshot(ts=0, trend="ranging")
        assert is_trending("BTC") is False

    @patch("src.indicators.regime.classify_regime")
    def test_is_high_volatility(self, mock_classify):
        mock_classify.return_value = RegimeSnapshot(ts=0, volatility="high_vol")
        assert is_high_volatility("BTC") is True

    @patch("src.indicators.regime.classify_regime")
    def test_is_squeeze(self, mock_classify):
        mock_classify.return_value = RegimeSnapshot(ts=0, bb_squeeze=True)
        assert is_squeeze("BTC") is True

    @patch("src.indicators.regime.classify_regime")
    def test_get_regime_score(self, mock_classify):
        mock_classify.return_value = RegimeSnapshot(ts=0, regime_score=42.5)
        assert get_regime_score("BTC") == 42.5
