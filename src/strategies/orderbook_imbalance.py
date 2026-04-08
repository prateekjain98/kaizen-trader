"""Order Book Imbalance Strategy."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.types import TradeSignal, ScannerConfig


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    last_updated: float = 0


_order_books: dict[str, OrderBookSnapshot] = {}
_MAX_ORDER_BOOKS = 500


def update_order_book(symbol: str, bids: list[OrderBookLevel], asks: list[OrderBookLevel]) -> None:
    if symbol not in _order_books and len(_order_books) >= _MAX_ORDER_BOOKS:
        oldest_key = min(_order_books, key=lambda k: _order_books[k].last_updated)
        del _order_books[oldest_key]
    _order_books[symbol] = OrderBookSnapshot(
        bids=sorted(bids, key=lambda l: l.price, reverse=True),
        asks=sorted(asks, key=lambda l: l.price),
        last_updated=time.time() * 1000,
    )


def _sum_book_depth(levels: list[OrderBookLevel], from_price: float, price_pct: float) -> float:
    if from_price == 0:
        return 0.0
    return sum(
        l.size * l.price for l in levels
        if abs(l.price - from_price) / from_price <= price_pct
    )


def get_bid_ask_spread(symbol: str) -> Optional[float]:
    """Return the bid-ask spread as a fraction of mid price, or None if no book data.

    E.g., 0.005 means 0.5% spread.
    """
    book = _order_books.get(symbol)
    if not book or not book.bids or not book.asks:
        return None
    now = time.time() * 1000
    if now - book.last_updated > 30_000:
        return None  # stale book data
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2
    return (best_ask - best_bid) / mid


def scan_orderbook_imbalance(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig,
) -> Optional[TradeSignal]:
    book = _order_books.get(symbol)
    now = time.time() * 1000
    if not book or now - book.last_updated > 30_000:
        return None

    bid_depth = _sum_book_depth(book.bids, current_price, 0.01)
    ask_depth = _sum_book_depth(book.asks, current_price, 0.01)
    total_depth = bid_depth + ask_depth
    if total_depth < 500_000:
        return None

    imbalance_ratio = bid_depth / (ask_depth + 1)

    # Bid wall support
    if imbalance_ratio > 3.0 and bid_depth > 2_000_000:
        wall_score = min(30, imbalance_ratio * 5)
        size_score = min(20, bid_depth / 200_000)
        score = min(82, 42 + wall_score + size_score)
        if score >= config.min_qual_score_scalp:
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
                strategy="orderbook_imbalance", side="long", tier="scalp", score=score,
                confidence="low", sources=["orderbook"],
                reasoning=f"{symbol} bid/ask ratio {imbalance_ratio:.1f}x within 1%, ${bid_depth/1e6:.1f}M bid wall",
                entry_price=current_price, stop_price=current_price * 0.99,
                suggested_size_usd=40,
                expires_at=now + 300_000, created_at=now,
            )

    # Ask wall short
    if imbalance_ratio < 0.33 and ask_depth > 2_000_000:
        wall_score = min(25, (1 / (imbalance_ratio + 0.01)) * 3)
        size_score = min(20, ask_depth / 200_000)
        score = min(80, 40 + wall_score + size_score)
        if score >= config.min_qual_score_scalp:
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
                strategy="orderbook_imbalance", side="short", tier="scalp", score=score,
                confidence="low", sources=["orderbook"],
                reasoning=f"{symbol} ask/bid ratio {1/max(imbalance_ratio, 0.001):.1f}x within 1%, ${ask_depth/1e6:.1f}M ask wall",
                entry_price=current_price, stop_price=current_price * 1.01,
                suggested_size_usd=35,
                expires_at=now + 300_000, created_at=now,
            )

    return None
