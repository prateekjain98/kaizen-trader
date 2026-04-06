# Self-Healing AI Crypto Trader

An autonomous crypto trading system that **improves itself over time** by having Claude analyze its own trading logs and patch its parameters.

```
┌──────────────────────────────────────────────────────────────────┐
│                    SELF-HEALING LOOP                              │
│                                                                    │
│  Trade → Loss → Diagnose → Patch Parameters → Better Next Trade  │
│                     ↑                                              │
│              Claude Code reads logs every N minutes               │
│              and applies deeper pattern analysis                   │
└──────────────────────────────────────────────────────────────────┘
```

## How it works

### Layer 1: Immediate self-healing (after every loss)
The system diagnoses WHY each losing trade failed and immediately adjusts the parameter responsible:

| Loss Pattern | Root Cause | Auto Fix |
|---|---|---|
| Entered at top, quick exit | `entered_pump_top` | Raise momentum threshold |
| Stop hit in <2h | `stop_too_tight` | Widen base trail % |
| Held too long, slowly bled | `stop_too_wide` | Tighten base trail % |
| Low qual score trade lost | `low_qual_score` | Raise min score threshold |
| Funding rate squeezed | `funding_squeeze` | Lower funding threshold |

### Layer 2: Claude-powered deep analysis (every N minutes)
Claude reads the full trade history, win rates by strategy, and self-healer diagnoses, then:
- Identifies patterns the immediate healer can't see (e.g. "momentum_scalp loses 70% of trades during UTC 02-06h")
- Recommends targeted parameter changes with evidence
- Suggests new strategies based on what the data reveals is missing
- All changes are validated against `CONFIG_BOUNDS` before applying

### Layer 3: Strategy evolution (human-in-the-loop)
Claude Code can read `CLAUDE.md` and directly edit strategy files when patterns emerge that require code changes rather than parameter tuning.

---

## Strategies

### Ported from v1 (enhanced)

| Strategy | Edge | Tier |
|---|---|---|
| `momentum_swing` | 1h momentum breakout + volume spike | Swing |
| `momentum_scalp` | 5m momentum breakout, freshness-gated | Scalp |
| `listing_pump` | New exchange listings (Coinbase, Binance, Kraken, Bybit) | Swing |
| `whale_accumulation` | Net whale flow to/from exchanges over 2h window | Swing |

### New in v2

| Strategy | Edge | Tier |
|---|---|---|
| `mean_reversion` | VWAP deviation + RSI oversold/overbought | Swing |
| `funding_extreme` | Extreme funding rates → over-leveraged side will flush | Swing |
| `liquidation_cascade` | Ride the cascade short, then buy the exhaustion dip | Scalp/Swing |
| `orderbook_imbalance` | Large bid/ask walls within 1% of price → scalp the support | Scalp |
| `narrative_momentum` | Sector social velocity spike → buy the sector laggard | Swing |
| `correlation_break` | Alt diverges from BTC correlation → mean reversion | Swing |
| `protocol_revenue` | DeFiLlama fee spike before token catches up | Swing |
| `fear_greed_contrarian` | Extreme Fear (<15) or Extreme Greed (>85) plays | Position |

---

## Architecture

```
src/
├── types.ts                    # All TypeScript types (start here)
├── config.ts                   # Parameter defaults + hard bounds
├── index.ts                    # Main process (WebSocket + polling loops)
│
├── strategies/                 # One file per trading strategy
│   ├── momentum.ts             # Momentum breakout (swing + scalp)
│   ├── mean-reversion.ts       # VWAP deviation + RSI
│   ├── listing-pump.ts         # Exchange listing detector
│   ├── whale-tracker.ts        # Whale flow analysis
│   ├── funding-extreme.ts      # Funding rate extremes
│   ├── liquidation-cascade.ts  # Liquidation event detection
│   ├── orderbook-imbalance.ts  # L2 order book depth
│   ├── narrative-momentum.ts   # Sector rotation (10 narratives)
│   ├── correlation-break.ts    # BTC correlation divergence
│   ├── protocol-revenue.ts     # DeFiLlama fundamentals
│   └── fear-greed-contrarian.ts # Fear & Greed index extremes
│
├── self-healing/
│   ├── index.ts                # Immediate: loss → diagnosis → parameter patch
│   └── log-analyzer.ts         # Claude: periodic deep log analysis
│
└── storage/
    └── database.ts             # SQLite: trades, logs, diagnoses, config history
```

---

## Signal sources

| Source | Data | API |
|---|---|---|
| CryptoPanic | News headlines, sentiment | `https://cryptopanic.com/developers/api/` |
| LunarCrush | Social galaxy score, volume, alt rank | `https://lunarcrush.com/developers` |
| Twitter/X | Cashtag mentions velocity | X Developer API |
| Whale Alert | Large wallet transfers | `https://whale-alert.io` |
| Binance | Funding rates, open interest, liquidations | Binance Futures WS |
| DeFiLlama | Protocol revenue, TVL | `https://api.llama.fi/overview/fees` |
| Alternative.me | Fear & Greed Index | `https://api.alternative.me/fng/` |
| Coinbase Advanced | Real-time prices, order book | Coinbase Advanced Trade WS |

---

## Self-healing loop detail

```
Every closed position
  └─ pnl < -0.5%?
       ├─ NO  → log win, no changes
       └─ YES → classifyLossReason()
                  ├─ entered_pump_top → raise momentumPct (+0.01)
                  ├─ stop_too_tight   → widen baseTrailPct (+0.01)
                  ├─ stop_too_wide    → tighten baseTrailPct (-0.01)
                  ├─ low_qual_score   → raise minQualScore (+2)
                  ├─ funding_squeeze  → lower fundingThreshold (-0.0001)
                  └─ unknown          → log for Claude analysis

Every N minutes (configurable, default 60)
  └─ >= MIN_TRADES_FOR_ANALYSIS closed trades?
       └─ YES → Claude reads last 200 trades + diagnoses + error logs
                → returns JSON: { summary, parameterPatch, suggestions }
                → validate each param against CONFIG_BOUNDS
                → apply valid changes, reject out-of-bounds changes
                → log everything to trader.db for audit trail
```

---

## Setup

```bash
# 1. Clone and install
git clone https://github.com/prateek9jain8/self-healing-crypto-trader
cd self-healing-crypto-trader
npm install

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY
# Leave PAPER_TRADING=true until you've validated the system

# 3. Run
npm start

# 4. Trigger manual log analysis
npm run analyze
```

### Required API keys

| Key | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes (for self-healing) | Claude log analysis |
| `COINBASE_API_KEY/SECRET` | Yes (for live trading) | Order execution + price feed |
| `PAPER_TRADING=true` | Strongly recommended first | No real money at risk |
| `CRYPTOPANIC_TOKEN` | Recommended | News sentiment |
| `LUNARCRUSH_API_KEY` | Recommended | Social signals |
| `BINANCE_API_KEY/SECRET` | Optional | Shorts + funding rates |
| `WHALE_ALERT_API_KEY` | Optional | Whale tracking |
| `TWITTER_BEARER_TOKEN` | Optional | Social mentions |

---

## Risk management

- **Circuit breaker**: halts new trades if daily drawdown exceeds `MAX_DAILY_LOSS_USD`
- **Position sizing**: configurable max per trade, respects daily loss limit
- **Per-symbol cooldown**: won't re-enter a recently closed symbol
- **Self-healer cap**: max 20 parameter adaptations per session (prevents over-correction)
- **CONFIG_BOUNDS**: every parameter has hard min/max — Claude can't override these

---

## Paper trading vs live

The system defaults to `PAPER_TRADING=true`. In paper mode:
- All orders are simulated at the current market price
- No API calls to Coinbase/Binance for order placement
- Everything else (signal detection, self-healing, logging) runs identically
- The database logs paper trades with `paper_trading=1` flag

Run paper trading for at least 2 weeks and verify the self-healing loop is improving win rates before enabling live trading.

---

## Disclaimer

This is an educational project. Crypto trading involves significant risk of loss. The self-healing mechanism improves parameters based on historical patterns — past performance does not guarantee future results. Use `PAPER_TRADING=true` until you fully understand the system's behavior.
