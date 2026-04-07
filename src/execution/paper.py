"""Paper trading simulator."""

import random
import time
import uuid

from src.storage.database import log
from src.types import Trade

_INITIAL_BALANCE = 10_000
_SLIPPAGE_BUY = 0.0005
_SLIPPAGE_SELL = 0.0003
_COMMISSION = 0.006

_balance = _INITIAL_BALANCE
_holdings: dict[str, float] = {}


def _simulate_delay() -> None:
    time.sleep(0.05 + random.random() * 0.1)


def paper_buy(symbol: str, product_id: str, size_usd: float,
              position_id: str, market_price: float) -> Trade:
    global _balance
    _simulate_delay()

    if size_usd > _balance:
        size_usd = _balance
        log("warn", f"Paper: insufficient balance, capped order to ${size_usd:.2f}", symbol=symbol)

    fill_price = market_price * (1 + _SLIPPAGE_BUY)
    commission = size_usd * _COMMISSION
    net_size_usd = size_usd - commission
    quantity = net_size_usd / fill_price

    _balance -= size_usd
    _holdings[symbol] = _holdings.get(symbol, 0) + quantity

    log("trade",
        f"[PAPER] BUY {symbol} ${size_usd:.0f} @ {fill_price:.4f} (slip +{_SLIPPAGE_BUY*100:.2f}% fee {commission:.2f})",
        symbol=symbol,
        data={"fill_price": fill_price, "quantity": quantity, "commission": commission, "balance_after": _balance})

    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side="buy",
        symbol=symbol, quantity=quantity, size_usd=size_usd,
        price=fill_price, status="paper", paper_trading=True,
        placed_at=time.time() * 1000,
    )


def paper_sell(symbol: str, product_id: str, quantity: float,
               position_id: str, market_price: float) -> Trade:
    global _balance
    _simulate_delay()

    held = _holdings.get(symbol, 0)
    actual_qty = min(quantity, held)

    fill_price = market_price * (1 - _SLIPPAGE_SELL)
    gross_proceeds = actual_qty * fill_price
    commission = gross_proceeds * _COMMISSION
    net_proceeds = gross_proceeds - commission

    _holdings[symbol] = held - actual_qty
    _balance += net_proceeds

    log("trade",
        f"[PAPER] SELL {symbol} {actual_qty:.6f} @ {fill_price:.4f} net ${net_proceeds:.2f} (slip -{_SLIPPAGE_SELL*100:.2f}%)",
        symbol=symbol,
        data={"fill_price": fill_price, "quantity": actual_qty, "commission": commission, "balance_after": _balance})

    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side="sell",
        symbol=symbol, quantity=actual_qty, size_usd=net_proceeds,
        price=fill_price, status="paper", paper_trading=True,
        placed_at=time.time() * 1000,
    )


def get_paper_balance() -> float:
    return _balance


def get_paper_holdings() -> dict[str, float]:
    return dict(_holdings)


def reset_paper_account() -> None:
    global _balance
    _balance = _INITIAL_BALANCE
    _holdings.clear()
