<div align="center">

# kaizen-trader

**An autonomous crypto trading engine that improves itself after every trade.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org/)
[![Claude](https://img.shields.io/badge/Powered%20by-Claude%20Opus-8A2BE2?logo=anthropic)](https://anthropic.com)
[![Paper Trading](https://img.shields.io/badge/Paper%20Trading-default-orange)](.env.example)

</div>

---

> **Disclaimer:** This is experimental software built for research purposes. Crypto trading involves significant risk of loss. The authors assume no responsibility for trading outcomes. **Always run with `PAPER_TRADING=true` first.**

---

## What it is

Most trading systems are static: they run the same rules until you manually tune them. kaizen-trader takes a different approach — it continuously analyzes its own trade history, patches its parameters, tracks whether those changes helped or hurt, and creates GitHub issues when it identifies missing data or blind spots.

The full audit trail — every trade, every diagnosis, every config snapshot, every parameter delta — is stored in SQLite locally, with optional dual-write to Convex for a real-time dashboard.

## Architecture

```
┌─────────────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
│  Railway: Python Bot        │      │  Convex: Real-time DB    │      │  Vercel: Dashboard       │
│  ($20/mo)                   │─────>│  (Free tier)             │<─────│  prateekjain.io/         │
│                             │      │                          │      │    kaizen-trader         │
│  - Full trading pipeline    │      │  - positions, trades     │      │                          │
│  - WebSocket feeds          │      │  - logs, diagnoses       │      │  - Live portfolio        │
│  - Self-healing + deltas    │      │  - parameter_deltas      │      │  - Real-time positions   │
│  - Claude analysis          │      │  - config_history        │      │  - AI chat               │
│  - Strategy evaluation      │      │  - 15-min metric cron    │      │                          │
│  - Auto GitHub issues       │      │                          │      │                          │
│  - Health check (:8080)     │      │                          │      │                          │
└─────────────────────────────┘      └──────────────────────────┘      └──────────────────────────┘
```

The bot runs as a single Python process with three continuous stages:

**1 — Signal ingestion:**
Coinbase WebSocket streams price ticks and L2 order book updates. Six external APIs (CryptoPanic, LunarCrush, Whale Alert, Binance Futures, DeFiLlama, Alternative.me) poll on independent schedules and update an in-memory `MarketContext`. Each signal fetcher has a circuit breaker (3 failures -> 5-minute cooldown).

**2 — Trade engine:**
On every tick (throttled to 2s/symbol), 11 strategy scanners run against the current price and context. Strategies are auto-discovered from `src/strategies/` via the registry. Signals pass the qualification scorer (multi-signal aggregation), are sized via quarter-Kelly, and routed to the executor (paper by default, Coinbase Advanced REST in live mode). A declarative protection chain blocks new positions when daily loss limits, max positions, or cooldowns are hit.

**3 — Self-healing:**
Four feedback loops run in parallel — immediate rule-based patching after every loss, periodic Claude deep analysis, delta evaluation to auto-revert bad changes, and Darwinian strategy selection. Details below.

See [`docs/architecture.md`](docs/architecture.md) for Mermaid diagrams, data flows, and design rationale.

---

## Self-healing loops

| Loop | Frequency | What it does |
|---|---|---|
| **L1 — Rule-based healer** | After every loss | Classifies why the trade lost (pump top, tight stop, low qual score, etc.) and patches the responsible parameter |
| **L2 — Claude analysis** | Every 60 min | Reads 300 trades + 50 diagnoses, computes Sharpe/Sortino/Calmar, reasons through patterns the rule-based healer can't detect, returns structured JSON patches with chain-of-thought |
| **L3 — Delta evaluator** | Every 2 hours | Every parameter change (from L1 or L2) is recorded with a before/after trade snapshot. After 10+ trades, the evaluator auto-reverts changes that worsened performance |
| **L4 — Darwinian selector** | Every 1 hour | Evaluates strategies on a rolling window. Disables underperformers, re-trials previously disabled ones, auto-creates GitHub issues for strategies disabled >14 days |

---

## Strategies

### Momentum
| Strategy | Entry condition | Tier |
|---|---|---|
| `momentum_swing` | Price +2% in 1h with 2x volume spike above rolling baseline | Swing |
| `momentum_scalp` | Price +2.5% in 5m, freshness-gated: 40%+ of the move must be in the last 2m | Scalp |

### Mean reversion
| Strategy | Entry condition | Tier |
|---|---|---|
| `mean_reversion` | Price >3% from VWAP + RSI <30 (long) or >3% above VWAP + RSI >70 (short) | Swing |
| `funding_extreme` | Perp funding >0.1%/8h (over-leveraged longs) or <-0.05% (short squeeze setup) | Swing |
| `correlation_break` | Alt deviates >3% from its rolling BTC regression baseline | Swing |
| `fear_greed_contrarian` | Fear & Greed <=15 (buy panic) or >=85 (sell euphoria) — BTC/ETH only | Swing |

### Event-driven
| Strategy | Entry condition | Tier |
|---|---|---|
| `listing_pump` | New listing on Coinbase / Binance / Kraken / Bybit within 30m of announcement | Swing |
| `whale_accumulation` | Net whale outflow from exchanges >$5M in 2h (accumulation) or >$10M inflow (distribution) | Swing |
| `liquidation_cascade` | >$2M longs liquidated in 10m + OI falling — cascade short; exhaustion dip buy | Scalp/Swing |

### Structural
| Strategy | Entry condition | Tier |
|---|---|---|
| `orderbook_imbalance` | Bid/ask depth ratio >3x within 1% of price — scalp the wall | Scalp |
| `narrative_momentum` | Social velocity for a sector spikes 3x — buy the sector laggard | Swing |
| `protocol_revenue` | DeFiLlama: protocol fees 2x above 7d avg, token hasn't moved yet | Swing |

---

## Signal sources

| Source | What it provides | Used by |
|---|---|---|
| **Coinbase Advanced** | Real-time ticks + L2 order book via WebSocket | All price-action strategies |
| **LunarCrush** | Galaxy score, social velocity, sentiment breakdown, AltRank, topic analysis | `narrative_momentum`, qualification scorer |
| **CryptoPanic** | News headlines with community votes, token-filtered | Qualification scorer, news gate |
| **Whale Alert** | On-chain transfers >$3M, classified by wallet type | `whale_accumulation` |
| **Binance Futures** | Funding rates, open interest, real-time liquidation stream | `funding_extreme`, `liquidation_cascade` |
| **DeFiLlama** | Daily fees for 2000+ protocols | `protocol_revenue` |
| **Alternative.me** | Fear & Greed Index (updated daily, free, no auth) | `fear_greed_contrarian`, qualification scorer |

All signal fetchers use per-endpoint circuit breakers (3 failures -> 5-min reset) and return stale cache warnings after 2x TTL.

---

## Qualification scoring

Before any trade executes, a multi-signal scorer aggregates the strategy's base score with four independent signal sources:

```
final_score = base_strategy_score
            + news_adjustment      (-15 to +15, from CryptoPanic sentiment)
            + social_adjustment    (-12 to +12, from LunarCrush galaxy score + velocity + AltRank)
            + context_adjustment   (-10 to +10, from market phase + BTC dominance)
            + fear_greed_alignment (-8  to +8,  directional agreement with trade side)
```

Minimum qualifying scores: swing = 55, scalp = 45.

---

## Position sizing

Quarter-Kelly sizing reduces variance vs. full Kelly while preserving long-term growth:

```
rawKelly    = (b*p - q) / b           where b = avg_win/avg_loss, p = win_rate, q = 1-p
kellySize   = rawKelly * 0.25
usdSize     = kellySize * portfolioUsd * qual_score_multiplier
finalSize   = clamp(usdSize, $10, MAX_POSITION_USD)
```

Until a strategy accumulates >=10 closed trades, it falls back to conservative 1% fixed-fractional sizing.

Graduated drawdown scaling: 5% drawdown -> 75% size, 10% -> 50%, 15% -> 25%, 20%+ -> 10%.

---

## Risk protections

The protection chain is declarative — protections are composable rules evaluated in order:

| Protection | Default | Description |
|---|---|---|
| `daily_loss_limit` | -$300 | Blocks new positions when daily realized P&L exceeds limit |
| `max_open_positions` | 5 | Limits concurrent open positions |
| `cooldown_after_loss` | 60s | Pause after a losing trade |

Each protection implements `check(ctx) -> Verdict`. The chain short-circuits on first block.

---

## Technical indicators

Built-in indicators computed from raw tick data (no external dependencies):

| Indicator | Module |
|---|---|
| ATR, EMA, Bollinger Bands, MACD, ADX, OBV, RSI | `src/indicators/core.py` |
| Cumulative Volume Delta (CVD) | `src/indicators/cvd.py` |
| Market regime classification (trend/range/volatile) | `src/indicators/regime.py` |

---

## Thread architecture

The bot runs as a single process with multiple daemon threads:

```
Main thread
├── CoinbaseWebSocket thread       (price ticks + L2 book)
├── Exit checker thread            (every 5s — trailing stops, max hold)
├── Market context refresh thread  (every 2min — fear/greed)
├── Signal refresh thread          (every 3min — news, social, funding, whale, protocol)
├── Self-healing analysis thread   (every 60min — Claude)
├── Strategy evaluation thread     (every 1h — Darwinian selector + delta evaluator)
├── Health check HTTP server       (port 8080)
└── Thread health monitor          (every 30s — detect and restart dead threads)
```

Coordination: `threading.Event` for shutdown, `Lock`/`RLock` on shared state, `threading.local()` for batch flags, `queue.Queue` for Convex background flush.

---

## Storage

**SQLite** (primary) — WAL mode, append-only audit trail, zero infrastructure. Tables: `positions`, `trades`, `logs`, `diagnoses`, `scanner_config_history`.

**Convex** (optional) — real-time subscriptions for the dashboard via `useQuery`. Fire-and-forget dual-write: if Convex is down, SQLite continues. Includes 15-minute metric aggregation cron.

---

## Project structure

```
src/
├── types.py                         All shared types (dataclasses)
├── config.py                        Parameter defaults, hard bounds, env vars
├── main.py                          Trading pipeline + health check + thread monitor
│
├── strategies/                      One file per strategy (11 total)
│   ├── registry.py                  Auto-discovery — scan for scan_*/on_* functions
│   ├── momentum.py                  Rolling price/volume, freshness gate
│   ├── mean_reversion.py            VWAP + RSI(14)
│   ├── funding_extreme.py           OI change tracking, annualized rate
│   ├── liquidation_cascade.py       Cascade rider + exhaustion dip buyer
│   ├── orderbook_imbalance.py       L2 book, bid/ask depth ratio
│   ├── whale_tracker.py             2h net flow window
│   └── ...                          correlation_break, listing_pump, narrative_momentum, etc.
│
├── signals/                         External API integrations (all with circuit breakers)
│   ├── _circuit_breaker.py          3-failure breaker with 5-min reset
│   ├── news.py                      CryptoPanic
│   ├── social.py                    LunarCrush
│   ├── whale.py                     Whale Alert
│   ├── funding.py                   Binance Futures
│   ├── fear_greed.py                Alternative.me
│   └── protocol.py                  DeFiLlama
│
├── feeds/
│   └── coinbase_ws.py               WebSocket with ping/pong, reconnect, thread-safe book
│
├── execution/
│   ├── coinbase.py                  Rate-limited, retries with backoff, idempotent orders
│   └── paper.py                     Slippage simulation, commission model
│
├── risk/
│   ├── portfolio.py                 Daily P&L tracking, drawdown, RLock-protected
│   ├── position_sizer.py            Quarter-Kelly with qual score multiplier
│   └── protections.py               Declarative protection chain
│
├── qualification/
│   └── scorer.py                    Multi-signal aggregation
│
├── evaluation/
│   ├── metrics.py                   Sharpe, Sortino, Calmar, profit factor, expectancy
│   └── strategy_selector.py         Darwinian enable/disable with probation + re-trial
│
├── self_healing/
│   ├── healer.py                    Rule-based: loss -> classify -> patch -> record delta
│   ├── log_analyzer.py              Claude: periodic deep analysis with chain-of-thought
│   ├── delta_evaluator.py           Track parameter changes, auto-revert if worsened
│   └── blind_spots.py               Fingerprint unknown losses, auto-create GitHub issues
│
├── indicators/
│   ├── core.py                      ATR, EMA, Bollinger Bands, MACD, ADX, OBV, RSI
│   ├── cvd.py                       Cumulative volume delta
│   └── regime.py                    Market regime classification
│
├── backtesting/
│   ├── data_loader.py               Binance klines with CSV caching
│   └── engine.py                    Configurable backtest engine
│
├── automation/
│   └── github_issues.py             Auto issue creation (blind spots, data gaps, underperformers)
│
├── storage/
│   ├── database.py                  SQLite — WAL mode, write locks, batch commits
│   ├── backend.py                   StorageBackend protocol + DualWriteBackend
│   └── convex_client.py             Convex SDK wrapper with background flush
│
└── utils/
    └── safe_math.py                 NaN/Inf guards for scoring and sizing

scripts/
├── analyze_logs.py                  Trigger Claude analysis manually
├── performance.py                   Metrics report (--csv for export)
└── backtest.py                      CLI backtesting tool

convex/                              Convex serverless functions
├── schema.ts                        8 tables
├── mutations.ts                     9 write mutations
├── queries.ts                       10 read queries for dashboard
├── crons.ts                         15-minute metric aggregation
└── aggregations.ts                  computeMetrics helper

docs/
├── architecture.md                  System design, Mermaid diagrams, design rationale
└── setup.md                         Installation, configuration, deployment guide
```

---

## Robustness

- **Thread safety**: All mutable global state protected with `Lock`/`RLock`. Per-thread batch flags via `threading.local()`. Double-checked locking on strategy registry.
- **Circuit breakers**: Per-endpoint breakers on all 6 signal fetchers (3 failures -> 5-min cooldown).
- **Division guards**: Every division in all 11 strategies guarded against zero denominators.
- **NaN/Inf guards**: `safe_score()`/`safe_ratio()` at scoring and sizing boundaries.
- **Memory bounds**: All rolling buffers capped with expiry-based purging.
- **Execution safety**: Rate limiting, exponential backoff retries, idempotent order IDs, partial fill handling.
- **Graceful shutdown**: Signal handler drains threads, closes DB + backends, flushes pending writes.
- **Thread monitoring**: Dead threads auto-restart with logging.

---

## Getting started

See [`docs/setup.md`](docs/setup.md) for installation, configuration, deployment, and adding custom strategies.

## License

MIT
