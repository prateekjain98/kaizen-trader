# Parallel Trader Insights

This file is APPENDED to by parallel paper-trader agent passes. Each entry is
timestamped and self-contained. Read the most recent entries when planning
the next change to the main bot's rule_brain or signal_detector.

## CONFIG STATUS (read before suggesting strategy enables)

Strategies disabled in prod by env-var gate (do NOT suggest enabling without
fresh n>=30 + ROBUST t-test verdict per scripts/run_live_backtest.py):
- `FUNDING_CARRY_ENABLED=0` — prior +$5.53 backtest was fabricated by 3
  compounding bugs (re-entry cooldown sim-time clock, filter-chain bypass,
  exit-attribution wick-vs-carry-pnl). See data_streams.py:757-767.
- `LIQUIDATION_CASCADE_ENABLED=0` — sweep at multiple thresholds yields
  n=6 max events over 90d on 8 majors. INSUFFICIENT for verdict.
- `OB_IMBALANCE_ENABLED=0` — historical L2 depth not available, OOS
  validation impossible. See data_streams.py:967-971.

Per-symbol cooldown (4h after 2 losses) and per-strategy cooldown (30min
after 3 losses) ARE wired and active — see commits 4c3fa60 + a94404a.

MIN_SCORE_TO_TRADE = 60 (was 40 pre-2026-05-03). RuleBrain only;
ClaudeBrain not currently active in prod (no ANTHROPIC_API_KEY on VM).

`_CARRY_LIQUID_UNIVERSE` (rule_brain.py:84) covers 27 majors — funding_carry
signals on coins outside this set score 0 on the carry-rank bonus even when
the env gate is flipped. Pass 2's BUSDT recommendation falls in this hole.

---

## 2026-05-03 10:50 UTC — Parallel Trader Pass
### Market snapshot
- BTC: $78,370.70 (24h: +0.18%) — dead-flat chop. 24h kline range $78,028–$79,145 (~1.4% intraday band, no trend).
- FGI: 47 (Neutral) — no contrarian extreme on either side; FGI bonus (+30 BTC/ETH) does NOT fire.
- Funding regime: mildly greedy. Across 147 USDT-perp pairs with vol > $10M: 59% positive funding, median +0.0041%, mean -0.0066% (mean dragged negative by KNC outlier). No broad short-squeeze setup.
- Top mover: LABUSDT -31.33% on $3.66B volume (single-name capitulation, not regime). Up-side: TSTUSDT +52.7%, BABYUSDT +42.2%, BUSDT +32.9% — concentrated in low-cap memecoins, not a sector rotation.

### Signals scored ≥60 (would-trade under main bot rules)
| symbol | signal_type | score | factors | suggested_side |
| ------ | ----------- | ----- | ------- | -------------- |
| _none_ | — | — | No funding-derived candidate clears MIN_SCORE_TO_TRADE=60 from this pass's data alone. The +50 mega-accel bonus is the missing piece, and 1h kline pulls weren't done for the full universe in this budget. | — |

### Signals scored 40–59 (marginal — would benefit from MIN_SCORE relaxation OR a confirming 1h accel)
- **KNCUSDT**: funding -0.6726% (3.4x the -0.2% extreme threshold) +40, vol $107M +15 → **55**. Suggested LONG (funding squeeze fade). Caveat: KNC perp funding this extreme almost always means a forced-deleverage event on a single venue; check spot vs perp basis before sizing. If 1h accel > +5%, score jumps to 85 and clears.
- **AXLUSDT**: funding -0.1926% +25, no vol bonus (vol $39.7M < $100M floor) → **25**. Already +9.87% in 24h, so funding is fading the rally not anticipating it. Skip.
- **BABYUSDT**: funding -0.1553% +25, vol $405M +25 → **50** raw, but +42.24% in 24h triggers late-pump penalty (-30 unless mega-accel overrides). Net ~20. Skip.
- **ENJUSDT**: funding -0.1211% +25, vol $11.7M (no bonus) → **25**. Skip.
- **BUSDT**: funding +0.2283% (positive — no squeeze bonus), vol $511M +25 → **25** raw. Up +32.88% with extreme positive funding = textbook short-carry setup, but funding_carry_short requires carry-rank emit from the dedicated loader, not handled here.

### Insights for main agent
- **Regime is "no edge" today.** BTC flat, FGI neutral, funding mildly positive — none of the high-conviction strategies (fgi_contrarian, mempool_stress + greed pairing, broad funding_squeeze) have their pre-conditions met. Expect a quiet day; don't relax MIN_SCORE_TO_TRADE just to manufacture trades.
- **Single actionable candidate: KNCUSDT funding squeeze.** -0.6726% is an outlier among outliers (next-extreme is BABYUSDT at -0.16%). If signal_detector picks up a KNC accel ≥ +5% in the next 4h, the brain will score it at 85+ (extreme funding +40, accel +30, vol +15) — pre-flight that path: confirm KNC is in the live universe and not on a volume-floor blacklist.
- **Memecoin pump cluster has zero overlap with funding outliers.** TST/BABY/B/AKT/FHE rallies are NOT funding-driven (5 of 10 top gainers have either positive or near-zero funding). The funding_squeeze strategy's edge is intact — the recent prod late-pump losses are likely coming from elsewhere (correlation_break false positives, listing_pump on stale listings).
- **No BTC/ETH directional edge.** stable_flow_bull, fgi_contrarian, mempool_stress all gated out by neutral FGI + flat BTC. If the main bot took a BTC directional bet in the last 4h, it was likely on a low-conviction (60–65) score; review for false-positive in journal.

### Validation honesty notes
- Sources fetched:
  - https://api.alternative.me/fng/ — HTTP 200, value=47 (Neutral), timestamp 1777766400.
  - https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=24 — HTTP 200, 24 bars retrieved (closes range $78,028–$78,793).
  - https://fapi.binance.com/fapi/v1/premiumIndex — HTTP 200, ~158KB, full universe.
  - https://fapi.binance.com/fapi/v1/ticker/24hr — HTTP 200, ~224KB, full universe.
- Caveats:
  - **No 1h-accel data per symbol** — fetching 147 individual kline endpoints was outside the time budget. All "marginal" scores above ASSUME accel_1h = 0. A real signal_detector pass with accel filled in could promote 2-3 of the 40–55 names above 60.
  - **No journal/positions context** — couldn't read live bot state (open positions, recently_closed, balance). Hard-filter checks (already-in-position, max-positions, balance-deployed) skipped; scores assume open slots available.
  - **KNC funding magnitude is suspicious** — -0.67% per 8h is annualized ~2,200%. Almost certainly a venue-specific dislocation; spot-vs-perp basis check recommended before any live entry. Did not verify against KNC spot.
  - **FGI is daily-snapshot data** — no intraday refresh; can't compare to 4h-ago.
  - Did not run `tools/post_tightening_report.py` — file present but no recent prod journal data accessible from this worktree path.

## 2026-05-03 10:53 UTC — Parallel Trader Pass (Pass 2)
### Δ since pass 1 (10:50 UTC, ~3 min later)
- **BTC: $78,370.70 → $78,384.90** (+$14, +0.018%). Still flat-chop. 24h % barely moved (+0.18% → +0.22%).
- **FGI: 47 → 47** (Neutral, same daily snapshot — no intraday refresh, as flagged).
- **Funding regime: essentially identical.** Positive-funding share 59% → 60.5%, median funding +0.0041% → +0.0038% (3.85 bps), mean still dragged negative by KNC (-0.0066% → -0.0066%). No regime shift.
- **Top movers — same cast.** LAB still -30.79% (was -31.33%, mild bounce of $30M off the floor). TST/BABY/B still leading gainers. AKTUSDT moved up the leaderboard from "rising" to **+28.16% top-5 gainer** with **+6.79% accel in the live 1h bar** — that's the only meaningful intraday delta.
- **KNC funding -0.6726% → -0.6715%** — still a 3.4x outlier, no normalization. The dislocation is persisting, not resolving. Spot-vs-perp basis check still recommended before sizing.

### Pass 1 marginals re-scored (with live accel_1h pulled this pass)
| symbol | pass1 score | accel_1h now | new score | promoted? |
| ------ | ----------- | ------------ | --------- | --------- |
| KNCUSDT | 55 | **+0.41%** (prev bar -3.89%) | **55** (no change) | **NO** — accel below +5% mega-bonus threshold. Pass 1's "jumps to 85" prediction did NOT trigger; KNC is bouncing weakly off lows, not exploding. |
| AXLUSDT | 25 | -0.44% | 25 | no |
| BABYUSDT | ~20 (after late-pump penalty) | **-11.49%** | ~-10 (already-pumped now dumping; reversal trade, not funding squeeze) | no |
| ENJUSDT | 25 | (not pulled, low vol) | 25 | no |
| BUSDT | 25 raw | **-9.87%** (now -9.87% in current bar after 24h +30%) | distress mode, short-carry would be tagged — but funding_carry_short loader-emitted only | no |
| **AKTUSDT (new entrant)** | n/a | **+6.79%** (clears +5% mega-accel) | funding -0.1012% (+25) + accel +30 + vol $31.5M (<$100M, no bonus) = **55** | **NO — vol floor is the blocker**. Drop vol floor from $100M → $30M and AKT scores ~70. |

### Signals scored ≥60 (would-trade under main bot rules)
| symbol | signal_type | score | factors | suggested_side |
| ------ | ----------- | ----- | ------- | -------------- |
| _none_ | — | — | Same as pass 1: zero clean ≥60 from public-API-only scoring. AKTUSDT is the closest miss at 55, blocked by the $100M volume floor. | — |

### Pattern detection — funding outliers vs movers vs volume leaders (NEW this pass)
**Overlap matrix** (top 10 of each list, vol>$10M universe):
- **Negative-funding ∩ top-gainers**: KNCUSDT (+6.21%), AXLUSDT (+9.64%), BABYUSDT (+41.23%), AKTUSDT (+28.16%) → 4/10 — these ARE recurring across both lists.
- **Negative-funding ∩ top-losers**: ENJ (-6.19%), CHIP (-11.00%), ORCA (-7.84%) → 3/10 — also real (these are getting sold WHILE shorts pay longs, classic capitulation).
- **Negative-funding ∩ top-volume**: BABYUSDT, BIOUSDT (-0.0354% fund, +9.35%), ORCAUSDT — 3/10.
- **Positive-funding ∩ top-gainers**: BUSDT (+30.11%, fund +0.23%) — 1/10. **This is the textbook short-carry late-pump, and it's the ONE name appearing across all three lists** (top funding, top gainer, top volume).
- **Top-gainers ∩ top-volume**: BABYUSDT, BUSDT — 2/10.

**Real-money signals (cross-list recurrence):**
1. **BUSDT** appears in: most-positive-funding (#1, +0.23%), top-gainers (#4, +30%), top-volume (#7, $512M). Now -9.87% in the live 1h bar. **This is the highest-conviction reversal setup of the day** — extreme positive funding + 30% rally + $500M turnover = exhausted longs paying premium. The funding_carry_short strategy SHOULD be picking this up; if it isn't, that's a loader gap to investigate.
2. **BABYUSDT** appears in: most-negative-funding (#3, -0.16%), top-gainers (#2, +41%), top-volume (#10, $406M). Now -11.49% in live bar — the squeeze already fired and reverted. Late entry would have been chasing.
3. **LABUSDT** appears in: most-positive-funding (#3, +0.11%), top-losers (#1, -30.79%), top-volume (#2, $3.66B). Capitulation already happened; positive funding now means lingering longs holding bags. Skip.

**Pattern verdict:** the cross-list recurrence test is working — names that show up in 3 lists are the ones the bot's strategies should be eating. The fact that NONE of them score ≥60 in main brain rules suggests either (a) MIN_SCORE_TO_TRADE is too tight for the current sleepy regime, or (b) the carry/squeeze loaders aren't surfacing the BUSDT-class setups.

### Insights for main agent
- **Pass 1's KNC prediction was wrong (in a useful way).** The "+5% accel → 85 score" path didn't fire because KNC's accel stayed at +0.41%. Translation: extreme funding alone, even at -0.67%, is NOT triggering a reflexive squeeze rally on this venue right now. The funding outlier is a sustained dislocation, not a coiled spring. **Recommendation:** don't pre-emptively widen MIN_SCORE on funding alone; the squeeze needs price confirmation that isn't materializing.
- **AKTUSDT is the closest near-miss this pass.** Funding -0.10%, accel +6.79%, but vol $31.5M sits below the $100M floor. Two options: (1) lower the volume floor to $25M for funding_squeeze with mega-accel confirmation, or (2) leave it and accept that this regime produces zero trades — both are defensible.
- **BUSDT short-carry: highest cross-list conviction.** If `funding_carry_short` strategy did NOT emit BUSDT in the last hour, audit the carry loader — extreme positive funding (+0.23%, ~22% annualized × 365/8 = 250%+ APY) with a 30%-pumped name is exactly its mandate. The price now turning over (-9.87% in live bar) means the trade is half-gone if it wasn't taken at the top.
- **Regime confirmation: still no edge.** 3 minutes between passes was always going to look identical, but the absence of any 60+ signal across two passes confirms pass 1's "no edge today" call. Don't manufacture trades.

### Validation honesty notes
- Sources fetched (this pass):
  - https://api.alternative.me/fng/ — HTTP 200, value=47, timestamp 1777766400 (same record as pass 1; daily refresh 47235s away).
  - https://fapi.binance.com/fapi/v1/premiumIndex — HTTP 200, ~158KB.
  - https://fapi.binance.com/fapi/v1/ticker/24hr — HTTP 200, ~224KB.
  - https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=24 — HTTP 200, 24 bars.
  - https://fapi.binance.com/fapi/v1/klines?symbol={KNC,AXL,BABY,CHIP,ORCA,CL,B,AKT}USDT&interval=1h&limit=2 — HTTP 200 each, accel_1h computed from current bar (open vs close).
- Caveats:
  - **accel_1h here = (close-open)/open of the in-progress 1h bar.** Real signal_detector may use a smoothed or delta-vs-prior-bar definition; magnitudes should be directionally correct but exact thresholds may differ.
  - **No journal/positions context** (same as pass 1). Hard-filters skipped.
  - **3-minute gap is too short for FGI/funding regime change** by construction. The "Δ since pass 1" deltas should be read as "what changed in 3 min", not as a trend signal.
  - **KNC -0.67% funding still un-validated against spot.** The dislocation persists, which weakly suggests it's a real (multi-hour) venue imbalance rather than a stale data point — but spot-vs-perp basis was not checked.
  - **AKTUSDT 1h accel of +6.79%** computed from the live (in-progress) bar. By the time the bar closes, this could be +3% or +12%; treat as directional, not point-precise.
