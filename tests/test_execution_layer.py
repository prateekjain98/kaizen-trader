"""Tests for the execution abstraction layer: protocol, providers, router."""

import time
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import _now_ms


# ─── ExecutionProvider Protocol ────────────────────────────────────────────────

class TestExecutionProtocol:
    def test_paper_provider_is_protocol_compliant(self):
        from src.execution.protocol import ExecutionProvider
        from src.execution.providers import PaperProvider
        provider = PaperProvider()
        assert isinstance(provider, ExecutionProvider)

    def test_coinbase_provider_is_protocol_compliant(self):
        from src.execution.protocol import ExecutionProvider
        from src.execution.providers import CoinbaseProvider
        provider = CoinbaseProvider()
        assert isinstance(provider, ExecutionProvider)

    def test_binance_provider_is_protocol_compliant(self):
        from src.execution.protocol import ExecutionProvider
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        assert isinstance(provider, ExecutionProvider)

    def test_provider_names(self):
        from src.execution.providers import PaperProvider, CoinbaseProvider, BinanceProvider
        assert PaperProvider().name == "paper"
        assert CoinbaseProvider().name == "coinbase"
        assert BinanceProvider().name == "binance"


# ─── PaperProvider ─────────────────────────────────────────────────────────────

class TestPaperProvider:
    def test_buy_and_sell(self):
        from src.execution.providers import PaperProvider
        from src.execution.paper import reset_paper_account
        reset_paper_account()

        provider = PaperProvider()
        trade = provider.buy("ETH", "ETH-USD", 100.0, "pos-1", 2000.0)
        assert trade.status == "paper"
        assert trade.quantity > 0
        assert trade.price > 0

        sell_trade = provider.sell("ETH", "ETH-USD", trade.quantity, "pos-1", 2000.0)
        assert sell_trade.status == "paper"
        assert sell_trade.quantity > 0

    def test_get_balances_includes_usd(self):
        from src.execution.providers import PaperProvider
        from src.execution.paper import reset_paper_account
        reset_paper_account()

        provider = PaperProvider()
        balances = provider.get_balances()
        assert "USD" in balances
        assert balances["USD"] == 10_000  # initial paper balance


# ─── BinanceProvider ───────────────────────────────────────────────────────────

class TestBinanceProvider:
    def test_buy_fails_without_credentials(self):
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        with patch("src.config.env") as mock_env:
            mock_env.binance_api_key = None
            mock_env.binance_api_secret = None
            trade = provider.buy("BTC", "BTCUSDT", 100.0, "pos-1", 95000.0)
            assert trade.status == "failed"
            assert "not configured" in trade.error

    def test_sell_fails_without_credentials(self):
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        with patch("src.config.env") as mock_env:
            mock_env.binance_api_key = None
            mock_env.binance_api_secret = None
            trade = provider.sell("BTC", "BTCUSDT", 0.001, "pos-1", 95000.0)
            assert trade.status == "failed"

    def test_buy_invalid_size(self):
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        with patch("src.config.env") as mock_env:
            mock_env.binance_api_key = "test"
            mock_env.binance_api_secret = "test"
            trade = provider.buy("BTC", "BTCUSDT", -100.0, "pos-1", 95000.0)
            assert trade.status == "failed"

    def test_sell_invalid_quantity(self):
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        with patch("src.config.env") as mock_env:
            mock_env.binance_api_key = "test"
            mock_env.binance_api_secret = "test"
            trade = provider.sell("BTC", "BTCUSDT", 0, "pos-1", 95000.0)
            assert trade.status == "failed"

    def test_symbol_mapping(self):
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        assert provider._get_binance_symbol("BTC") == "BTCUSDT"
        assert provider._get_binance_symbol("eth") == "ETHUSDT"
        assert provider._get_binance_symbol("UNKNOWN") == "UNKNOWNUSDT"

    def test_get_balances_without_creds_returns_empty(self):
        from src.execution.providers import BinanceProvider
        provider = BinanceProvider()
        with patch("src.config.env") as mock_env:
            mock_env.binance_api_key = None
            mock_env.binance_api_secret = None
            balances = provider.get_balances()
            assert balances == {}


# ─── Router ────────────────────────────────────────────────────────────────────

class TestRouter:
    def _reset_router(self):
        from src.execution.router import _lock, _providers, _exchange_overrides
        with _lock:
            _providers.clear()
            _exchange_overrides.clear()

    def test_paper_mode_always_returns_paper(self):
        from src.execution.router import get_provider
        self._reset_router()
        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = True
            provider = get_provider("BTC")
            assert provider.name == "paper"

    def test_live_mode_routes_to_coinbase_by_default(self):
        from src.execution.router import get_provider
        self._reset_router()
        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = False
            provider = get_provider("BTC")
            assert provider.name == "coinbase"

    def test_binance_only_symbols(self):
        from src.execution.router import get_provider
        self._reset_router()
        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = False
            provider = get_provider("INJ")
            assert provider.name == "binance"

    def test_exchange_override(self):
        from src.execution.router import get_provider, set_exchange_override
        self._reset_router()
        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = False
            set_exchange_override("ETH", "binance")
            provider = get_provider("ETH")
            assert provider.name == "binance"

    def test_get_exchange_overrides(self):
        from src.execution.router import set_exchange_override, get_exchange_overrides
        self._reset_router()
        set_exchange_override("SOL", "binance")
        overrides = get_exchange_overrides()
        assert overrides == {"SOL": "binance"}

    def test_execute_buy_routes_correctly(self):
        from src.execution.router import execute_buy
        from src.execution.paper import reset_paper_account
        self._reset_router()
        reset_paper_account()

        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = True
            trade = execute_buy("ETH", "ETH-USD", 100.0, "pos-1", 2000.0)
            assert trade.paper_trading is True
            assert trade.status == "paper"

    def test_execute_sell_routes_correctly(self):
        from src.execution.router import execute_buy, execute_sell
        from src.execution.paper import reset_paper_account
        self._reset_router()
        reset_paper_account()

        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = True
            buy_trade = execute_buy("ETH", "ETH-USD", 100.0, "pos-1", 2000.0)
            sell_trade = execute_sell("ETH", "ETH-USD", buy_trade.quantity, "pos-1", 2000.0)
            assert sell_trade.paper_trading is True

    def test_get_all_balances_paper_mode(self):
        from src.execution.router import get_all_balances
        from src.execution.paper import reset_paper_account
        self._reset_router()
        reset_paper_account()

        with patch("src.execution.router.env") as mock_env:
            mock_env.paper_trading = True
            balances = get_all_balances()
            assert "paper" in balances
            assert balances["paper"]["USD"] == 10_000
