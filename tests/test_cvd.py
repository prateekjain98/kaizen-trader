"""Tests for src/indicators/cvd.py — Cumulative Volume Delta."""

import time
from unittest.mock import patch

import pytest

from src.indicators.cvd import (
    push_trade, get_cvd_snapshot, get_cvd, get_buy_sell_ratio,
    _tick_buffers, _snapshot_cache, _lock,
)


def _reset():
    with _lock:
        _tick_buffers.clear()
        _snapshot_cache.clear()


class TestPushTrade:
    def setup_method(self):
        _reset()

    def test_push_creates_buffer(self):
        push_trade("BTC", 50000, 1.0, "buy")
        with _lock:
            assert "BTC" in _tick_buffers
            assert len(_tick_buffers["BTC"]) == 1

    def test_push_accumulates_ticks(self):
        for i in range(10):
            push_trade("BTC", 50000 + i, 0.5, "buy" if i % 2 == 0 else "sell")
        with _lock:
            assert len(_tick_buffers["BTC"]) == 10


class TestCVDSnapshot:
    def setup_method(self):
        _reset()

    def test_returns_none_without_data(self):
        assert get_cvd_snapshot("UNKNOWN") is None

    def test_returns_none_with_too_few_ticks(self):
        push_trade("ETH", 3000, 1.0, "buy")
        assert get_cvd_snapshot("ETH") is None

    def test_buy_pressure_positive_cvd(self):
        for i in range(20):
            push_trade("BTC", 50000 + i, 1.0, "buy")
        snap = get_cvd_snapshot("BTC")
        assert snap is not None
        assert snap.cvd > 0

    def test_sell_pressure_negative_cvd(self):
        for i in range(20):
            push_trade("BTC", 50000 - i, 1.0, "sell")
        snap = get_cvd_snapshot("BTC")
        assert snap is not None
        assert snap.cvd < 0

    def test_snapshot_has_windowed_cvd(self):
        for i in range(20):
            push_trade("BTC", 50000, 1.0, "buy")
        snap = get_cvd_snapshot("BTC")
        assert snap is not None
        assert snap.cvd_1m >= 0
        assert snap.cvd_5m >= 0
        assert snap.cvd_15m >= 0


class TestGetCVD:
    def setup_method(self):
        _reset()

    def test_get_cvd_returns_none_without_data(self):
        assert get_cvd("UNKNOWN") is None

    def test_get_cvd_returns_float(self):
        for i in range(10):
            push_trade("SOL", 100, 1.0, "buy")
        cvd = get_cvd("SOL")
        assert cvd is not None
        assert isinstance(cvd, float)


class TestBuySellRatio:
    def setup_method(self):
        _reset()

    def test_returns_none_without_data(self):
        assert get_buy_sell_ratio("UNKNOWN") is None

    def test_all_buys_returns_none(self):
        # No sell volume → sell_volume_1m = 0 → returns None
        for i in range(10):
            push_trade("BTC", 50000, 1.0, "buy")
        ratio = get_buy_sell_ratio("BTC")
        # With zero sell volume, returns None (division guard)
        assert ratio is None

    def test_mixed_returns_ratio(self):
        for i in range(10):
            push_trade("ETH", 3000, 2.0, "buy")
        for i in range(10):
            push_trade("ETH", 3000, 1.0, "sell")
        ratio = get_buy_sell_ratio("ETH")
        assert ratio is not None
        assert ratio > 1  # more buy volume than sell
