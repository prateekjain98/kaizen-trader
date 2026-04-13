"""Tests for walk-forward backtesting."""
import pytest
from src.backtesting.walk_forward import (
    WalkForwardConfig,
    generate_windows,
)


class TestGenerateWindows:
    def test_generates_correct_number_of_windows(self):
        windows = generate_windows(
            start_ms=0,
            end_ms=120 * 86_400_000,
            train_days=30,
            test_days=7,
        )
        assert len(windows) >= 10
        assert len(windows) <= 15

    def test_windows_dont_overlap_test_periods(self):
        windows = generate_windows(
            start_ms=0,
            end_ms=90 * 86_400_000,
            train_days=30,
            test_days=7,
        )
        for i in range(len(windows) - 1):
            assert windows[i].test_end_ms <= windows[i + 1].test_start_ms

    def test_train_precedes_test(self):
        windows = generate_windows(
            start_ms=0,
            end_ms=90 * 86_400_000,
            train_days=30,
            test_days=7,
        )
        for w in windows:
            assert w.train_start_ms < w.train_end_ms
            assert w.train_end_ms == w.test_start_ms
            assert w.test_start_ms < w.test_end_ms

    def test_empty_if_insufficient_data(self):
        windows = generate_windows(
            start_ms=0,
            end_ms=20 * 86_400_000,
            train_days=30,
            test_days=7,
        )
        assert len(windows) == 0


class TestWalkForwardConfig:
    def test_default_values(self):
        cfg = WalkForwardConfig(
            symbols=["BTC-USD"],
            start_date="2025-01-01",
            end_date="2025-06-30",
        )
        assert cfg.train_days == 30
        assert cfg.test_days == 7
        assert cfg.initial_balance == 10000.0
