"""Tests for strategy auto-discovery registry."""

import pytest

from src.strategies.registry import (
    discover_strategies, get_registry, get_scan_functions,
    STRATEGY_REGISTRY, StrategyEntry,
)


class TestDiscovery:
    def test_discovers_all_strategies(self):
        registry = discover_strategies()
        # We expect at least the 11 known scan/on functions
        assert len(registry) >= 11

    def test_each_entry_has_callable(self):
        registry = discover_strategies()
        for sid, entry in registry.items():
            assert callable(entry.scan_function), f"{sid} scan_function is not callable"

    def test_known_strategies_present(self):
        registry = discover_strategies()
        # Check a few known strategy IDs by function name pattern
        func_names = {e.scan_function.__name__ for e in registry.values()}
        assert "scan_momentum" in func_names
        assert "scan_mean_reversion" in func_names
        assert "scan_funding_extreme" in func_names
        assert "scan_fear_greed_contrarian" in func_names

    def test_skips_init_and_registry(self):
        registry = discover_strategies()
        module_names = {e.module_name for e in registry.values()}
        assert "src.strategies.__init__" not in module_names
        assert "src.strategies.registry" not in module_names


class TestGetRegistry:
    def test_returns_populated(self):
        registry = get_registry()
        assert len(registry) >= 11

    def test_idempotent(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2


class TestGetScanFunctions:
    def test_returns_list_of_callables(self):
        funcs = get_scan_functions()
        assert len(funcs) >= 11
        for f in funcs:
            assert callable(f)


class TestBackwardCompatibility:
    def test_import_from_strategies_package(self):
        from src.strategies import scan_momentum
        assert callable(scan_momentum)

    def test_import_from_strategies_package_mean_reversion(self):
        from src.strategies import scan_mean_reversion
        assert callable(scan_mean_reversion)
