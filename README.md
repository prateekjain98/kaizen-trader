<div align="center">

# kaizen-trader

**An autonomous crypto trading engine that improves itself after every trade.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org/)
[![Claude](https://img.shields.io/badge/Powered%20by-Claude%20Opus-8A2BE2?logo=anthropic)](https://anthropic.com)
[![Paper Trading](https://img.shields.io/badge/Paper%20Trading-default-orange)](.env.example)
[![Tests](https://img.shields.io/badge/Tests-346%20passing-brightgreen)]()

</div>

---

> **Disclaimer:** This is experimental software built for research purposes. Crypto trading involves significant risk of loss. The authors assume no responsibility for trading outcomes. **Always run with `PAPER_TRADING=true` first.**

---

## What it is

Most trading systems are static: they run the same rules until you manually tune them. kaizen-trader takes a different approach — it continuously analyzes its own trade history, patches its parameters, tracks whether those changes helped or hurt, and creates GitHub issues when it identifies missing data or blind spots.

There are four feedback loops running in parallel:

**Loop 1 — Immediate (after every loss):**
The system classifies why a trade lost and adjusts the responsible parameter before the next trade. Entered the top of a pump? Momentum threshold goes up. Stop hit in under 2 hours? Trail widens.

**Loop 2 — Periodic (every 60 minutes via Claude):**
Claude reads the full trade history, computes Sharpe/Sortino/Calmar ratios, and reasons through patterns that the rule-based healer can't detect — time-of-day effects, strategy interaction issues, signal quality drift. Returns structured JSON patches with chain-of-thought reasoning.

**Loop 3 — Delta evaluation (every 2 hours):**
Every parameter change (from Loop 1 or 2) is recorded with a before/after trade snapshot. After 10+ subsequent trades, the evaluator checks if the change improved or worsened performance. Worsened changes are auto-reverted.

**Loop 4 — Darwinian strategy selection (every 1 hour):**
Strategies are evaluated on a rolling window. Underperformers are disabled, previously disabled strategies are re-trialed. Strategies disabled for >14 days trigger an automatic GitHub issue.

The full audit trail — every trade, every diagnosis, every config snapshot, every parameter delta — is stored in SQLite locally, with optional dual-write to Convex for a real-time dashboard.

---

## Architecture

```
┌─────────────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
│  Railway: Python Bot        │      │  Convex: Real-time DB    │      │  Vercel: Dashboard       │
│  ($20/mo)                   │─────▶│  (Free tier)             │◀─────│  prateekjain.io/         │
│                             │      │                          │      │    kaizen-trader         │
│  - Full trading pipeline    │      │  - positions, trades     │      │                          │
│  - WebSocket feeds          │      │  - logs, diagnoses       │      │  - Live portfolio via    │
│  - Self-healing + deltas    │      │  - parameter_deltas      │      │    Coinbase + Binance    │
│  - Claude analysis          │      │  - config_history        │      │  - Real-time positions   │
│  - Strategy evaluation      │      │  - 15-min metric cron    │      │  - AI chat (Vercel SDK)  │
│  - Auto GitHub issues       │      │                          │      │                          │
│  - Health check (:8080)     │      │                          │      │                          │
└─────────────────────────────┘      └──────────────────────────┘      └──────────────────────────┘
```

Three stages run continuously in a single Python process:

**1 — Signal ingestion:**
Coinbase WebSocket streams price ticks and L2 order book updates. Six external APIs (CryptoPanic, LunarCrush, Whale Alert, Binance Futures, DeFiLlama, Alternative.me) poll on independent schedules and update an in-memory `MarketContext`. Each signal fetcher has a circuit breaker (3 failures → 5-minute cooldown).

**2 — Trade engine:**
On every tick (throttled to 2s/symbol), 11 strategy scanners run against the current price and context. Strategies are auto-discovered from `src/strategies/` via the registry. Signals pass the qualification scorer (multi-signal aggregation), are sized via quarter-Kelly, and routed to the executor (paper by default, Coinbase Advanced REST in live mode). A declarative protection chain blocks new positions when daily loss limits, max positions, or cooldowns are hit.

**3 — Self-healing + autonomous improvement:**
After every loss: classify, patch, record delta, check blind spots. Every hour: Claude deep analysis, delta evaluation, strategy evaluation. When blind spots accumulate or strategies chronically underperform: auto-create GitHub issues.

The full data flow is documented in [`docs/architecture.md`](docs/architecture.md).

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
| **LunarCrush** | Galaxy score, social velocity, sentiment breakdown, AltRank, topic analysis, time series | `narrative_momentum`, qualification scorer |
| **CryptoPanic** | News headlines with community votes, token-filtered | `mean_reversion` news gate, qualification scorer |
| **Whale Alert** | On-chain transfers >$3M, classified by wallet type | `whale_accumulation` |
| **Binance Futures** | Funding rates, open interest, real-time liquidation stream | `funding_extreme`, `liquidation_cascade` |
| **DeFiLlama** | Daily fees for 2000+ protocols | `protocol_revenue` |
| **Alternative.me** | Fear & Greed Index (updated daily, free, no auth) | `fear_greed_contrarian`, qualification scorer |
| **Coinbase Advanced** | Real-time ticks + L2 order book via WebSocket | All price-action strategies |

All signal fetchers use per-endpoint circuit breakers (3 failures → 5-min reset) and return stale cache warnings after 2x TTL.

---

## Qualification scorer

Before any trade executes, a multi-signal scorer aggregates the strategy's base score with four independent signal sources:

```
final_score = base_strategy_score
            + news_adjustment      (-15 to +15, from CryptoPanic sentiment)
            + social_adjustment    (-12 to +12, from LunarCrush galaxy score + velocity + AltRank + sentiment)
            + context_adjustment   (-10 to +10, from market phase + BTC dominance)
            + fear_greed_alignment (-8  to +8,  directional agreement with trade side)
```

Social scoring uses discrete thresholds: galaxy score >60 adds +5, velocity >30 adds +3 / >50 adds +7, AltRank improving >20 adds +4, negative sentiment >70% penalizes longs -5, social volume doubling adds +3.

---

## Kelly position sizing

```
rawKelly    = (b*p - q) / b           where b = avg_win/avg_loss, p = win_rate, q = 1-p
kellySize   = rawKelly * 0.25         quarter-Kelly (reduces variance significantly)
usdSize     = kellySize * portfolioUsd * qual_score_multiplier
finalSize   = clamp(usdSize, $10, MAX_POSITION_USD)
```

Until a strategy accumulates >=10 closed trades, it falls back to conservative 1% fixed-fractional sizing.

---

## Self-healing detail

```
After every loss:
  healer.on_position_closed()
    classify_loss_reason()        -> entered_pump_top | stop_too_tight | low_qual_score | ...
    apply_loss_adaptation()       -> patch one parameter in the live config
    record_delta()                -> capture before/after trade snapshot for evaluation
    insert_diagnosis()            -> write to SQLite (+ Convex if configured)
    check_blind_spots()           -> fingerprint unknown losses, auto-create GitHub issue at threshold

Every 60 minutes (Claude analysis):
    compute_metrics()             -> Sharpe, Sortino, win rates, Kelly per strategy
    build_prompt()                -> metrics + trades + diagnoses + deltas + blind spots
    Claude Opus                   -> chain-of-thought reasoning -> JSON response
    Pydantic validation           -> reject malformed responses
    confidence filter             -> skip low-confidence changes
    apply_changes() + record_delta() -> patch config with tracking
    create GitHub issues          -> for data gap suggestions from Claude

Every 2 hours (delta evaluation):
    evaluate_pending_deltas()     -> for each delta with 10+ post-change trades:
                                     compare before/after win_rate + avg_pnl
                                     revert if worsened (max 1 revert/cycle)

Every 1 hour (strategy selection):
    evaluate_strategies()         -> disable underperformers, re-trial disabled
    create GitHub issues          -> for strategies disabled >14 days
```

---

## Risk protections

The protection chain is declarative — protections are defined as a list and evaluated in order:

| Protection | Default | Description |
|---|---|---|
| `daily_loss_limit` | -$300 | Blocks new positions when daily realized P&L exceeds limit |
| `max_open_positions` | 5 | Limits concurrent open positions |
| `cooldown_after_loss` | 60s | Pause after a losing trade |

Protections are composable — add/remove/reorder via config. Each protection implements `can_open()` and optionally `notify_close()` / `reset_day()`.

---

## Backtesting

```bash
python scripts/backtest.py --symbol BTC --start 2025-01-01 --end 2025-06-01
python scripts/backtest.py --symbol ETH --start 2025-03-01 --end 2025-06-01 --commission 0.001
```

The backtesting engine:
- Fetches historical OHLCV data from Binance public API (free, no auth)
- Caches locally as CSV to avoid re-downloading
- Simulates qualification, sizing, execution with configurable slippage + commission
- Outputs: total P&L, win rate, Sharpe ratio, max drawdown, trade count

---

## Project structure

```
src/
├── types.py                         All shared types (dataclasses) — start here
├── config.py                        Parameter defaults, hard bounds, validation
├── main.py                          Full trading pipeline + health check + thread monitor
│
├── strategies/                      One file per strategy (11 total)
│   ├── registry.py                  Auto-discovery — scan for scan_*/on_* functions
│   ├── momentum.py                  Rolling price/volume buffers, freshness gate
│   ├── mean_reversion.py            VWAP computation, RSI(14) from scratch
│   ├── listing_pump.py              Multi-exchange detection, freshness scoring
│   ├── whale_tracker.py             2h net flow window, wallet type classification
│   ├── funding_extreme.py           OI change tracking, annualized rate display
│   ├── liquidation_cascade.py       Cascade rider + exhaustion dip buyer
│   ├── orderbook_imbalance.py       In-memory L2 book, bid/ask depth ratio
│   ├── narrative_momentum.py        10 sector definitions, linear regression laggard
│   ├── correlation_break.py         Rolling BTC/alt regression, divergence detection
│   ├── protocol_revenue.py          DeFiLlama revenue multiple scoring
│   └── fear_greed_contrarian.py     Extreme index plays, re-entry gate
│
├── signals/                         Real API integrations (all with circuit breakers)
│   ├── _circuit_breaker.py          3-failure circuit breaker with 5-min reset
│   ├── news.py                      CryptoPanic — headline scoring + vote analysis
│   ├── social.py                    LunarCrush — galaxy score, velocity, topic, time series
│   ├── whale.py                     Whale Alert — wallet type classification, net flow
│   ├── funding.py                   Binance Futures — funding rates, OI change tracking
│   ├── fear_greed.py                Alternative.me — index + delta1d
│   └── protocol.py                  DeFiLlama — protocol revenue spike detection
│
├── feeds/
│   └── coinbase_ws.py               WebSocket with ping/pong, max reconnect, thread-safe book
│
├── execution/
│   ├── coinbase.py                  Rate-limited, retries with backoff, idempotent orders
│   └── paper.py                     Thread-safe slippage simulation, commission model
│
├── risk/
│   ├── portfolio.py                 Daily P&L tracking, RLock-protected, Sharpe/drawdown
│   ├── position_sizer.py            Quarter-Kelly with qual score multiplier
│   └── protections.py               Declarative protection chain (daily loss, max positions, cooldown)
│
├── qualification/
│   └── scorer.py                    Multi-signal aggregation with enhanced social scoring
│
├── evaluation/
│   ├── metrics.py                   Sharpe, Sortino, Calmar, profit factor, Kelly per strategy
│   └── strategy_selector.py         Darwinian enable/disable with chronic underperformer issues
│
├── self_healing/
│   ├── healer.py                    Rule-based: immediate loss -> parameter patch + delta recording
│   ├── log_analyzer.py              Claude: periodic deep analysis with chain-of-thought
│   ├── delta_evaluator.py           Track parameter changes, auto-revert if worsened
│   └── blind_spots.py               Fingerprint unknown losses, auto-create GitHub issues
│
├── automation/
│   └── github_issues.py             Auto GitHub issue creation (blind spots, data gaps, underperformers)
│
├── backtesting/
│   ├── data_loader.py               Binance klines with CSV caching
│   └── engine.py                    BacktestConfig, BacktestResult, BacktestEngine
│
├── storage/
│   ├── database.py                  SQLite — WAL mode, write locks, batch commits, dual-write
│   ├── backend.py                   StorageBackend protocol + DualWriteBackend
│   └── convex_client.py             Python Convex SDK wrapper with background flush queue
│
└── utils/
    └── safe_math.py                 NaN/Inf guards for scoring and sizing

scripts/
├── analyze_logs.py                  Trigger Claude analysis manually
├── performance.py                   Print metrics report (--csv flag for export)
└── backtest.py                      CLI backtesting tool

convex/                              Convex serverless functions (JS/TS)
├── schema.ts                        8 tables: positions, trades, logs, diagnoses, ...
├── mutations.ts                     9 write mutations
├── queries.ts                       10 read queries for dashboard
├── crons.ts                         15-minute metric aggregation
└── aggregations.ts                  computeMetrics internal function

docs/
└── architecture.md                  System design, data flows, key decisions

CLAUDE.md                            Instructions for Claude Code to query and improve strategies
```

---

## Quick start

### Prerequisites

- Python 3.11+
- SQLite (included with Python)
- Coinbase Advanced Trade account (for price feed)
- Anthropic API key (for self-healing)

### Install

```bash
git clone https://github.com/prateekjain98/kaizen-trader
cd kaizen-trader

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env — minimum: ANTHROPIC_API_KEY + COINBASE_API_KEY/SECRET
# PAPER_TRADING=true by default
```

### Run

```bash
python -m src.main
```

The bot starts all threads (WS feed, signal fetchers, exit checker, self-healing, strategy evaluation) and exposes a health endpoint at `http://localhost:8080/health`.

### Run tests

```bash
python -m pytest tests/ -v
```

346 tests covering strategies, risk, self-healing, storage, backtesting, automation, and social integration.

### Manual analysis

```bash
# Trigger Claude log analysis
python scripts/analyze_logs.py

# Performance report
python scripts/performance.py
python scripts/performance.py --last 50
python scripts/performance.py --csv > trades.csv

# Backtest a strategy
python scripts/backtest.py --symbol BTC --start 2025-01-01 --end 2025-06-01
```

### Query the database

```bash
# Win rate by strategy
sqlite3 trader.db "
  SELECT strategy,
    COUNT(*) as trades,
    ROUND(100.0 * SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_pct,
    ROUND(SUM(pnl_usd), 2) as total_pnl
  FROM positions WHERE status='closed'
  GROUP BY strategy ORDER BY total_pnl DESC;"

# Recent self-healer actions
sqlite3 trader.db "
  SELECT symbol, loss_reason, action, datetime(timestamp/1000, 'unixepoch') as at
  FROM diagnoses ORDER BY timestamp DESC LIMIT 20;"
```

---

## Configuration

```bash
PAPER_TRADING=true                # always start here

# Required
ANTHROPIC_API_KEY=                # Claude log analysis
COINBASE_API_KEY=                 # price feed + execution
COINBASE_API_SECRET=

# Recommended
LUNARCRUSH_API_KEY=               # social signals (Twitter/Reddit/YouTube/TikTok)
CRYPTOPANIC_TOKEN=                # news sentiment

# Optional — enables additional strategies
BINANCE_API_KEY=                  # funding_extreme, liquidation_cascade
BINANCE_API_SECRET=
WHALE_ALERT_API_KEY=              # whale_accumulation

# Risk limits
MAX_POSITION_USD=100
MAX_DAILY_LOSS_USD=300
MAX_OPEN_POSITIONS=5

# Self-healing schedule
LOG_ANALYSIS_INTERVAL_MINS=60
MIN_TRADES_FOR_ANALYSIS=10

# Optional — dual-write to Convex for real-time dashboard
CONVEX_URL=

# Optional — auto GitHub issue creation
GITHUB_REPO=                      # e.g., prateekjain98/kaizen-trader

# Database
DB_PATH=trader.db

# Health check
PORT=8080
```

Strategies degrade gracefully when their data source isn't configured — the system runs on whatever signals are available.

---

## Adding a strategy

Every strategy is a function returning `Optional[TradeSignal]`:

```python
# src/strategies/my_strategy.py
import uuid
from typing import Optional
from src.types import TradeSignal, ScannerConfig, MarketContext

def scan_my_strategy(
    symbol: str,
    product_id: str,
    current_price: float,
    config: ScannerConfig,
    ctx: MarketContext,
) -> Optional[TradeSignal]:
    if ctx.fear_greed_index > 30:
        return None

    return TradeSignal(
        id=str(uuid.uuid4()),
        symbol=symbol,
        product_id=product_id,
        strategy="my_strategy",
        side="long",
        tier="swing",
        score=72,
        confidence="medium",
        sources=["fear_greed"],
        reasoning=f"{symbol} in extreme fear - contrarian entry",
        entry_price=current_price,
        stop_price=current_price * 0.95,
        suggested_size_usd=100,
        expires_at=int(time.time() * 1000) + 3_600_000,
        created_at=int(time.time() * 1000),
    )
```

The strategy registry auto-discovers any `scan_*` or `on_*` function in `src/strategies/`. No manual registration needed — just drop a file and restart.

For richer metadata, define a `STRATEGY_META` dict in the module:

```python
STRATEGY_META = {
    "strategies": [
        {"id": "my_strategy", "function": "scan_my_strategy", "tier": "swing", "description": "..."}
    ],
    "signal_sources": ["fear_greed"],
}
```

---

## Deployment

### Railway (recommended)

```bash
# Procfile already configured:
# bot: python -m src.main

# Push to Railway — auto-deploys from GitHub
railway up
```

Set environment variables in Railway dashboard. Health check at `/health` on port 8080. Persistent volume at `/data` for SQLite fallback.

### Local

```bash
python -m src.main
```

---

## Robustness

The codebase has been hardened for production:

- **Thread safety**: All mutable global state protected with `threading.Lock` / `RLock`. Per-thread batch flags via `threading.local()`. Double-checked locking on strategy registry.
- **Circuit breakers**: Per-endpoint breakers on all 6 signal fetchers (3 failures → 5-min cooldown).
- **Division guards**: Every division in all 11 strategies guarded against zero denominators.
- **NaN/Inf guards**: `safe_score()` / `safe_ratio()` at scoring and sizing boundaries.
- **Memory bounds**: All rolling buffers capped (`_MAX_SYMBOLS`, `_MAX_WINDOWS`, `_WINDOW_EXPIRY_MS`).
- **Execution safety**: Rate limiting with locks, exponential backoff retries, idempotent order IDs, partial fill handling, price/size sanity checks.
- **Graceful shutdown**: Signal handler drains threads, closes DB + backends, flushes pending writes.
- **Thread health monitoring**: Dead threads auto-restart with logging.
- **Batch commits**: `batch_writes()` context manager defers SQLite commits for bulk operations.

---

## License

MIT
