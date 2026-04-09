"""Paper trading simulator."""

import random
import threading
import time
import uuid

from src.config import env
from src.storage.database import log
from src.types import Trade

_INITIAL_BALANCE = 10_000
_SLIPPAGE_BUY = 0.0005
_SLIPPAGE_SELL = 0.0003
_MIN_ORDER_USD = 1.0

_lock = threading.Lock()
_balance = _INITIAL_BALANCE
_holdings: dict[str, float] = {}


def _simulate_delay() -> None:
    time.sleep(0.05 + random.random() * 0.1)


def paper_buy(symbol: str, product_id: str, size_usd: float,
              position_id: str, market_price: float) -> Trade:
    global _balance
    _simulate_delay()

    # Price sanity check
    if market_price <= 0:
        log("error", f"Paper: invalid market_price={market_price} for {symbol}", symbol=symbol)
        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="buy",
            symbol=symbol, quantity=0, size_usd=0,
            price=0, status="failed", paper_trading=True,
            placed_at=time.time() * 1000, error="Invalid market_price <= 0",
        )

    # Order size validation
    if size_usd < _MIN_ORDER_USD:
        log("error", f"Paper: order size ${size_usd:.2f} below minimum ${_MIN_ORDER_USD:.2f}", symbol=symbol)
        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="buy",
            symbol=symbol, quantity=0, size_usd=0,
            price=0, status="failed", paper_trading=True,
            placed_at=time.time() * 1000, error=f"Order size below minimum ${_MIN_ORDER_USD}",
        )

    max_position_usd = env.max_position_usd
    if size_usd > max_position_usd:
        log("warn", f"Paper: order size ${size_usd:.2f} exceeds max ${max_position_usd:.2f}, capping",
            symbol=symbol)
        size_usd = max_position_usd

    with _lock:
        if size_usd > _balance:
            original_size = size_usd
            size_usd = _balance
            log("warn", f"Paper: insufficient balance, capped order from ${original_size:.2f} to ${size_usd:.2f}",
                symbol=symbol)

        # Negative balance protection
        if size_usd <= 0:
            log("error", f"Paper: zero balance, cannot buy {symbol}", symbol=symbol)
            return Trade(
                id=str(uuid.uuid4()), position_id=position_id, side="buy",
                symbol=symbol, quantity=0, size_usd=0,
                price=0, status="failed", paper_trading=True,
                placed_at=time.time() * 1000, error="Insufficient balance",
            )

        fill_price = market_price * (1 + _SLIPPAGE_BUY)
        commission = size_usd * env.commission_per_side
        net_size_usd = size_usd - commission
        quantity = net_size_usd / fill_price

        _balance -= size_usd
        _holdings[symbol] = _holdings.get(symbol, 0) + quantity
        balance_after = _balance

    log("trade",
        f"[PAPER] BUY {symbol} ${size_usd:.0f} @ {fill_price:.4f} (slip +{_SLIPPAGE_BUY*100:.2f}% fee {commission:.2f})",
        symbol=symbol,
        data={"fill_price": fill_price, "quantity": quantity, "commission": commission, "balance_after": balance_after})

    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side="buy",
        symbol=symbol, quantity=quantity, size_usd=size_usd,
        price=fill_price, status="paper", paper_trading=True,
        placed_at=time.time() * 1000, commission=commission,
    )


def paper_sell(symbol: str, product_id: str, quantity: float,
               position_id: str, market_price: float) -> Trade:
    global _balance
    _simulate_delay()

    # Price sanity check — division by zero guard
    if market_price <= 0:
        log("error", f"Paper: invalid market_price={market_price} for sell of {symbol}", symbol=symbol)
        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="sell",
            symbol=symbol, quantity=0, size_usd=0,
            price=0, status="failed", paper_trading=True,
            placed_at=time.time() * 1000, error="Invalid market_price <= 0",
        )

    if quantity <= 0:
        log("error", f"Paper: invalid sell quantity={quantity} for {symbol}", symbol=symbol)
        return Trade(
            id=str(uuid.uuid4()), position_id=position_id, side="sell",
            symbol=symbol, quantity=0, size_usd=0,
            price=0, status="failed", paper_trading=True,
            placed_at=time.time() * 1000, error="Invalid quantity <= 0",
        )

    with _lock:
        held = _holdings.get(symbol, 0)
        actual_qty = min(quantity, held)

        # Log warning if sell request exceeds holdings
        if quantity > held:
            log("warn",
                f"Paper: sell request {quantity:.8f} exceeds holdings {held:.8f} for {symbol}, "
                f"selling actual held amount {actual_qty:.8f}",
                symbol=symbol,
                data={"requested_qty": quantity, "held_qty": held, "actual_qty": actual_qty})

        if actual_qty <= 0:
            log("error", f"Paper: no holdings of {symbol} to sell", symbol=symbol)
            return Trade(
                id=str(uuid.uuid4()), position_id=position_id, side="sell",
                symbol=symbol, quantity=0, size_usd=0,
                price=0, status="failed", paper_trading=True,
                placed_at=time.time() * 1000, error="No holdings to sell",
            )

        fill_price = market_price * (1 - _SLIPPAGE_SELL)

        # Division by zero guard on fill_price
        if fill_price <= 0:
            log("error", f"Paper: computed fill_price={fill_price} <= 0 for {symbol}", symbol=symbol)
            return Trade(
                id=str(uuid.uuid4()), position_id=position_id, side="sell",
                symbol=symbol, quantity=0, size_usd=0,
                price=0, status="failed", paper_trading=True,
                placed_at=time.time() * 1000, error="Fill price <= 0",
            )

        gross_proceeds = actual_qty * fill_price
        commission = gross_proceeds * env.commission_per_side
        net_proceeds = gross_proceeds - commission

        _holdings[symbol] = held - actual_qty
        # Negative balance protection: only add non-negative proceeds
        if net_proceeds > 0:
            _balance += net_proceeds
        balance_after = _balance

    log("trade",
        f"[PAPER] SELL {symbol} {actual_qty:.6f} @ {fill_price:.4f} net ${net_proceeds:.2f} (slip -{_SLIPPAGE_SELL*100:.2f}%)",
        symbol=symbol,
        data={"fill_price": fill_price, "quantity": actual_qty, "commission": commission, "balance_after": balance_after})

    return Trade(
        id=str(uuid.uuid4()), position_id=position_id, side="sell",
        symbol=symbol, quantity=actual_qty, size_usd=net_proceeds,
        price=fill_price, status="paper", paper_trading=True,
        placed_at=time.time() * 1000, commission=commission,
    )


def get_paper_balance() -> float:
    with _lock:
        return _balance


def get_paper_holdings() -> dict[str, float]:
    with _lock:
        return dict(_holdings)


def reset_paper_account() -> None:
    global _balance
    with _lock:
        _balance = _INITIAL_BALANCE
        _holdings.clear()
