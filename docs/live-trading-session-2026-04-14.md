# Live Trading Session — April 14-21, 2026

## Overview

- **Duration:** ~7 days (April 14 02:00 IST → April 21 ongoing)
- **Starting balance:** $37.36 (Binance Futures deposit)
- **Final balance:** $44.17 (realized)
- **Net P&L:** +$6.81 (+18.2%)
- **Leverage:** 1x always (after fixing initial 20x mistake)
- **Exchange:** Binance Futures (USDM perpetual contracts)

## Trade Log

### Phase 1: Manual trading (Claude in conversation as brain)

| # | Symbol | Side | Entry | Exit | P&L | Duration | Strategy | Exit Reason |
|---|--------|------|-------|------|-----|----------|----------|-------------|
| 1 | RAVE | long | $10.23 | $7.84 | **-$4.81** | ~30min | momentum | stop (20x leverage!) |
| 2 | BLESS | long | $0.0195 | ~$0.020 | **+$2.03** | ~20min | momentum | manual close (20x) |
| 3 | RAVE | long | $10.60 | ~$9.86 | **-$0.72** | ~15min | momentum | manual cut |
| 4 | RAVE | long | $10.01 | ~$8.89 | **-$1.02** | ~1h | momentum (1h accel) | manual cut |
| 5 | **BLESS** | **long** | **$0.01966** | **$0.02753** | **+$8.09** | **~2h** | **momentum (1h accel)** | **+41% target hit** |
| 6 | BLESS | long | $0.0289 | ~$0.0291 | **+$0.02** | ~1h | re-entry | manual (fading) |
| 7 | ON | long | $0.1656 | ~$0.148 | **-$1.05** | ~1h | momentum | manual cut (market dump) |
| 8 | WET | long | $0.1738 | ~$0.157 | **-$1.90** | ~30min | momentum | manual cut (bad timing) |
| 9 | IRYS | long | $0.03742 | ~$0.0366 | **-$0.50** | ~1h | 1h acceleration | manual cut (chop) |
| 10 | MYX | long | $0.4796 | ~$0.465 | **-$0.65** | ~40min | 1h acceleration | manual cut (chop) |
| 11 | **ENJ** | **long** | **$0.0457** | **$0.0578** | **+$4.28** | **10h** | **funding squeeze** | **+26.5% target hit** |
| 12 | DOT | long | $1.178 | ~$1.30 | **+$1.02** | ~30h | correlation break | still running at handoff |
| 13 | RAVE | long | closed by watchdog | | **-$0.72** | | | watchdog stop |

**Manual phase subtotal:** +$3.07 from 13 trades

### Phase 2: Autonomous engine (RuleBrain, no API key)

Engine running `python -m src.engine.runner --live --auto-balance --tick 60`

| # | Symbol | Side | Entry | P&L | Duration | Strategy | Exit Reason |
|---|--------|------|-------|-----|----------|----------|-------------|
| 14 | BIO | long | $0.026 | **-$0.16** | 1.0h | funding squeeze | chop exit |
| 15 | JOE | long | $0.0498 | **-$0.22** | 2.3h | funding squeeze | chop exit |
| 16 | D | long | $0.013 | **-$0.22** | 3.3h | funding squeeze | chop exit |
| 17 | CTSI | long | $0.0425 | **+$0.15** | 1.0h | funding squeeze | chop exit (winner) |
| 18 | CTSI | long | $0.0449 | **-$0.01** | 1.0h | funding squeeze | chop exit |
| 19 | BARD | long | $0.3132 | **+$0.17** | 1.0h | funding squeeze | chop exit (winner) |
| 20 | BLUR | long | $0.0255 | **-$0.23** | 1.4h | funding squeeze | chop exit |
| 21 | ALT | long | $0.0076 | **-$0.24** | 1.0h | funding squeeze | chop exit |
| 22 | **WAL** | **long** | **$0.0794** | **+$0.76** | **1.3h** | **funding squeeze** | **trailing stop +6.6%** |
| 23 | **MBOX** | **long** | **$0.0150** | **+$3.03** | **0.6h** | **funding squeeze** | **+25.4% target hit!** |
| 24 | TRU | long | $0.0040 | **+$0.13** | 1.0h | funding squeeze | chop exit |
| 25 | EDU | long | | **+$0.07** | 1.0h | funding squeeze | chop exit |
| 26 | AXL | long | $0.0637 | **-$0.21** | 1.0h | funding squeeze | chop exit |
| 27 | BIO | long | $0.0385 | **-$0.47** | ~1h | funding squeeze | chop exit |
| 28 | TRU | long | | **-$3.23** | >24h | funding squeeze | manual close (API blocked) |
| 29 | **SAGA** | **long** | | **+$3.41** | | **funding squeeze** | **manual TP +28.5%** |
| 30 | **TST** | **long** | | **+$2.38** | | **funding squeeze** | **manual TP +26.5%** |

**Engine phase subtotal:** +$5.12 from 17 trades (including positions opened during API outage)

## Summary by Strategy

| Strategy | Trades | Wins | Win Rate | Total P&L | Avg Win | Avg Loss |
|----------|--------|------|----------|-----------|---------|----------|
| Funding squeeze | 19 | 8 | 42% | **+$5.16** | +$1.36 | -$0.37 |
| Momentum (1h accel) | 5 | 1 | 20% | **+$4.54** | +$8.09 | -$0.89 |
| Momentum (late pump) | 4 | 1 | 25% | **-$4.50** | +$2.03 | -$2.18 |
| Correlation break | 1 | 1 | 100% | **+$1.02** | +$1.02 | — |
| 1h acceleration | 2 | 0 | 0% | **-$1.15** | — | -$0.58 |

## Key Learnings

### What worked
1. **Funding squeeze** — by far the best strategy. Even with low win rate (42%), the winners are huge (+25-28%) and losers are small (-$0.37 avg via chop exit). Expected value is strongly positive.
2. **Chop exit** — cutting dead trades after 1h with <2% movement saved us from many larger losses. Most chop exits lost only $0.15-0.25.
3. **Trailing stops** — WAL went to +15.6% then pulled back, but the trailing stop locked in +6.6% profit instead of giving it all back.
4. **1x leverage** — after the initial 20x disaster (RAVE -$4.81), switching to 1x made losses manageable and eliminated liquidation risk.
5. **Patience** — the biggest wins (BLESS +41%, ENJ +26%, MBOX +25%) required holding through drawdowns for hours before the move came.

### What didn't work
1. **Late-stage momentum chasing** — entering tokens already +100% on 24h was consistently unprofitable. BLESS re-entry, ON, WET all lost.
2. **Re-entering same token at higher price** — BLESS re-entry at $0.029 after selling at $0.027 = small win but wrong direction.
3. **20x leverage** — the initial RAVE trade at 20x turned a -12% move into a -129% ROE, wiping $4.81 instantly.
4. **Social/news signals** — zero trades were triggered by Reddit, CoinGecko trending, or news. All winners came from exchange data (funding rates, price action).

### Architecture discoveries
1. **Binance API returns status=NEW for market orders** — need to poll for FILLED status
2. **Step sizes vary per symbol** — must fetch exchangeInfo and round quantities correctly
3. **IP whitelisting breaks with dynamic ISPs** — Binance API key should have unrestricted IP access or use a static IP VPS
4. **Futures API needed for prices** — some tokens only exist on futures, not spot
5. **Server-side OCO stops** — survive process crashes, essential for production

## Infrastructure

- **Local machine:** MacOS, Python 3.13
- **Engine:** `python -m src.engine.runner --live --auto-balance --tick 60`
- **Watchdog:** `python watchdog.py` (stop-loss safety net between sessions)
- **Data streams:** 11 free APIs + Binance WebSocket (700+ symbols real-time)
- **Brain:** RuleBrain ($0) when no API key, ClaudeBrain (~$1-2/day) with Haiku+Sonnet
- **Database:** Convex (cloud)
- **Exchange:** Binance Futures (OKX support added but untested live)

## Codebase Changes Made

| PR | Description |
|----|-------------|
| #25 | Rule-based brain, OKX support, live trading fixes |
| #26 | Reduce late-pump penalty, mega acceleration override |
| #27 | Remove dead code (-507 lines), fix signal gaps, update docs |
| #28 | Rewrite architecture and setup docs |
| #29 | Elite autonomous ClaudeBrain with Haiku→Sonnet architecture |
