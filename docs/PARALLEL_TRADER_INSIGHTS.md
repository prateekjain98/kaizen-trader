# Parallel Trader Insights

This file is APPENDED to by parallel paper-trader agent passes. Each entry is
timestamped and self-contained. Read the most recent entries when planning
the next change to the main bot's rule_brain or signal_detector.

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
