<div align="center">

# kaizen-trader

**An autonomous crypto trading engine that improves itself after every loss.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Node.js](https://img.shields.io/badge/Node.js-20%2B-green?logo=node.js)](https://nodejs.org/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.7-blue?logo=typescript)](https://www.typescriptlang.org/)
[![Paper Trading](https://img.shields.io/badge/Paper%20Trading-enabled%20by%20default-orange)](.env.example)

</div>

---

> **Disclaimer:** This software is for educational purposes only. Do not risk money you cannot afford to lose. USE THE SOFTWARE AT YOUR OWN RISK. The authors assume no responsibility for your trading results. Always run in paper trading mode first and understand the system before enabling live trading.

---

## What is kaizen-trader?

Most trading bots run the same logic forever. When market conditions change, their parameters go stale and they keep losing in the same way.

kaizen-trader does something different: it diagnoses every losing trade, identifies the root cause, and adjusts the parameter responsible — in real-time, without restarting. On top of that, Claude periodically reads the full trade history and applies deeper pattern analysis that the rule-based healer can't see.

```
A trade closes at a loss
  └── Root cause?
        ├── Entered at the top of a pump     →  raise momentum threshold (+0.01)
        ├── Stop hit too fast (<2h)           →  widen trailing stop (+1%)
        ├── Slow bleed over a long hold       →  tighten trailing stop (-1%)
        ├── Low-conviction entry              →  raise minimum signal score (+2)
        └── Funding rate squeezed out         →  lower funding threshold

Every 60 minutes
  └── Claude reads the last 200 trades + diagnoses
        →  finds patterns the rule-based healer can't see
        →  returns a validated JSON parameter patch
        →  logs the reasoning for the next iteration to build on
```

All parameter changes are constrained by hard bounds in `src/config.ts`. Claude cannot exceed them.

---

## Why kaizen-trader?

| | kaizen-trader | Most bots |
|---|---|---|
| Self-healing | ✅ diagnoses losses, patches parameters | ❌ static rules |
| AI log analysis | ✅ Claude reviews trade history periodically | ❌ no |
| Signal breadth | ✅ price, social, news, on-chain, macro, order book | ⚠️ price only |
| Storage | ✅ full audit trail: trades, diagnoses, config history | ⚠️ varies |
| Default mode | ✅ paper trading, no risk out of the box | ⚠️ varies |

---

## Strategies

### Momentum

| Strategy | Entry condition |
|---|---|
| `momentum_swing` | Price +2% in 1h with 2× volume spike above baseline |
| `momentum_scalp` | Price +2.5% in 5m with fresh move (40%+ of gain in last 2m) |

### Mean reversion

| Strategy | Entry condition |
|---|---|
| `mean_reversion` | Price >3% from VWAP + RSI oversold (<30) or overbought (>70) |
| `fear_greed_contrarian` | Fear & Greed Index ≤15 (buy panic) or ≥85 (sell euphoria) |
| `correlation_break` | Alt deviates >3% from its historical BTC regression — bets on reversion |
| `funding_extreme` | Perp funding >0.1%/8h (over-leveraged longs → short) or <−0.05% (squeeze → long) |

### Event-driven

| Strategy | Entry condition |
|---|---|
| `listing_pump` | New listing on Coinbase / Binance / Kraken / Bybit, within 30m of announcement |
| `whale_accumulation` | Net whale flow >$5M out of exchanges over 2h (accumulation signal) |
| `liquidation_cascade` | >$2M longs liquidated in 10m + OI falling → ride the flush; buy the exhaustion dip |

### Structural

| Strategy | Entry condition |
|---|---|
| `orderbook_imbalance` | Bid/ask depth ratio >3× within 1% of price — scalp the wall |
| `narrative_momentum` | Social velocity for a sector (AI, DeFi, RWA…) spikes 3× → buy the sector's laggard |
| `protocol_revenue` | DeFiLlama: protocol fees 2× above 7d average before the token price moves |

---

## Adding a strategy

Every strategy is a single function in `src/strategies/`. Here is the complete implementation of a simple one:

```typescript
// src/strategies/my-strategy.ts
import type { TradeSignal, ScannerConfig, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

export function scanMyStrategy(
  symbol: string,
  productId: string,
  currentPrice: number,
  config: ScannerConfig,
  ctx: MarketContext,
): TradeSignal | null {

  // Return null if conditions are not met
  if (ctx.fearGreedIndex > 30) return null;

  return {
    id: randomUUID(),
    symbol,
    productId,
    strategy: 'my_strategy',
    side: 'long',
    tier: 'swing',
    score: 72,
    confidence: 'medium',
    sources: ['fear_greed'],
    reasoning: `${symbol} in extreme fear — contrarian entry`,
    entryPrice: currentPrice,
    stopPrice: currentPrice * 0.95,
    suggestedSizeUsd: 100,
    expiresAt: Date.now() + 3_600_000,
    createdAt: Date.now(),
  };
}
```

Then register it in `src/strategies/index.ts` and add `'my_strategy'` to the `StrategyId` union in `src/types.ts`.

---

## Sample output

```
[2026-01-15T09:14:02Z] [INFO]  ─── kaizen-trader starting ───
[2026-01-15T09:14:02Z] [INFO]  PAPER TRADING mode — no real orders will be placed
[2026-01-15T09:14:02Z] [INFO]  Claude log analysis scheduled every 60 minutes
[2026-01-15T09:21:38Z] [SIGNAL] [SOL] momentum_swing — SOL +3.1% in 1h with 2.4× volume spike  score=71
[2026-01-15T09:21:39Z] [TRADE]  [SOL] BUY  $100 @ $182.40  (paper)  qual=71
[2026-01-15T10:03:11Z] [TRADE]  [SOL] SELL $100 @ $189.90  (paper)  pnl=+4.1%  reason=trailing_stop
[2026-01-15T11:07:44Z] [SIGNAL] [ARB] narrative_momentum — layer2 sector 3.8× social velocity; ARB lagging by −2.1%  score=66
[2026-01-15T11:07:45Z] [TRADE]  [ARB] BUY  $80  @ $1.23   (paper)
[2026-01-15T11:29:03Z] [TRADE]  [ARB] SELL $80  @ $1.19   (paper)  pnl=−3.2%  reason=trailing_stop
[2026-01-15T11:29:03Z] [HEAL]   [ARB] LOSS −3.2% reason=stop_too_tight → widen baseTrailPctSwing 7% → 8%
[2026-01-15T12:14:02Z] [HEAL]   Claude analysis complete (confidence=medium)
                                  top issues: narrative_momentum entering too late in pump cycle
                                  applied: momentumPctSwing 0.020 → 0.025, narrativeVelocityThreshold 3.0 → 3.8
```

---

## Quick start

```bash
git clone https://github.com/prateekjain98/kaizen-trader
cd kaizen-trader
npm install

cp .env.example .env
# Edit .env — fill in at minimum ANTHROPIC_API_KEY and COINBASE_API_KEY/SECRET
# PAPER_TRADING is true by default — no real money at risk

npm start
```

Run a manual Claude analysis at any time:

```bash
npm run analyze
```

---

## Configuration

```bash
# .env
PAPER_TRADING=true           # always start here

ANTHROPIC_API_KEY=           # required for self-healing log analysis
COINBASE_API_KEY=            # required for price feed + order execution
COINBASE_API_SECRET=

BINANCE_API_KEY=             # optional — enables shorts, funding rates, liquidation stream
BINANCE_API_SECRET=

LUNARCRUSH_API_KEY=          # optional — social signals (Twitter/Reddit/YouTube/TikTok)
CRYPTOPANIC_TOKEN=           # optional — news headlines
WHALE_ALERT_API_KEY=         # optional — on-chain whale transfers

MAX_POSITION_USD=100
MAX_DAILY_LOSS_USD=300
MAX_OPEN_POSITIONS=5
LOG_ANALYSIS_INTERVAL_MINS=60
```

Strategies degrade gracefully when their data source isn't configured — the system still trades on whatever signals are available.

---

## How the self-healing works in detail

```
src/self-healing/index.ts     — fires after every closed position
  classifyLossReason()        — inspects hold time, PnL, momentum at entry, exit reason
  applyLossAdaptation()       — patches one parameter in the live config object
  insertDiagnosis()           — writes the diagnosis to SQLite for Claude to read later

src/self-healing/log-analyzer.ts  — fires on a timer (default: every 60 minutes)
  builds a prompt with:
    • last 200 closed trades grouped by strategy
    • win rates, average PnL, hold times
    • all recent self-healer diagnoses
    • recent error/warning logs
  sends to claude-opus-4-6
  receives JSON: { summary, parameterPatch, newStrategySuggestions }
  validates each patch value against CONFIG_BOUNDS
  applies valid changes, rejects out-of-bounds values
  logs everything to SQLite for the next iteration
```

The full audit trail — every trade, every diagnosis, every config snapshot — lives in `trader.db`. Claude Code can query it directly using the instructions in `CLAUDE.md`.

---

## Project structure

```
src/
├── types.ts                      # All shared types — start here
├── config.ts                     # Parameter defaults and hard bounds
├── index.ts                      # Process entry point
├── strategies/                   # One file per strategy
├── self-healing/
│   ├── index.ts                  # Immediate: loss → diagnosis → parameter patch
│   └── log-analyzer.ts           # Claude: periodic deep analysis via Anthropic SDK
└── storage/
    └── database.ts               # SQLite — trades, logs, diagnoses, config history

scripts/
└── analyze-logs.ts               # Run Claude analysis manually: npm run analyze

CLAUDE.md                         # Instructions for Claude Code to query logs and improve the system
```

---

## Requirements

- Node.js 20+
- SQLite (bundled via `better-sqlite3`, no separate install needed)
- Coinbase Advanced Trade account (for price feed)
- Anthropic API key (for self-healing log analysis)

---

## Contributing

1. Fork the repo and create a branch
2. Add your strategy in `src/strategies/` following the pattern above
3. Register it in `src/strategies/index.ts` and `src/types.ts`
4. Open a pull request with a description of the entry conditions and the edge being captured

---

## License

MIT
