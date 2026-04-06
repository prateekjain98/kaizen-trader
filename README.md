# kaizen-trader

> **kaizen** (改善) — the practice of continuous improvement

An autonomous crypto trading system with 12 strategies and a self-healing engine that gets smarter after every trade. It diagnoses its own losses, patches its parameters in real-time, and calls Claude periodically to perform deeper analysis on its trade history.

> **Risk warning:** This is experimental software. Crypto trading can result in total loss of capital. Always run with `PAPER_TRADING=true` first. No financial advice is expressed or implied.

---

## How the self-healing works

After every losing trade, the system classifies the root cause and adjusts the responsible parameter immediately:

```
Trade closes at a loss
  └── Why did it lose?
        ├── Entered at the top of a pump  →  raise momentum threshold
        ├── Stop hit too fast (<2h)        →  widen trailing stop
        ├── Slow bleed over long hold      →  tighten trailing stop
        ├── Low conviction entry            →  raise minimum signal score
        └── Funding rate squeezed it       →  lower funding threshold

Every 60 minutes
  └── Claude reads the last 200 trades + all diagnoses
        →  identifies patterns the rule-based healer misses
        →  returns a validated parameter patch
        →  logs reasoning for the next iteration to build on
```

All parameter changes are bounded by hard limits in `src/config.ts`. Claude cannot override these.

---

## Strategies

### Momentum

| Strategy | Signal | Timeframe |
|---|---|---|
| `momentum_swing` | Price +2% in 1h with 2× volume spike | Swing |
| `momentum_scalp` | Price +2.5% in 5m, freshness-gated, 2.5× volume | Scalp |

### Mean reversion

| Strategy | Signal | Timeframe |
|---|---|---|
| `mean_reversion` | Price >3% below VWAP + RSI <30 (long) or >3% above VWAP + RSI >70 (short) | Swing |
| `fear_greed_contrarian` | Fear & Greed Index ≤15 (extreme fear long) or ≥85 (extreme greed short) | Swing |
| `correlation_break` | Alt diverges >3% from its BTC regression baseline — bets on reversion | Swing |

### Event-driven

| Strategy | Signal | Timeframe |
|---|---|---|
| `listing_pump` | New listing on Coinbase / Binance / Kraken / Bybit detected within 30m | Swing |
| `whale_accumulation` | Net whale flow >$5M out of exchanges in 2h window (accumulation) | Swing |
| `liquidation_cascade` | >$2M longs liquidated in 10m + OI dropping → ride the cascade; buy exhaustion dip | Scalp/Swing |

### Structural / macro

| Strategy | Signal | Timeframe |
|---|---|---|
| `funding_extreme` | Perp funding >0.1%/8h (over-leveraged longs → short) or <-0.05% (squeeze → long) | Swing |
| `orderbook_imbalance` | Bid/ask depth ratio >3× within 1% of price — scalp the wall | Scalp |
| `narrative_momentum` | Sector social velocity >3× baseline → buy the sector's laggard token | Swing |
| `protocol_revenue` | DeFiLlama: protocol fees 2× above 7d avg before token price catches up | Swing |

---

## Signal sources

| Source | What it provides | Why this one |
|---|---|---|
| **LunarCrush** | Social score, volume, AltRank across Twitter/Reddit/YouTube/TikTok | Single API covers all social signals — no need for separate platform keys |
| **CryptoPanic** | Crypto news headlines, sentiment | Dedicated news aggregator with token-level filtering |
| **Whale Alert** | Large on-chain transfers ($3M+) | Best coverage of CEX/DEX/wallet flows |
| **Binance Futures WS** | Funding rates, open interest, real-time liquidations | Only exchange with public liquidation WebSocket |
| **DeFiLlama** | Protocol TVL, daily revenue by protocol | Free, accurate, covers 2000+ protocols |
| **Alternative.me** | Fear & Greed Index | Free, widely cited, no auth required |
| **Coinbase Advanced WS** | Real-time prices, L2 order book | Primary execution venue |

---

## Project structure

```
kaizen-trader/
├── src/
│   ├── types.ts                     # All shared types — start here
│   ├── config.ts                    # Parameter defaults + hard bounds
│   ├── index.ts                     # Process entry — attaches all loops
│   │
│   ├── strategies/                  # One file per strategy
│   │   ├── momentum.ts
│   │   ├── mean-reversion.ts
│   │   ├── listing-pump.ts
│   │   ├── whale-tracker.ts
│   │   ├── funding-extreme.ts
│   │   ├── liquidation-cascade.ts
│   │   ├── orderbook-imbalance.ts
│   │   ├── narrative-momentum.ts
│   │   ├── correlation-break.ts
│   │   ├── protocol-revenue.ts
│   │   └── fear-greed-contrarian.ts
│   │
│   ├── self-healing/
│   │   ├── index.ts                 # Immediate: loss → diagnosis → patch
│   │   └── log-analyzer.ts         # Claude: periodic deep analysis
│   │
│   └── storage/
│       └── database.ts              # SQLite — trades, logs, diagnoses, config history
│
├── scripts/
│   └── analyze-logs.ts             # Run Claude analysis manually
│
├── CLAUDE.md                        # How Claude Code should read and improve this system
├── .env.example
└── trader.db                        # Created at runtime (gitignored)
```

---

## Quick start

```bash
git clone https://github.com/prateekjain98/kaizen-trader
cd kaizen-trader
npm install

cp .env.example .env
# Fill in at minimum: ANTHROPIC_API_KEY, COINBASE_API_KEY/SECRET
# Leave PAPER_TRADING=true until you trust the system

npm start
```

To trigger a manual Claude analysis at any time:

```bash
npm run analyze
```

### Minimum required keys

| Key | Required for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude log analysis (the core self-healing loop) |
| `COINBASE_API_KEY` + `COINBASE_API_SECRET` | Price feed + order execution |

Everything else is optional — strategies degrade gracefully when their data source isn't configured.

---

## Configuration

All parameters live in `src/config.ts`. The self-healer adjusts these at runtime. Each parameter has a hard bound it cannot exceed:

```
momentumPctSwing          default 2%      bounds [1%, 15%]
momentumPctScalp          default 2.5%    bounds [1.5%, 10%]
volumeMultiplierSwing     default 2.0×    bounds [1.5×, 5×]
baseTrailPctSwing         default 7%      bounds [4%, 18%]
baseTrailPctScalp         default 4%      bounds [2%, 8%]
minQualScoreSwing         default 55      bounds [45, 85]
fundingRateExtremeThresh  default 0.1%    bounds [0.05%, 0.5%]
narrativeVelocityThresh   default 3×      bounds [1.5×, 8×]
```

---

## Risk controls

- **Circuit breaker** — halts all new entries if daily drawdown exceeds `MAX_DAILY_LOSS_USD`
- **Per-symbol cooldown** — won't re-enter a symbol recently exited
- **Concurrent position cap** — `MAX_OPEN_POSITIONS` (default 5)
- **Self-healer session cap** — max 20 parameter changes per process lifetime (prevents overcorrection)
- **Hard parameter bounds** — Claude and the rule-based healer both respect these

---

## Adding a strategy

1. Create `src/strategies/your-strategy.ts`:
   ```ts
   export function scanYourStrategy(
     symbol: string,
     productId: string,
     currentPrice: number,
     config: ScannerConfig,
     ctx: MarketContext,
   ): TradeSignal | null {
     // return a TradeSignal when conditions are met, null otherwise
   }
   ```
2. Add the `StrategyId` to the union in `src/types.ts`
3. Export from `src/strategies/index.ts`

---

## License

MIT
