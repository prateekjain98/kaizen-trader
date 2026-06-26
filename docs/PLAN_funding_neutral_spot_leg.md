# Delta-neutral funding carry — spot-hedge leg + execution

**Goal:** let `funding_neutral` run the *real* market-neutral carry (SHORT perp +
LONG spot, equal notional) instead of a naked directional short. Default-OFF
behind `ENABLE_FUNDING_CARRY_NEUTRAL=false`. Paper-first; live only after review.

**Why:** in the current regime the only strong funding edges are *positive*-funding
perps (e.g. SKHYNIX +0.52%/8h ≈ 579% APR). A naked short harvests funding but eats
full price risk. Hedging the price exposure with an equal long-spot position makes
the funding income the only P&L driver — the version professional desks run.

## Phases

### Phase 1 — Spot execution provider  (no live risk: paper + mocked tests)
`src/execution/spot_providers.py :: BinanceSpotProvider`
- Hits `https://api.binance.com/api/v3` (spot), HMAC-signed (mirror futures provider).
- `place_spot_market(symbol, side, quantity|quote_qty)`, `get_spot_balances()`.
- Spot exchangeInfo LOT_SIZE / MIN_NOTIONAL cache + `_round_step` (reuse logic).
- No leverage, no reduceOnly (spot sells are bounded by held balance).
- Fully unit-tested with mocked `requests`.

### Phase 2 — Delta-neutral position manager  (paper-simulated, tested)
`src/execution/neutral_carry.py :: NeutralCarryManager`
- Input: a `NeutralOpportunity` from `funding_neutral.find_funding_neutral_opportunities`.
- Open: SHORT perp (fapi) + BUY spot (api/v3) of equal USD notional.
- **THE safety invariant:** never hold one leg naked. If leg A fills and leg B
  fails, immediately unwind leg A. One filled leg with no hedge = abort+flatten.
- Track paired position (both order ids, both fills, entry basis), persist to state.
- Monitor: hedge-ratio drift, accrued funding, unwind trigger (funding decays
  below floor, or max hold, or basis blowout).
- Unwind: close perp (reduceOnly) + sell spot; record realized funding P&L.

### Phase 3 — Wiring + hard guards
- Runs only when `is_enabled()` (env flag, default false).
- Liquidity gate: perp vol > $50M AND spread < 0.05% (kills the $1M stock-perps).
- Caps: max 1–2 concurrent neutral positions, per-position notional ≤ MAX_POSITION_USD.
- Hook into runner on the funding cadence; surfaces to Convex like other positions.

### Phase 4 — Live enablement  (gated on user review of paper results)
- Run paper for N funding cycles, review realized vs modeled funding + slippage.
- Only then flip `ENABLE_FUNDING_CARRY_NEUTRAL=true` on prod.

## Non-goals / risk notes
- Short-spot (negative funding, long-perp side) needs margin borrow — out of scope;
  positive-funding (short-perp / long-spot) only, matching the scanner.
- Spot and perp LOT_SIZE differ → residual tiny delta is expected; bound it, don't
  chase zero.
