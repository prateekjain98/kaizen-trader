"""TWAP (Time-Weighted Average Price) execution.

Splits large orders into time-sliced chunks to reduce market impact.
Orders below the threshold execute as a single market order.
"""
from __future__ import annotations

import threading
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
    if abs(remainder) > 1e-9:
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

        # Execute first slice immediately so caller gets a trade back without blocking
        first_trade = self._provider.buy(symbol, product_id, slices[0], position_id, market_price)

        if len(slices) > 1:
            remaining = slices[1:]
            t = threading.Thread(
                target=self._execute_remaining_slices,
                args=(symbol, product_id, remaining, position_id, market_price),
                daemon=True,
            )
            t.start()

        return first_trade

    def _execute_remaining_slices(
        self,
        symbol: str,
        product_id: str,
        slices: list[float],
        position_id: str,
        market_price: float,
    ) -> None:
        """Execute remaining TWAP slices in background."""
        for i, slice_usd in enumerate(slices):
            if self._config.interval_s > 0:
                time.sleep(self._config.interval_s)
            try:
                trade = self._provider.buy(symbol, product_id, slice_usd, position_id, market_price)
                logger.info(
                    "TWAP slice %d/%d filled: %s %.8f @ %.2f",
                    i + 2, len(slices) + 1, symbol, trade.quantity, trade.price,
                )
            except Exception:
                logger.exception("TWAP slice %d/%d failed for %s", i + 2, len(slices) + 1, symbol)

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
