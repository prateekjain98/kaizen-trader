"""Tests for the cross-exchange divergence strategy."""

import time
from collections import deque
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import make_signal, _now_ms
from src.types import ScannerConfig, MarketContext


def _make_ctx(phase="neutral"):
    return MarketContext(
        phase=phase, btc_dominance=48.0, fear_greed_index=50,
        total_market_cap_change_d1=0.5, timestamp=_now_ms(),
    )


class TestPriceSnapshot:
    def test_record_with_binance_price(self):
        from src.strategies.cross_exchange_divergence import (
            record_price_snapshot, _divergence_history, _lock,
        )
        with _lock:
            _divergence_history.clear()

        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=95000.0):
            snap = record_price_snapshot("BTC", 95100.0)
            assert snap is not None
            assert snap.coinbase_price == 95100.0
            assert snap.binance_price == 95000.0
            assert abs(snap.divergence_pct - 0.1053) < 0.01

    def test_record_returns_none_without_binance(self):
        from src.strategies.cross_exchange_divergence import (
            record_price_snapshot, _divergence_history, _lock,
        )
        with _lock:
            _divergence_history.clear()

        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=None):
            snap = record_price_snapshot("BTC", 95000.0)
            assert snap is None

    def test_history_pruning(self):
        from src.strategies.cross_exchange_divergence import (
            record_price_snapshot, _divergence_history, _lock, PriceSnapshot,
        )
        with _lock:
            _divergence_history.clear()
            # Insert old entries manually
            old = deque(
                PriceSnapshot("BTC", 95000, 95000, 0.0, time.time() * 1000 - 7_200_000)
                for _ in range(50)
            )
            _divergence_history["BTC"] = old

        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=95000.0):
            record_price_snapshot("BTC", 95000.0)

        with _lock:
            # Old entries should be pruned (older than 1 hour)
            assert len(_divergence_history["BTC"]) == 1


class TestDivergenceStats:
    def _seed_history(self, symbol, divergences):
        from src.strategies.cross_exchange_divergence import (
            _divergence_history, _lock, PriceSnapshot,
        )
        now = time.time() * 1000
        with _lock:
            _divergence_history[symbol] = deque(
                PriceSnapshot(symbol, 95000 + d * 950, 95000, d, now - (len(divergences) - i) * 1000)
                for i, d in enumerate(divergences)
            )

    def test_insufficient_data_returns_none(self):
        from src.strategies.cross_exchange_divergence import (
            _get_divergence_stats, _divergence_history, _lock,
        )
        with _lock:
            _divergence_history.clear()
        assert _get_divergence_stats("BTC") is None

    def test_stats_with_data(self):
        from src.strategies.cross_exchange_divergence import _get_divergence_stats
        # 10 points around 0% divergence, then current at 0.5%
        divs = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.02, 0.01, 0.50]
        self._seed_history("TEST", divs)
        stats = _get_divergence_stats("TEST")
        assert stats is not None
        assert stats["current_div_pct"] == 0.50
        assert stats["sample_count"] == 10
        assert stats["z_score"] > 0  # current is well above average

    def test_zero_std_returns_none(self):
        from src.strategies.cross_exchange_divergence import _get_divergence_stats
        # All identical divergences
        divs = [0.01] * 15
        self._seed_history("FLAT", divs)
        stats = _get_divergence_stats("FLAT")
        assert stats is None  # zero std


class TestScanCrossExchange:
    def _seed_history(self, symbol, divergences):
        from src.strategies.cross_exchange_divergence import (
            _divergence_history, _lock, PriceSnapshot,
        )
        now = time.time() * 1000
        with _lock:
            _divergence_history[symbol] = deque(
                PriceSnapshot(symbol, 95000 + d * 950, 95000, d, now - (len(divergences) - i) * 1000)
                for i, d in enumerate(divergences)
            )

    def test_no_signal_for_unmapped_symbol(self):
        from src.strategies.cross_exchange_divergence import scan_cross_exchange_divergence
        result = scan_cross_exchange_divergence(
            "OBSCURE", "OBSCURE-USD", 1.0, ScannerConfig(), _make_ctx(),
        )
        assert result is None

    def test_no_signal_with_insufficient_history(self):
        from src.strategies.cross_exchange_divergence import (
            scan_cross_exchange_divergence, _divergence_history, _lock,
        )
        with _lock:
            _divergence_history.clear()

        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=95000.0):
            result = scan_cross_exchange_divergence(
                "BTC", "BTC-USD", 95000.0, ScannerConfig(), _make_ctx(),
            )
            assert result is None

    def test_long_signal_when_coinbase_underpriced(self):
        from src.strategies.cross_exchange_divergence import scan_cross_exchange_divergence
        # History: normally near 0, now Coinbase is below Binance
        divs = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.02, 0.01, -0.01]
        self._seed_history("BTC", divs)

        # Current: Coinbase at 94700, Binance at 95000 → -0.32% div
        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=95000.0):
            result = scan_cross_exchange_divergence(
                "BTC", "BTC-USD", 94700.0, ScannerConfig(), _make_ctx(),
            )
            if result is not None:
                assert result.side == "long"
                assert result.strategy == "cross_exchange_divergence"
                assert result.tier == "scalp"

    def test_short_signal_when_coinbase_overpriced(self):
        from src.strategies.cross_exchange_divergence import scan_cross_exchange_divergence
        divs = [0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.02, 0.01, -0.01]
        self._seed_history("ETH", divs)

        # Current: Coinbase at 2010, Binance at 2000 → +0.5% div
        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=2000.0):
            result = scan_cross_exchange_divergence(
                "ETH", "ETH-USD", 2010.0, ScannerConfig(), _make_ctx(),
            )
            if result is not None:
                assert result.side == "short"
                assert result.strategy == "cross_exchange_divergence"

    def test_no_signal_when_divergence_small(self):
        from src.strategies.cross_exchange_divergence import scan_cross_exchange_divergence
        # All very small divergences
        divs = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01, 0.02]
        self._seed_history("SOL", divs)

        # Current: very close prices
        with patch("src.strategies.cross_exchange_divergence._fetch_binance_price", return_value=150.0):
            result = scan_cross_exchange_divergence(
                "SOL", "SOL-USD", 150.05, ScannerConfig(), _make_ctx(),
            )
            assert result is None  # divergence too small


class TestGetDivergenceStats:
    def test_returns_all_tracked(self):
        from src.strategies.cross_exchange_divergence import (
            get_divergence_stats, _divergence_history, _lock, PriceSnapshot,
        )
        now = time.time() * 1000
        with _lock:
            _divergence_history.clear()
            for sym in ["BTC", "ETH"]:
                _divergence_history[sym] = [
                    PriceSnapshot(sym, 100 + i * 0.1, 100, i * 0.1, now - i * 1000)
                    for i in range(15)
                ]

        stats = get_divergence_stats()
        assert "BTC" in stats
        assert "ETH" in stats
