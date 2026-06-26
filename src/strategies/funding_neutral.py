"""Delta-neutral funding-rate capture (#1) — the market-neutral version of the
funding edge that professional desks actually run (vs. our directional
`funding_squeeze`).

Idea: when perp funding is large and positive, SHORT the perp and hold the same
notional LONG on spot. The legs cancel price exposure (delta-neutral) and you
collect the funding payment every 8h with no directional risk. (Negative funding
is the mirror — long perp / short spot — but short-spot needs borrow, so the
clean retail version targets positive funding.)

STATUS / SAFETY:
  * DEFAULT OFF. Controlled by env ENABLE_FUNDING_CARRY_NEUTRAL (default false).
  * Intentionally NOT named scan_*/on_* so the strategy registry does NOT
    auto-wire it into the directional signal→executor flow. It can never place a
    naked directional trade.
  * Live execution requires a Binance SPOT order leg, which the engine does not
    have yet (it is futures-only). Until that spot leg is built, this module only
    *identifies and surfaces* opportunities (the alpha-detection half). Wiring the
    hedged spot+perp execution + rebalancing/unwind is a separate, deliberate step.

This file is therefore a real, tested opportunity scanner + an explicit guard, so
the capability can be turned on safely once spot execution exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Binance pays funding 3×/day (every 8h).
FUNDING_PERIODS_PER_DAY = 3
# One-time round-trip cost to open AND close BOTH legs (perp + spot), taker:
# ~0.1% per fill × 4 fills.
ROUND_TRIP_BOTH_LEGS = 0.004
# Recommend an opportunity only if the gross annualized funding yield clears this
# and it pays back the entry/exit fees within a reasonable hold.
MIN_GROSS_APR = 0.08          # 8% APR floor
MAX_BREAKEVEN_DAYS = 10.0


# Hard execution gate — the edge analysis showed the funding is largest on the
# THINNEST perps (~$1M/24h, 0.2% spreads), where slippage eats the carry. Only
# trade names liquid enough to enter AND unwind both legs cleanly.
MIN_LIQUIDITY_VOL_USD = 50_000_000.0   # perp 24h quote volume floor
MAX_SPREAD_PCT = 0.05                   # top-of-book spread ceiling (%)


def is_enabled() -> bool:
    return os.getenv("ENABLE_FUNDING_CARRY_NEUTRAL", "false").lower() == "true"


def passes_liquidity_gate(vol_24h_usd: float, spread_pct: float,
                          min_vol: float = MIN_LIQUIDITY_VOL_USD,
                          max_spread: float = MAX_SPREAD_PCT) -> bool:
    """A carry candidate is tradeable only if the perp is deep enough and tight
    enough that round-trip slippage on both legs won't swamp the funding yield."""
    return vol_24h_usd >= min_vol and 0.0 <= spread_pct <= max_spread


@dataclass
class NeutralOpportunity:
    symbol: str
    funding_8h: float           # current funding rate per 8h (signed)
    perp_side: str              # "short" (capture +funding) or "long" (capture −funding)
    gross_daily_pct: float      # |funding_8h| × 3
    gross_apr: float            # gross_daily × 365
    breakeven_days: float       # days of funding to repay open+close fees on both legs
    recommended: bool


def find_funding_neutral_opportunities(
    funding_rates: dict[str, float],
    min_gross_apr: float = MIN_GROSS_APR,
    max_breakeven_days: float = MAX_BREAKEVEN_DAYS,
) -> list[NeutralOpportunity]:
    """Pure function: rank symbols by delta-neutral funding-capture attractiveness.

    `funding_rates` maps symbol -> current funding rate per 8h (e.g. 0.0005 = 0.05%).
    Returns opportunities sorted by gross APR, with `recommended` set when the
    yield clears the fee hurdle.
    """
    out: list[NeutralOpportunity] = []
    for symbol, f8h in funding_rates.items():
        if not f8h:
            continue
        gross_daily = abs(f8h) * FUNDING_PERIODS_PER_DAY
        if gross_daily <= 0:
            continue
        gross_apr = gross_daily * 365
        breakeven_days = ROUND_TRIP_BOTH_LEGS / gross_daily
        # Positive funding -> short the perp to RECEIVE it; negative -> long perp.
        perp_side = "short" if f8h > 0 else "long"
        recommended = gross_apr >= min_gross_apr and breakeven_days <= max_breakeven_days
        out.append(NeutralOpportunity(
            symbol=symbol, funding_8h=f8h, perp_side=perp_side,
            gross_daily_pct=gross_daily, gross_apr=gross_apr,
            breakeven_days=breakeven_days, recommended=recommended,
        ))
    out.sort(key=lambda o: o.gross_apr, reverse=True)
    return out
