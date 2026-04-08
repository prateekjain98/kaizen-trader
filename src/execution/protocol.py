"""Execution provider protocol — abstraction for multi-exchange support.

All exchange implementations (Coinbase, Binance, Paper) conform to this protocol.
The router selects which provider to use based on symbol and configuration.
"""

from typing import Protocol, runtime_checkable

from src.types import Trade


@runtime_checkable
class ExecutionProvider(Protocol):
    """Common interface for all exchange executors."""

    @property
    def name(self) -> str:
        """Exchange identifier (e.g. 'coinbase', 'binance', 'paper')."""
        ...

    def buy(self, symbol: str, product_id: str, size_usd: float,
            position_id: str, market_price: float) -> Trade:
        """Place a market buy order.

        Args:
            symbol: Base asset (e.g. "BTC")
            product_id: Exchange-specific pair (e.g. "BTC-USD", "BTCUSDT")
            size_usd: Dollar amount to spend
            position_id: Internal position ID for idempotency
            market_price: Current market price (used for paper + size calc)

        Returns:
            Trade with fill details
        """
        ...

    def sell(self, symbol: str, product_id: str, quantity: float,
             position_id: str, market_price: float) -> Trade:
        """Place a market sell order.

        Args:
            symbol: Base asset
            product_id: Exchange-specific pair
            quantity: Amount of base asset to sell
            position_id: Internal position ID
            market_price: Current market price

        Returns:
            Trade with fill details
        """
        ...

    def get_balances(self) -> dict[str, float]:
        """Return available balances by currency."""
        ...
