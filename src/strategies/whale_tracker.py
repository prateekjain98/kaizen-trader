"""Whale Accumulation/Distribution Strategy."""

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal

_WINDOW_MS = 7_200_000
_MIN_ALERT_SIZE_USD = 3_000_000


@dataclass
class NetFlowState:
    inflow_to_exchange: float = 0
    outflow_from_exchange: float = 0
    last_ts: float = 0


_flow_windows: dict[str, NetFlowState] = {}
_MAX_FLOW_SYMBOLS = 500


def on_whale_transfer(tx: dict) -> None:
    if tx["amount_usd"] < _MIN_ALERT_SIZE_USD:
        return
    now = time.time() * 1000
    symbol = tx["symbol"]

    if symbol not in _flow_windows:
        # Evict stale entries to prevent unbounded growth
        if len(_flow_windows) >= _MAX_FLOW_SYMBOLS:
            oldest_key = min(_flow_windows, key=lambda k: _flow_windows[k].last_ts)
            del _flow_windows[oldest_key]
        _flow_windows[symbol] = NetFlowState(last_ts=now)
    state = _flow_windows[symbol]

    if now - state.last_ts > _WINDOW_MS:
        state.inflow_to_exchange = 0
        state.outflow_from_exchange = 0
    state.last_ts = now

    if tx["to_type"] == "exchange":
        state.inflow_to_exchange += tx["amount_usd"]
    elif tx["from_type"] == "exchange" or tx["to_type"] == "unknown_wallet":
        state.outflow_from_exchange += tx["amount_usd"]


def scan_whale_accumulation(
    symbol: str, product_id: str, current_price: float,
) -> Optional[TradeSignal]:
    state = _flow_windows.get(symbol)
    if not state:
        return None
    now = time.time() * 1000

    net = state.outflow_from_exchange - state.inflow_to_exchange
    total_flow = state.outflow_from_exchange + state.inflow_to_exchange
    if total_flow < _MIN_ALERT_SIZE_USD:
        return None
    net_ratio = net / total_flow

    # Accumulation
    if net_ratio > 0.4 and state.outflow_from_exchange > 5_000_000:
        flow_score = min(30, state.outflow_from_exchange / 1_000_000)
        ratio_score = min(20, net_ratio * 20)
        score = min(88, 45 + flow_score + ratio_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="whale_accumulation", side="long", tier="swing", score=score,
            confidence="medium" if score > 70 else "low",
            sources=["whale_alert"],
            reasoning=f"{symbol} ${state.outflow_from_exchange/1e6:.0f}M net outflow from exchanges in 2h",
            entry_price=current_price, stop_price=current_price * 0.93,
            suggested_size_usd=100,
            expires_at=now + 21_600_000, created_at=now,
        )

    # Distribution
    if net_ratio < -0.5 and state.inflow_to_exchange > 10_000_000:
        flow_score = min(25, state.inflow_to_exchange / 1_000_000)
        ratio_score = min(20, -net_ratio * 20)
        score = min(84, 40 + flow_score + ratio_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="whale_accumulation", side="short", tier="swing", score=score,
            confidence="medium" if score > 68 else "low",
            sources=["whale_alert"],
            reasoning=f"{symbol} ${state.inflow_to_exchange/1e6:.0f}M whale inflows to exchanges in 2h",
            entry_price=current_price, stop_price=current_price * 1.04,
            suggested_size_usd=80,
            expires_at=now + 14_400_000, created_at=now,
        )

    return None


def get_net_exchange_flow() -> dict:
    """Aggregate exchange flows across all tracked symbols.

    Returns:
        Dict with 'net_flow_usd' (positive = outflow/bullish, negative = inflow/bearish),
        'total_inflow', 'total_outflow', 'symbols_tracked'.
    """
    now = time.time() * 1000
    total_inflow = 0.0
    total_outflow = 0.0
    active_symbols = 0

    for sym, state in _flow_windows.items():
        if now - state.last_ts > _WINDOW_MS:
            continue  # stale data
        total_inflow += state.inflow_to_exchange
        total_outflow += state.outflow_from_exchange
        active_symbols += 1

    net = total_outflow - total_inflow
    return {
        "net_flow_usd": net,
        "total_inflow_usd": total_inflow,
        "total_outflow_usd": total_outflow,
        "symbols_tracked": active_symbols,
    }
