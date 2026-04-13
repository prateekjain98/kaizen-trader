"""Execution router — selects the right exchange provider per symbol/config.

Routing logic:
1. If paper trading is enabled, always use PaperProvider.
2. Otherwise, route based on symbol → exchange mapping.
3. Default exchange is Coinbase (existing behavior).
"""

import threading
from typing import Optional

from src.config import env
from src.execution.protocol import ExecutionProvider
from src.execution.providers import CoinbaseProvider, PaperProvider, BinanceProvider
from src.execution.twap import TWAPExecutor, TWAPConfig
from src.storage.database import log
from src.types import Trade

_twap_config = TWAPConfig(threshold_usd=500, num_slices=3, interval_s=30)

# Route all supported symbols to Binance for execution
# (Coinbase WS is data-only; Binance is the execution venue)
_BINANCE_ONLY_SYMBOLS = {
    "BTC", "ETH", "SOL", "BNB", "AVAX", "LINK", "UNI", "AAVE",
    "ARB", "OP", "DOGE", "MATIC", "SUI", "APT", "SEI", "INJ",
    "FET", "TIA",
}

_lock = threading.Lock()
_providers: dict[str, ExecutionProvider] = {}
_exchange_overrides: dict[str, str] = {}  # symbol -> exchange name


def _get_provider(name: str) -> ExecutionProvider:
    """Lazy-initialize and cache providers."""
    with _lock:
        if name not in _providers:
            if name == "coinbase":
                _providers[name] = CoinbaseProvider()
            elif name == "binance":
                _providers[name] = BinanceProvider()
            elif name == "paper":
                _providers[name] = PaperProvider()
            else:
                raise ValueError(f"Unknown exchange provider: {name}")
        return _providers[name]


def set_exchange_override(symbol: str, exchange: str) -> None:
    """Override which exchange to use for a specific symbol."""
    with _lock:
        _exchange_overrides[symbol.upper()] = exchange
    log("info", f"Exchange override: {symbol} → {exchange}", symbol=symbol)


def get_exchange_overrides() -> dict[str, str]:
    """Return current exchange overrides."""
    with _lock:
        return dict(_exchange_overrides)


def _resolve_exchange(symbol: str) -> str:
    """Determine which exchange to route to for a given symbol."""
    sym = symbol.upper()

    # Check explicit overrides first
    with _lock:
        override = _exchange_overrides.get(sym)
    if override:
        return override

    # Binance-only symbols
    if sym in _BINANCE_ONLY_SYMBOLS:
        return "binance"

    # Default to Coinbase for everything else
    return "coinbase"


def get_provider(symbol: str) -> ExecutionProvider:
    """Get the appropriate execution provider for a symbol.

    If paper trading is enabled, always returns PaperProvider.
    Otherwise, routes to the correct exchange based on symbol.
    """
    if env.paper_trading:
        return _get_provider("paper")

    exchange = _resolve_exchange(symbol)
    return _get_provider(exchange)


def execute_buy(symbol: str, product_id: str, size_usd: float,
                position_id: str, market_price: float) -> Trade:
    """Route a buy order to the appropriate exchange."""
    provider = get_provider(symbol)
    log("info", f"Routing BUY {symbol} ${size_usd:.2f} via {provider.name}",
        symbol=symbol, data={"exchange": provider.name, "size_usd": size_usd})
    executor = TWAPExecutor(provider=provider, config=_twap_config)
    return executor.execute_buy(symbol, product_id, size_usd, position_id, market_price)


def execute_sell(symbol: str, product_id: str, quantity: float,
                 position_id: str, market_price: float) -> Trade:
    """Route a sell order to the appropriate exchange."""
    provider = get_provider(symbol)
    log("info", f"Routing SELL {symbol} {quantity:.8f} via {provider.name}",
        symbol=symbol, data={"exchange": provider.name, "quantity": quantity})
    return provider.sell(symbol, product_id, quantity, position_id, market_price)


def get_all_balances() -> dict[str, dict[str, float]]:
    """Get balances from all configured exchanges."""
    result = {}
    for name in ["paper"] if env.paper_trading else ["coinbase", "binance"]:
        try:
            provider = _get_provider(name)
            result[name] = provider.get_balances()
        except Exception as exc:
            log("warn", f"Failed to fetch balances from {name}: {exc}")
            result[name] = {}
    return result
