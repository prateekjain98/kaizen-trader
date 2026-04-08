"""DCA scaling-in — enter swing positions in tranches.

Instead of going all-in at entry, swing positions enter in up to 3 tranches:
- Tranche 1: 50% at signal price
- Tranche 2: 25% if price retraces 1-2% from entry (better average)
- Tranche 3: 25% if price moves 1% in our favor (confirmation)

Scalp positions always enter in a single tranche.
"""

import time
from typing import Optional

from src.types import Position
from src.storage.database import log


# Tranche schedule: (fraction_of_total, trigger_type, trigger_pct)
# trigger_type: "immediate" (enter now), "retrace" (buy dip), "confirm" (buy strength)
_SWING_TRANCHES = [
    (0.50, "immediate", 0.0),
    (0.25, "retrace", -0.015),  # 1.5% retrace from entry
    (0.25, "confirm", 0.010),   # 1% move in our favor
]


def get_initial_fraction(tier: str) -> float:
    """Get the fraction of total size for the initial entry."""
    if tier == "scalp":
        return 1.0
    return _SWING_TRANCHES[0][0]  # 50% for swing


def get_max_tranches(tier: str) -> int:
    """Get the max number of tranches for a tier."""
    return 1 if tier == "scalp" else len(_SWING_TRANCHES)


def should_add_tranche(pos: Position, current_price: float) -> Optional[dict]:
    """Check if we should add another tranche to this position.

    Args:
        pos: Current open position.
        current_price: Latest market price.

    Returns:
        Dict with 'fraction' and 'reason' if a tranche should be added, None otherwise.
    """
    if pos.tranche_count >= pos.max_tranches:
        return None
    if pos.tier == "scalp":
        return None
    if pos.entry_price <= 0:
        return None

    tranche_idx = pos.tranche_count  # 0-indexed next tranche
    if tranche_idx >= len(_SWING_TRANCHES):
        return None

    fraction, trigger_type, trigger_pct = _SWING_TRANCHES[tranche_idx]

    if trigger_type == "retrace":
        # For longs: buy when price drops below entry by trigger_pct
        # For shorts: buy when price rises above entry by |trigger_pct|
        if pos.side == "long":
            target = pos.entry_price * (1 + trigger_pct)  # trigger_pct is negative
            if current_price <= target:
                return {
                    "fraction": fraction,
                    "reason": f"retrace tranche: price {current_price:.4f} <= {target:.4f}",
                }
        else:
            target = pos.entry_price * (1 - trigger_pct)
            if current_price >= target:
                return {
                    "fraction": fraction,
                    "reason": f"retrace tranche: price {current_price:.4f} >= {target:.4f}",
                }

    elif trigger_type == "confirm":
        # For longs: buy when price rises above entry by trigger_pct (confirmation)
        # For shorts: buy when price drops below entry by trigger_pct
        if pos.side == "long":
            target = pos.entry_price * (1 + trigger_pct)
            if current_price >= target:
                return {
                    "fraction": fraction,
                    "reason": f"confirmation tranche: price {current_price:.4f} >= {target:.4f}",
                }
        else:
            target = pos.entry_price * (1 - trigger_pct)
            if current_price <= target:
                return {
                    "fraction": fraction,
                    "reason": f"confirmation tranche: price {current_price:.4f} <= {target:.4f}",
                }

    return None


def compute_tranche_size_usd(pos: Position, tranche_fraction: float) -> float:
    """Compute USD size for a DCA tranche, capped at 50% of current position size."""
    per_tranche = pos.size_usd / pos.tranche_count
    normalized = tranche_fraction / (1 / pos.max_tranches)
    tranche_usd = per_tranche * normalized
    return min(tranche_usd, pos.size_usd * 0.5)
