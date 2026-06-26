"""Delta-neutral funding-carry position manager.

Pairs a SHORT perp leg with an equal-notional LONG spot leg so price exposure
cancels and the funding payment is the only P&L driver. This is the safe,
professional version of the funding edge — vs. a naked directional short.

SAFETY INVARIANT (the reason this class exists):
    Never hold a single naked leg. Opening is two sequential fills; if the
    second leg fails after the first filled, the first is immediately unwound.
    A half-open hedge is worse than no trade — it's an unmanaged directional
    position the rest of the engine doesn't know about.

DEFAULT-OFF: only constructed/invoked by the funding_neutral wiring, which is
gated behind ENABLE_FUNDING_CARRY_NEUTRAL (default false). In paper mode both
legs are simulated and no provider is required.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.storage.database import log
from src.strategies.funding_neutral import NeutralOpportunity


@dataclass
class NeutralPosition:
    id: str
    symbol: str
    perp_side: str            # "short" (positive-funding capture)
    notional_usd: float
    perp_qty: float
    spot_qty: float
    entry_perp_price: float
    entry_spot_price: float
    funding_8h_at_entry: float
    opened_at: float = field(default_factory=lambda: time.time())
    perp_order_id: Optional[str] = None
    spot_order_id: Optional[str] = None

    def hold_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600.0


class NeutralCarryManager:
    """Opens, tracks and unwinds delta-neutral funding-carry positions.

    perp_provider must expose:
        open_short(symbol, position_id, quantity, market_price) -> Trade
        close_short(symbol, position_id, quantity, market_price) -> Trade
    spot_provider must expose:
        place_spot_market(symbol, position_id, side, quantity, market_price) -> Trade
    In paper mode both are unused (fills are simulated).
    """

    def __init__(self, paper: bool = True, perp_provider=None, spot_provider=None,
                 max_notional_usd: float = 45.0):
        self.paper = paper
        self.perp = perp_provider
        self.spot = spot_provider
        self.max_notional_usd = max_notional_usd
        self.positions: list[NeutralPosition] = []

    # ------------------------------------------------------------------ open
    def open(self, opp: NeutralOpportunity, notional_usd: float,
             mark_price: float) -> Optional[NeutralPosition]:
        if opp.perp_side != "short":
            # Negative-funding (long-perp / short-spot) needs margin borrow on
            # spot — deliberately out of scope. See PLAN doc.
            log("info", f"[neutral] {opp.symbol}: perp_side={opp.perp_side} unsupported (short-only)")
            return None
        if notional_usd <= 0 or notional_usd > self.max_notional_usd:
            log("warn", f"[neutral] {opp.symbol}: notional ${notional_usd:.2f} outside "
                        f"(0, ${self.max_notional_usd}] — rejected")
            return None
        if mark_price <= 0:
            return None

        pid = f"neutral_{opp.symbol}_{uuid.uuid4().hex[:8]}"
        qty = notional_usd / mark_price

        if self.paper:
            pos = NeutralPosition(
                id=pid, symbol=opp.symbol, perp_side="short", notional_usd=notional_usd,
                perp_qty=qty, spot_qty=qty, entry_perp_price=mark_price,
                entry_spot_price=mark_price, funding_8h_at_entry=opp.funding_8h,
                perp_order_id="paper", spot_order_id="paper",
            )
            self.positions.append(pos)
            log("info", f"[neutral][paper] OPEN {opp.symbol} short-perp+long-spot "
                        f"${notional_usd:.2f} @ {mark_price} (funding {opp.funding_8h*100:+.3f}%/8h)")
            return pos

        # --- LIVE: leg 1, SHORT the perp -------------------------------------
        perp_trade = self.perp.open_short(opp.symbol, pid, qty, mark_price)
        if getattr(perp_trade, "status", "") != "filled":
            log("warn", f"[neutral] {opp.symbol}: perp short failed "
                        f"({getattr(perp_trade, 'error', '?')}) — aborting, nothing opened")
            return None

        perp_qty = float(getattr(perp_trade, "quantity", qty) or qty)
        perp_px = float(getattr(perp_trade, "price", mark_price) or mark_price)

        # --- LIVE: leg 2, BUY equal spot -------------------------------------
        spot_trade = self.spot.place_spot_market(opp.symbol, pid, "BUY", perp_qty, perp_px)
        if getattr(spot_trade, "status", "") != "filled":
            # CRITICAL: spot failed but perp is live and naked. Flatten it now.
            log("error", f"[neutral] {opp.symbol}: spot BUY failed after perp short filled — "
                         f"UNWINDING perp to avoid naked leg")
            try:
                self.perp.close_short(opp.symbol, pid, perp_qty, perp_px)
            except Exception as exc:  # noqa: BLE001
                log("error", f"[neutral] {opp.symbol}: FAILED to unwind naked perp leg: {exc} "
                             f"— MANUAL INTERVENTION REQUIRED")
            return None

        pos = NeutralPosition(
            id=pid, symbol=opp.symbol, perp_side="short", notional_usd=notional_usd,
            perp_qty=perp_qty, spot_qty=float(getattr(spot_trade, "quantity", perp_qty) or perp_qty),
            entry_perp_price=perp_px,
            entry_spot_price=float(getattr(spot_trade, "price", perp_px) or perp_px),
            funding_8h_at_entry=opp.funding_8h,
            perp_order_id=getattr(perp_trade, "order_id", None),
            spot_order_id=getattr(spot_trade, "order_id", None),
        )
        self.positions.append(pos)
        log("info", f"[neutral] OPEN {opp.symbol} short-perp+long-spot ${notional_usd:.2f} "
                    f"perp@{perp_px} spot@{pos.entry_spot_price} (funding {opp.funding_8h*100:+.3f}%/8h)")
        return pos

    # ---------------------------------------------------------------- unwind
    def unwind(self, pos: NeutralPosition, mark_price: float) -> bool:
        """Close both legs: buy back the perp, sell the spot. Returns True on
        success. Drops the position from tracking either way (a failed unwind is
        logged loudly for manual handling)."""
        if self.paper:
            if pos in self.positions:
                self.positions.remove(pos)
            log("info", f"[neutral][paper] UNWIND {pos.symbol} after {pos.hold_hours():.1f}h")
            return True

        ok = True
        perp_trade = self.perp.close_short(pos.symbol, pos.id, pos.perp_qty, mark_price)
        if getattr(perp_trade, "status", "") != "filled":
            ok = False
            log("error", f"[neutral] {pos.symbol}: perp close failed on unwind — MANUAL CHECK")
        spot_trade = self.spot.place_spot_market(pos.symbol, pos.id, "SELL", pos.spot_qty, mark_price)
        if getattr(spot_trade, "status", "") != "filled":
            ok = False
            log("error", f"[neutral] {pos.symbol}: spot sell failed on unwind — MANUAL CHECK")

        if pos in self.positions:
            self.positions.remove(pos)
        log("info", f"[neutral] UNWIND {pos.symbol} after {pos.hold_hours():.1f}h ok={ok}")
        return ok
