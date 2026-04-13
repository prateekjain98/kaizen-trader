"""TWAP (Time-Weighted Average Price) execution.

Splits large orders into time-sliced chunks to reduce market impact.
Orders below the threshold execute as a single market order.
"""
from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import Optional

from src.execution.protocol import ExecutionProvider
from src.types import Trade


logger = logging.getLogger(__name__)


@dataclass
class TWAPConfig:
    threshold_usd: float = 500
    num_slices: int = 3
    max_slices: int = 5
    min_slice_usd: float = 50
    interval_s: float = 30


def compute_twap_slices(
    size_usd: float,
    config: TWAPConfig = TWAPConfig(),
) -> list[float]:
    """Compute slice sizes for a TWAP order."""
    if size_usd <= config.threshold_usd:
        return [size_usd]

    n = min(config.num_slices, config.max_slices)
    while n > 1 and (size_usd / n) < config.min_slice_usd:
        n -= 1

    slice_size = size_usd / n
    slices = [slice_size] * n

    remainder = size_usd - sum(slices)
    if remainder != 0:
        slices[-1] += remainder

    return slices


class TWAPExecutor:
    """Wraps an execution provider with TWAP slicing."""

    def __init__(self, provider: ExecutionProvider, config: TWAPConfig = TWAPConfig()):
        self._provider = provider
        self._config = config

    def execute_buy(
        self,
        symbol: str,
        product_id: str,
        size_usd: float,
        position_id: str,
        market_price: float,
    ) -> Trade:
        """Execute a buy order, potentially sliced via TWAP."""
        slices = compute_twap_slices(size_usd, self._config)

        if len(slices) == 1:
            return self._provider.buy(symbol, product_id, size_usd, position_id, market_price)

        logger.info(
            "TWAP: splitting $%.0f %s buy into %d slices over %ds",
            size_usd, symbol, len(slices), self._config.interval_s * (len(slices) - 1),
        )

        total_qty = 0.0
        total_cost = 0.0
        total_commission = 0.0
        last_trade: Optional[Trade] = None

        for i, slice_usd in enumerate(slices):
            trade = self._provider.buy(symbol, product_id, slice_usd, position_id, market_price)
            total_qty += trade.quantity
            total_cost += trade.quantity * trade.price
            total_commission += trade.commission
            last_trade = trade

            if i < len(slices) - 1 and self._config.interval_s > 0:
                time.sleep(self._config.interval_s)

        avg_price = total_cost / total_qty if total_qty > 0 else market_price

        return Trade(
            id=last_trade.id if last_trade else "",
            position_id=position_id,
            side="buy",
            symbol=symbol,
            quantity=total_qty,
            size_usd=size_usd,
            price=avg_price,
            status="filled",
            paper_trading=last_trade.paper_trading if last_trade else True,
            placed_at=last_trade.placed_at if last_trade else time.time() * 1000,
            commission=total_commission,
        )

    def execute_sell(
        self,
        symbol: str,
        product_id: str,
        quantity: float,
        position_id: str,
        market_price: float,
    ) -> Trade:
        """Execute a sell order (sells are not sliced - exits should be fast)."""
        return self._provider.sell(symbol, product_id, quantity, position_id, market_price)
