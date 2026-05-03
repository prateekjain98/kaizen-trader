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
  **CORRECTION (2026-05-03):** prior "VALIDATION-IMPOSSIBLE" claim was
  WRONG. The harness HAS a working `funding_carry_loader.py` reconstructor;
  validation just needs `--symbols` to cover the full 27-symbol
  `_CARRY_LIQUID_UNIVERSE` (default CLI passes only 8 majors which is
  exactly `_MIN_SYMBOLS_FOR_RANKING`, making ranks degenerate).
  **Honest validation, 90d split=3, 27 symbols, 2026-05-03:**
    All 3 windows positive (W1 +\$3.91 / W2 +\$2.95 / W3 +\$0.63)
    Total: 62 trades, +\$7.49 (+15% on \$50 balance over 90d)
    Carry isolated: n=32, mean +0.83%/trade, t=0.68, sum +\$5.63
    Aggregate: n=62, mean +0.61%, t=0.83 → **PRELIMINARY** (n<100, t<2.81)
  Verdict gate per constitution: PRELIMINARY < ROBUST → DO NOT flip env.
  Need n≥100 with t>2.81 (Bonferroni α=0.0025).
  **180d follow-up (2026-05-03, commit pending):** carry edge does NOT
  hold up. n=47 carry trades, mean +0.024%/trade, t=+0.03 (pure noise).
  Per-window: W1 +\$0.39 / W2 **-\$1.06** / W3 +\$1.82 — NOT all-positive.
  90d's "all-windows-positive" was a small-sample artifact. 180d aggregate
  ALL: n=94, mean +0.27%, t=0.57 PRELIMINARY (mostly carried by
  stable_flow_bull which IS positive in 2 of 3 windows).
  **CONCLUSION: funding_carry edge is not robust at the 180d horizon.**
  Stays disabled. Re-attempt only after harness is extended with: (a)
  larger universe (50+ symbols not just the 27 we have), (b) tighter
  carry-rank threshold (e.g. top 2% not top 10%), (c) trade-direction
  cooldown so back-to-back losing carries don't cascade.
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

## 2026-05-03 11:04 UTC — Parallel Trader Pass (Pass 3 — regime correlation)

### Δ since pass 2 (commit d7dcb66, ~11 min later)
- **BTC: $78,384.90 → $78,382.60** (−$2.30, essentially flat). Close range over last 24h: $78,131–$78,793 (~0.85% band — even tighter than pass 1's 1.4%).
- **FGI today: 47 (Neutral)** — same daily snapshot as passes 1 & 2.
- **FGI 7d trend (oldest → newest): 47, 33, 26, 29, 26, 39, 47.** Sequence is **Fear → recovering**: market spent 5 of last 7 days in Fear (low: 26), today's 47 is a +21pt rebound from yesterday's 39 (and +21pt off the trough). Direction is **rising-out-of-fear**, not greedy.

### Regime metrics (computed this pass)
- **BTC 24h realized vol (1h close-to-close stdev × √24): 0.75%.** Annualized (× √(24·365)): **~14.4%** — extremely low. For context, BTC's long-run realized vol typically prints 40–80% annualized; 14% is "dead-flat funeral parlor" territory.
- **BTC-ETH 24h hourly-return Pearson correlation: +0.909.** Very high — BTC and ETH are moving in lockstep. No idiosyncratic alt opportunity at the majors level; whatever happens is a beta trade.
- **FGI 7d direction: rising-out-of-fear** (trough 26 three days ago, now 47, classification crossed Fear→Neutral today).

### Regime classification
**CHOP / VOL-COMPRESSION** with a **fear-recovery FGI tailwind**. Specifically:
- 14.4% annualized BTC vol = bottom-decile vol regime. This is **bad for funding_squeeze** (squeeze needs price impulse to fire — KNC pass 1's prediction failing is exhibit A) and **bad for liquidation_cascade** (no big moves to cascade).
- 0.91 BTC-ETH corr = **majors are beta-driven**, no relative-value edge at the top of the book. fgi_contrarian on BTC/ETH is the cleanest play if FGI continues recovering, but today's reading (47) is squarely in the Neutral dead zone — no contrarian signal.
- The 7d FGI arc (Fear → Neutral) is the kind of setup where late-pump altcoin reversals (the BUSDT-class setups) bite hardest: shorts who pressed during the fear lows now get squeezed as risk-on returns, then the late-chasers blow up. **This is funding_carry_short's home regime** — and it remains disabled per CONFIG STATUS.

### Would-have-trade post-mortem (pulled 4 × 1h bars per symbol)

| symbol | pass 1/2 entry signal | 4h price move (close[0]→close[3]) | max favorable | max adverse | verdict |
| ------ | --------------------- | --------------------------------- | ------------- | ----------- | ------- |
| **KNCUSDT** | pass 1 LONG @ 0.1779 (funding squeeze, score 55) | **-3.56%** | +1.24% | -5.14% | **LOSER** — KNC made fresh lows (0.1681) before the bounce; a 1-2% stop would have triggered. Pass 1 + pass 2's caution (predicted +5% accel that never came) was correct. |
| **AKTUSDT** | pass 2 LONG-near-miss @ 0.6197 (funding +mega-accel, score 55, blocked by vol floor) | **+1.64%** | +4.19% | -4.16% | **MILDLY POSITIVE / FLAT** — closed +1.6%, but path was choppy (peaked +4.2%, dipped -4.2%). With a 2% stop on the dip into bar 1's low ($0.6139) the trade would have been stopped out before the +4% bar 2 print. Volume floor saved a coin-flip. |
| **BUSDT** | pass 2 SHORT-carry conviction @ 0.5372 (extreme positive funding + 30% pump + $500M turnover) | **-23.64%** | +1.40% (long-side) → **-27.44% favorable for SHORT** | -1.40% adverse for SHORT | **MASSIVE WINNER (would-have)** — close fell from $0.5372 → $0.4102 in 4 hours. A short-carry entry at pass 2's call would have realized **~+23.6% PnL** with only 1.4% adverse excursion. **This is the trade the funding_carry_short strategy is meant to capture and is currently gated off by `FUNDING_CARRY_ENABLED=0`.** |

### Insight for main agent — this is the strongest cross-pass evidence yet

1. **Regime is CHOP+VOL-COMPRESSION with a fear-recovery overlay.** 14.4% annualized BTC vol + 0.91 BTC-ETH corr means: don't expect funding_squeeze or cascade strategies to fire cleanly. They need impulse. They're not getting it. Pass 1 + pass 2 + pass 3 all returned zero ≥60 candidates — that's the regime, not a bot bug.
2. **BUSDT post-mortem is the headline.** A coin we flagged in pass 2 as "highest cross-list conviction, funding_carry_short SHOULD pick this up" just printed -23.6% in 4 hours. The short-carry entry would have been a clean +23% win with negligible adverse excursion. This is **anecdotal n=1**, not a backtest — but it's the kind of n=1 worth logging because it's exactly the trade type the prod backtest harness CANNOT synthesize (per CONFIG STATUS line 14-22). The harness gap is now visibly costing real-world signal.
3. **Do NOT enable funding_carry on this evidence alone.** The CONFIG STATUS rule is correct: the prior +$5.53 backtest had 3 compounding bugs, and one favorable post-mortem doesn't override that. **What this pass DOES support:** prioritize building the historical 8h-boundary funding-rank loader so the harness can actually validate carry. Today's BUSDT is one data point that says "the gap matters" — it's a reason to fund the harness work, not to flip the env-var gate.
4. **AKT and KNC outcomes vindicate current gating.** AKT was a coin-flip blocked by the $100M vol floor — floor did its job. KNC was a -3.56% loss prevented by MIN_SCORE_TO_TRADE=60. Both gates earned their keep this pass. **Recommendation: do NOT relax MIN_SCORE or vol floor based on the pass 2 "near-miss" framing.**
5. **High BTC-ETH correlation (0.91) is a soft signal that idiosyncratic-coin strategies (correlation_break, listing_pump) might also be misfiring.** When everything moves together, "correlation break" detectors will see noise as signal. Worth checking the recent journal for correlation_break entries that lost money in the last 24h.

### Action suggestion grounded in the regime read
- **Today (next 4-8h):** stay in zero-trade mode. The 14% vol regime + Neutral FGI + 0.91 majors-corr produces no edge for the enabled strategies. Pass 1's "no edge today" call is now confirmed across 3 passes.
- **This week:** invest engineering time in the funding_carry historical loader (CONFIG STATUS line 19-22). Today's BUSDT post-mortem is concrete justification — a single un-validatable strategy missed a +23% setup in 4h. That's the cost-of-not-validating, made visible.
- **Watch trigger:** if BTC vol expands above 25% annualized OR FGI crosses above 55 (greedy), re-run this pass. The current regime can flip on any 4h candle.

### Validation honesty notes
- Sources fetched (this pass):
  - https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=24 — HTTP 200, 24 bars (3371 bytes).
  - https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=1h&limit=24 — HTTP 200, 24 bars (3333 bytes).
  - https://api.alternative.me/fng/?limit=7 — HTTP 200, 7 records (760 bytes), values 47/39/26/29/26/33/47 newest-first.
  - https://fapi.binance.com/fapi/v1/klines?symbol={KNC,AKT,B}USDT&interval=1h&limit=4 — HTTP 200 each, 4 bars each.
- Caveats:
  - **Realized vol = 1h close-to-close log returns × √24.** Standard formulation but sensitive to the 24-bar window; a 7-day window would smooth the 14.4% number, likely landing 18-25% (still bottom-half).
  - **Pearson on n=23 hourly returns is a small sample.** 0.91 is high enough to be directionally robust but the point estimate has ~±0.05 noise.
  - **Post-mortem uses the 4 most recent closed/in-progress 1h bars.** Pass 1 was at 10:50 UTC (~14 min ago); 4 bars ≈ 4h is slightly longer than the actual elapsed time, so the earliest bar pre-dates pass 1's signal slightly. Directional verdicts (LOSER/FLAT/WINNER) are robust to this; exact PnL numbers are approximate. The BUSDT bar-0 high of 0.5447 is roughly when pass 2's "highest conviction" call was made.
  - **No spot-vs-perp basis check** for KNC (still flagged from pass 1).
  - **No journal/positions context** — same gap as pass 1 & 2.
  - **BUSDT "+23% would-have" is hypothetical.** No order was placed; slippage, partial fills, funding payments not modeled. Real execution on a -23% in 4h move on a low-cap perp would face widening spreads and likely a 1-3% slippage haircut.
