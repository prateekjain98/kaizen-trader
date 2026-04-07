"""Liquidation Cascade Strategy."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.types import TradeSignal, ScannerConfig, MarketContext


@dataclass
class LiquidationEvent:
    symbol: str
    side: str  # 'buy' = short liq, 'sell' = long liq
    size_usd: float
    price: float
    ts: float


@dataclass
class LiquidationWindow:
    events: list[LiquidationEvent] = field(default_factory=list)
    total_long_liqs_usd: float = 0
    total_short_liqs_usd: float = 0
    oi_at_window_start: float = 0
    current_oi: float = 0
    window_start_price: float = 0


_windows: dict[str, LiquidationWindow] = {}
_MAX_WINDOWS = 500
_WINDOW_EXPIRY_MS = 1_800_000  # 30 minutes


def on_liquidation_event(event: LiquidationEvent, current_oi: float) -> None:
    now = time.time() * 1000

    # Expire stale windows and enforce max size
    if len(_windows) > _MAX_WINDOWS:
        stale = [k for k, w in _windows.items()
                 if not w.events or w.events[-1].ts < now - _WINDOW_EXPIRY_MS]
        for k in stale:
            del _windows[k]
        # If still over limit, drop oldest
        while len(_windows) > _MAX_WINDOWS:
            oldest_key = min(_windows, key=lambda k: _windows[k].events[-1].ts if _windows[k].events else 0)
            del _windows[oldest_key]

    if event.symbol not in _windows:
        _windows[event.symbol] = LiquidationWindow(
            oi_at_window_start=current_oi, current_oi=current_oi,
            window_start_price=event.price,
        )
    win = _windows[event.symbol]
    win.events.append(event)
    win.current_oi = current_oi

    if event.side == "sell":
        win.total_long_liqs_usd += event.size_usd
    else:
        win.total_short_liqs_usd += event.size_usd

    cutoff = now - 600_000
    win.events = [e for e in win.events if e.ts >= cutoff]
    win.total_long_liqs_usd = sum(e.size_usd for e in win.events if e.side == "sell")
    win.total_short_liqs_usd = sum(e.size_usd for e in win.events if e.side == "buy")


def scan_liquidation_cascade(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    win = _windows.get(symbol)
    if not win or len(win.events) < 3:
        return None
    now = time.time() * 1000

    oi_drop = ((win.oi_at_window_start - win.current_oi) / win.oi_at_window_start
               if win.oi_at_window_start > 0 else 0)

    # Strategy A: Ride cascade
    if (win.total_long_liqs_usd > 2_000_000 and oi_drop > 0.05
            and ctx.phase != "extreme_fear"):
        size_score = min(30, win.total_long_liqs_usd / 200_000)
        oi_score = min(20, oi_drop * 200)
        score = min(85, 45 + size_score + oi_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="liquidation_cascade", side="short", tier="scalp", score=score,
            confidence="medium" if score > 70 else "low",
            sources=["liquidation_data"],
            reasoning=f"{symbol} ${win.total_long_liqs_usd/1e6:.1f}M longs liq'd in 10m, OI down {oi_drop*100:.0f}%",
            entry_price=current_price, stop_price=current_price * 1.025,
            suggested_size_usd=50, expires_at=now + 600_000, created_at=now,
        )

    # Strategy B: Dip buy post-cascade
    price_drop_pct = (win.window_start_price - current_price) / win.window_start_price if win.window_start_price > 0 else 0
    cascade_exhausted = (oi_drop > 0.10 and win.events
                         and win.events[-1].ts < now - 120_000)

    if (win.total_long_liqs_usd > 5_000_000 and price_drop_pct > 0.08
            and cascade_exhausted and ctx.phase != "bear"):
        drop_score = min(30, price_drop_pct * 200)
        score = min(82, 52 + drop_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="liquidation_cascade", side="long", tier="swing", score=score,
            confidence="medium" if score > 72 else "low",
            sources=["liquidation_data"],
            reasoning=f"{symbol} down {price_drop_pct*100:.0f}% from cascade, ${win.total_long_liqs_usd/1e6:.1f}M liq'd, OI stabilizing",
            entry_price=current_price, stop_price=current_price * 0.97,
            suggested_size_usd=90, expires_at=now + 3_600_000, created_at=now,
        )

    return None
