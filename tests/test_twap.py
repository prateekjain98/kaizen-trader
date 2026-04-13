"""Tests for TWAP execution."""
import pytest
from unittest.mock import MagicMock
from src.execution.twap import TWAPConfig, compute_twap_slices, TWAPExecutor


class TestComputeSlices:
    def test_small_order_single_slice(self):
        slices = compute_twap_slices(size_usd=200, config=TWAPConfig())
        assert len(slices) == 1
        assert slices[0] == pytest.approx(200)

    def test_large_order_multiple_slices(self):
        slices = compute_twap_slices(size_usd=800, config=TWAPConfig(threshold_usd=500))
        assert len(slices) >= 2
        assert sum(slices) == pytest.approx(800)

    def test_slices_are_roughly_equal(self):
        slices = compute_twap_slices(size_usd=1000, config=TWAPConfig(
            threshold_usd=500, num_slices=4,
        ))
        assert len(slices) == 4
        for s in slices:
            assert s == pytest.approx(250, abs=1)

    def test_max_slices_capped(self):
        slices = compute_twap_slices(size_usd=5000, config=TWAPConfig(
            threshold_usd=100, num_slices=5, max_slices=5,
        ))
        assert len(slices) <= 5

    def test_each_slice_above_minimum(self):
        slices = compute_twap_slices(size_usd=600, config=TWAPConfig(
            threshold_usd=500, num_slices=10, min_slice_usd=100,
        ))
        for s in slices:
            assert s >= 100


class TestTWAPExecutor:
    def test_execute_below_threshold_calls_provider_once(self):
        provider = MagicMock()
        provider.buy.return_value = MagicMock(
            status="filled", quantity=0.01, price=40000,
            commission=0.04, position_id="p1", placed_at=1000.0,
        )
        executor = TWAPExecutor(provider=provider, config=TWAPConfig(threshold_usd=500))

        result = executor.execute_buy(
            symbol="BTC", product_id="BTC-USD",
            size_usd=200, position_id="p1", market_price=40000,
        )
        assert provider.buy.call_count == 1

    def test_execute_above_threshold_calls_provider_multiple_times(self):
        provider = MagicMock()
        provider.buy.return_value = MagicMock(
            status="filled", quantity=0.01, price=40000,
            commission=0.04, position_id="p1", placed_at=1000.0,
        )
        executor = TWAPExecutor(provider=provider, config=TWAPConfig(
            threshold_usd=500, num_slices=3, interval_s=0,
        ))

        result = executor.execute_buy(
            symbol="BTC", product_id="BTC-USD",
            size_usd=900, position_id="p1", market_price=40000,
        )
        assert provider.buy.call_count == 3
