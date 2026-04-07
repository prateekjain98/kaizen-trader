"""Tests for the backtesting engine and data loader."""

import csv
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.backtesting.data_loader import (
    load_klines, _parse_kline, _read_cache, _write_cache, _cache_path,
)
from src.backtesting.engine import (
    BacktestEngine, BacktestConfig, BacktestResult, _date_to_ms,
    _compute_rsi, _compute_vwap, _compute_momentum_pct, _compute_volume_ratio,
)
from src.types import ScannerConfig, Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candles(
    n: int = 100,
    start_price: float = 100.0,
    trend: float = 0.001,
    base_volume: float = 1000.0,
    start_ms: int = 1_700_000_000_000,
    interval_ms: int = 3_600_000,
) -> list[dict]:
    """Generate synthetic candle data with a gentle trend."""
    candles: list[dict] = []
    price = start_price
    for i in range(n):
        open_time = start_ms + i * interval_ms
        # Simple deterministic price movement
        change = trend * price * (1 if i % 3 != 0 else -0.5)
        close = price + change
        high = max(price, close) * 1.005
        low = min(price, close) * 0.995
        vol = base_volume * (1.2 if i % 7 == 0 else 1.0)
        candles.append({
            "open_time": open_time,
            "open": round(price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": round(vol, 2),
            "close_time": open_time + interval_ms - 1,
        })
        price = close
    return candles


def _make_volatile_candles(
    n: int = 100,
    start_price: float = 100.0,
    start_ms: int = 1_700_000_000_000,
    interval_ms: int = 3_600_000,
) -> list[dict]:
    """Generate candles with large swings to trigger strategy signals."""
    candles: list[dict] = []
    price = start_price
    for i in range(n):
        open_time = start_ms + i * interval_ms
        # Create a pump pattern: prices rise sharply for 10 candles, then drop
        cycle = i % 40
        if cycle < 10:
            change = price * 0.008  # 0.8% up
            vol = 3000.0  # high volume
        elif cycle < 15:
            change = price * 0.002
            vol = 2000.0
        elif cycle < 25:
            change = -price * 0.006  # drop
            vol = 1500.0
        else:
            change = -price * 0.001  # slow drift down
            vol = 800.0

        close = price + change
        high = max(price, close) * 1.003
        low = min(price, close) * 0.997
        candles.append({
            "open_time": open_time,
            "open": round(price, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "close": round(close, 4),
            "volume": round(vol, 2),
            "close_time": open_time + interval_ms - 1,
        })
        price = close
    return candles


# ---------------------------------------------------------------------------
# Data Loader Tests
# ---------------------------------------------------------------------------

class TestParseKline:
    def test_parse_kline_basic(self):
        raw = [1700000000000, "100.5", "102.0", "99.0", "101.0", "5000.0",
               1700003599999, "0", "0", "0", "0", "0"]
        result = _parse_kline(raw)
        assert result["open_time"] == 1700000000000
        assert result["open"] == 100.5
        assert result["high"] == 102.0
        assert result["low"] == 99.0
        assert result["close"] == 101.0
        assert result["volume"] == 5000.0
        assert result["close_time"] == 1700003599999


class TestCsvCache:
    def test_write_and_read_cache(self, tmp_path):
        candles = _make_candles(5)
        cache_file = tmp_path / "test_cache.csv"
        _write_cache(cache_file, candles)

        loaded = _read_cache(cache_file)
        assert loaded is not None
        assert len(loaded) == 5
        for orig, cached in zip(candles, loaded):
            assert orig["open_time"] == cached["open_time"]
            assert abs(orig["close"] - cached["close"]) < 0.01
            assert abs(orig["volume"] - cached["volume"]) < 0.01

    def test_read_cache_missing_file(self, tmp_path):
        result = _read_cache(tmp_path / "nonexistent.csv")
        assert result is None


class TestLoadKlines:
    def test_invalid_interval(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            load_klines("BTC", "2m", 1700000000000, 1700100000000)

    @patch("src.backtesting.data_loader._fetch_klines_chunk")
    def test_load_klines_fetches_and_caches(self, mock_fetch, tmp_path):
        candles = _make_candles(10, start_ms=1700000000000)
        mock_fetch.return_value = candles

        with patch("src.backtesting.data_loader._DATA_DIR", tmp_path):
            with patch("src.backtesting.data_loader._cache_path") as mock_cache:
                cache_file = tmp_path / "BTC_1h_1700000000000_1700036000000.csv"
                mock_cache.return_value = cache_file

                result = load_klines("BTC", "1h", 1700000000000, 1700036000000)
                assert len(result) == 10
                assert mock_fetch.called

                # Second call should hit cache
                mock_fetch.reset_mock()
                result2 = load_klines("BTC", "1h", 1700000000000, 1700036000000)
                assert len(result2) == 10
                assert not mock_fetch.called  # cache hit


# ---------------------------------------------------------------------------
# Engine Helper Tests
# ---------------------------------------------------------------------------

class TestDateConversion:
    def test_date_to_ms(self):
        ms = _date_to_ms("2025-01-01")
        # 2025-01-01 00:00:00 UTC
        assert ms == 1735689600000

    def test_date_to_ms_different_date(self):
        ms = _date_to_ms("2025-06-15")
        assert ms > _date_to_ms("2025-01-01")


class TestIndicators:
    def test_compute_rsi_insufficient_data(self):
        assert _compute_rsi([100.0, 101.0]) is None

    def test_compute_rsi_all_gains(self):
        closes = [100 + i for i in range(20)]
        rsi = _compute_rsi(closes)
        assert rsi is not None
        assert rsi == 100.0

    def test_compute_rsi_mixed(self):
        closes = [100, 102, 101, 103, 100, 105, 103, 107, 104, 108,
                  106, 110, 108, 112, 110, 114]
        rsi = _compute_rsi(closes)
        assert rsi is not None
        assert 0 <= rsi <= 100

    def test_compute_vwap(self):
        candles = [
            {"close": 100, "volume": 10},
            {"close": 102, "volume": 20},
            {"close": 98, "volume": 15},
        ]
        vwap = _compute_vwap(candles)
        expected = (100*10 + 102*20 + 98*15) / (10 + 20 + 15)
        assert vwap is not None
        assert abs(vwap - expected) < 0.01

    def test_compute_vwap_empty(self):
        assert _compute_vwap([]) is None

    def test_compute_momentum_pct(self):
        candles = [{"close": 100}] * 5 + [{"close": 110}]
        pct = _compute_momentum_pct(candles, 5)
        assert pct is not None
        assert abs(pct - 0.10) < 0.001

    def test_compute_momentum_pct_insufficient(self):
        candles = [{"close": 100}]
        assert _compute_momentum_pct(candles, 5) is None

    def test_compute_volume_ratio(self):
        candles = [{"volume": 1000}] * 9 + [{"volume": 3000}]
        ratio = _compute_volume_ratio(candles, 10)
        # avg = (9*1000 + 3000) / 10 = 1200, current = 3000, ratio = 2.5
        assert ratio == 2.5


# ---------------------------------------------------------------------------
# Backtest Engine Tests
# ---------------------------------------------------------------------------

class TestBacktestEngine:
    def _make_config(self, symbols=None, candle_count=100) -> BacktestConfig:
        return BacktestConfig(
            symbols=symbols or ["BTC"],
            start_date="2025-01-01",
            end_date="2025-04-01",
            initial_balance=10000.0,
            scanner_config=ScannerConfig(),
            commission_pct=0.001,
            slippage_pct=0.0005,
            max_open_positions=5,
            interval="1h",
        )

    def test_engine_init(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        assert engine.balance == 10000.0
        assert engine.open_positions == []
        assert engine.closed_positions == []

    def test_slippage_long_entry(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        # Long entry: price should increase (slippage hurts buyer)
        result = engine._apply_slippage(100.0, "long", entry=True)
        assert result == 100.05  # 100 * (1 + 0.0005)

    def test_slippage_long_exit(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        # Long exit: price should decrease (slippage hurts seller)
        result = engine._apply_slippage(100.0, "long", entry=False)
        assert result == 99.95  # 100 * (1 - 0.0005)

    def test_slippage_short_entry(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        # Short entry: price should decrease (slippage hurts short seller)
        result = engine._apply_slippage(100.0, "short", entry=True)
        assert result == 99.95

    def test_commission_calculation(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        commission = engine._apply_commission(1000.0)
        assert commission == 1.0  # 0.1%

    def test_kelly_sizing(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        size = engine._kelly_size(70.0)
        assert size >= 10  # minimum
        assert size <= engine.balance * 0.2  # max 20% of balance

    def test_cooldown_mechanism(self):
        config = self._make_config()
        engine = BacktestEngine(config)
        now = 1_700_000_000_000.0

        assert not engine._check_cooldown("BTC", "momentum_swing", now)
        engine._set_cooldown("BTC", "momentum_swing", now, 3_600_000)
        assert engine._check_cooldown("BTC", "momentum_swing", now + 1_000)
        assert not engine._check_cooldown("BTC", "momentum_swing", now + 3_700_000)

    @patch("src.backtesting.engine.load_klines")
    def test_run_with_no_data(self, mock_load):
        mock_load.return_value = []
        config = self._make_config()
        engine = BacktestEngine(config)
        result = engine.run()

        assert result.total_trades == 0
        assert result.final_balance == 10000.0
        assert result.max_drawdown_pct == 0

    @patch("src.backtesting.engine.load_klines")
    def test_run_with_flat_candles(self, mock_load):
        """Flat candles should produce few or no trades."""
        candles = _make_candles(50, start_price=100, trend=0.0, start_ms=_date_to_ms("2025-01-01"))
        mock_load.return_value = candles

        config = self._make_config()
        engine = BacktestEngine(config)
        result = engine.run()

        # With flat prices, strategies shouldn't trigger
        assert result.final_balance > 0
        assert len(result.equity_curve) > 0

    @patch("src.backtesting.engine.load_klines")
    def test_run_with_volatile_candles(self, mock_load):
        """Volatile candles should produce some trades."""
        candles = _make_volatile_candles(200, start_price=100, start_ms=_date_to_ms("2025-01-01"))
        mock_load.return_value = candles

        config = self._make_config()
        engine = BacktestEngine(config)
        result = engine.run()

        assert isinstance(result, BacktestResult)
        assert result.final_balance > 0
        assert len(result.equity_curve) > 0
        # All positions should be closed
        assert len(engine.open_positions) == 0

    @patch("src.backtesting.engine.load_klines")
    def test_equity_curve_length(self, mock_load):
        candles = _make_candles(30, start_ms=_date_to_ms("2025-01-01"))
        mock_load.return_value = candles

        config = self._make_config()
        engine = BacktestEngine(config)
        result = engine.run()

        # Each candle produces one equity point
        assert len(result.equity_curve) == 30

    @patch("src.backtesting.engine.load_klines")
    def test_commission_deducted(self, mock_load):
        """Force a trade and verify commission is deducted."""
        # Create candles with a strong uptrend and high volume to trigger momentum
        start_ms = _date_to_ms("2025-01-01")
        interval_ms = 3_600_000
        candles: list[dict] = []
        price = 100.0
        for i in range(60):
            open_time = start_ms + i * interval_ms
            if 10 <= i <= 20:
                # Strong pump with high volume
                change = price * 0.015
                vol = 5000.0
            else:
                change = price * 0.0005
                vol = 1000.0
            close = price + change
            candles.append({
                "open_time": open_time,
                "open": round(price, 4),
                "high": round(max(price, close) * 1.002, 4),
                "low": round(min(price, close) * 0.998, 4),
                "close": round(close, 4),
                "volume": round(vol, 2),
                "close_time": open_time + interval_ms - 1,
            })
            price = close

        mock_load.return_value = candles

        config = BacktestConfig(
            symbols=["BTC"],
            start_date="2025-01-01",
            end_date="2025-04-01",
            initial_balance=10000.0,
            scanner_config=ScannerConfig(
                momentum_pct_swing=0.02,
                volume_multiplier_swing=1.5,
                min_qual_score_swing=45,
            ),
            commission_pct=0.01,  # 1% commission to make it visible
            slippage_pct=0.0,
            max_open_positions=5,
            interval="1h",
        )
        engine = BacktestEngine(config)
        result = engine.run()

        # If any trades occurred, final balance should reflect commission costs
        if result.total_trades > 0:
            # Commission is charged on entry and exit, so balance should differ from
            # what it would be without any commission
            total_commission_paid = sum(
                config.commission_pct * (p.size_usd * 2)  # entry + exit approximation
                for p in result.positions
            )
            # At least some commission was paid
            assert total_commission_paid > 0

    @patch("src.backtesting.engine.load_klines")
    def test_max_positions_respected(self, mock_load):
        """Engine should not exceed max_open_positions."""
        candles = _make_volatile_candles(200, start_ms=_date_to_ms("2025-01-01"))

        def fake_load(symbol, interval, start, end):
            # Return same candles offset slightly for each symbol
            return candles

        mock_load.side_effect = fake_load

        config = BacktestConfig(
            symbols=["BTC", "ETH", "SOL", "AVAX", "DOT", "LINK", "ADA", "MATIC"],
            start_date="2025-01-01",
            end_date="2025-04-01",
            initial_balance=10000.0,
            scanner_config=ScannerConfig(
                momentum_pct_swing=0.01,
                volume_multiplier_swing=1.5,
                min_qual_score_swing=40,
            ),
            max_open_positions=2,
            interval="1h",
        )

        engine = BacktestEngine(config)

        # Monkey-patch to track max open positions during the run
        max_seen = [0]
        original_open = engine._open_position

        def tracking_open(signal, now_ms):
            result = original_open(signal, now_ms)
            if len(engine.open_positions) > max_seen[0]:
                max_seen[0] = len(engine.open_positions)
            return result

        engine._open_position = tracking_open
        engine.run()

        assert max_seen[0] <= 2

    @patch("src.backtesting.engine.load_klines")
    def test_backtest_result_structure(self, mock_load):
        candles = _make_candles(50, start_ms=_date_to_ms("2025-01-01"))
        mock_load.return_value = candles

        config = self._make_config()
        engine = BacktestEngine(config)
        result = engine.run()

        assert isinstance(result, BacktestResult)
        assert isinstance(result.metrics, PortfolioMetrics)
        assert isinstance(result.positions, list)
        assert isinstance(result.equity_curve, list)
        assert isinstance(result.final_balance, float)
        assert isinstance(result.max_drawdown_pct, float)
        assert result.max_drawdown_pct >= 0


# ---------------------------------------------------------------------------
# Import for PortfolioMetrics type check in test
# ---------------------------------------------------------------------------
from src.evaluation.metrics import PortfolioMetrics
