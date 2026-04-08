"""Tests for safe math utilities including rolling z-scores."""

import math
import pytest

from src.utils.safe_math import (
    safe_score, safe_ratio, RollingZScore, compute_zscore,
)


class TestSafeScore:
    def test_normal_clamp(self):
        assert safe_score(50) == 50
        assert safe_score(-5) == 0
        assert safe_score(110) == 100

    def test_nan_returns_lo(self):
        assert safe_score(float("nan")) == 0

    def test_inf_returns_lo(self):
        assert safe_score(float("inf")) == 0
        assert safe_score(float("-inf")) == 0

    def test_custom_bounds(self):
        assert safe_score(5, 10, 20) == 10
        assert safe_score(25, 10, 20) == 20


class TestSafeRatio:
    def test_normal(self):
        assert safe_ratio(1.5) == 1.5

    def test_nan(self):
        assert safe_ratio(float("nan")) == 0.0

    def test_inf(self):
        assert safe_ratio(float("inf")) == 0.0


class TestRollingZScore:
    def test_insufficient_data_returns_zero(self):
        rz = RollingZScore(window=100)
        for i in range(5):
            rz.push(float(i))
        assert rz.zscore() == 0.0

    def test_normal_distribution(self):
        rz = RollingZScore(window=100)
        # Push 100 values centered around 50 with std ~29
        for i in range(100):
            rz.push(float(i))
        # Mean = 49.5, std ≈ 28.87
        z = rz.zscore(49.5)  # mean -> z=0
        assert abs(z) < 0.01

        z_high = rz.zscore(78.37)  # ~1 std above mean
        assert 0.9 < z_high < 1.1

    def test_window_rolling(self):
        rz = RollingZScore(window=20)
        for i in range(50):
            rz.push(float(i))
        # Only last 20 values (30-49) should be in window
        assert rz.count == 20
        assert abs(rz.mean - 39.5) < 0.01

    def test_nan_values_ignored(self):
        rz = RollingZScore(window=100)
        for i in range(20):
            rz.push(float(i))
        rz.push(float("nan"))
        rz.push(float("inf"))
        assert rz.count == 20  # NaN and Inf not added

    def test_zero_std_returns_zero(self):
        rz = RollingZScore(window=100)
        for _ in range(20):
            rz.push(5.0)
        assert rz.zscore(5.0) == 0.0
        assert rz.zscore(10.0) == 0.0

    def test_properties(self):
        rz = RollingZScore(window=100)
        for i in range(10):
            rz.push(float(i))
        assert rz.count == 10
        assert abs(rz.mean - 4.5) < 0.01
        assert rz.std > 0


class TestComputeZscore:
    def test_normal(self):
        values = list(range(100))
        z = compute_zscore(values, 49.5)
        assert abs(z) < 0.01

    def test_insufficient_data(self):
        assert compute_zscore([1, 2, 3], 2) == 0.0

    def test_with_nan_values(self):
        values = list(range(20)) + [float("nan"), float("inf")]
        z = compute_zscore(values, 9.5)
        assert abs(z) < 0.01

    def test_extreme_zscore(self):
        values = [10.0] * 50
        values[0] = 10.1  # tiny variation
        z = compute_zscore(values, 100.0)
        assert z > 5  # far from mean
