<div align="center">

# kaizen-trader

**An autonomous crypto trading engine that improves itself after every trade.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org/)
[![Claude](https://img.shields.io/badge/Powered%20by-Claude%20Opus-8A2BE2?logo=anthropic)](https://anthropic.com)
[![Paper Trading](https://img.shields.io/badge/Paper%20Trading-default-orange)](.env.example)

[**Live Portfolio Dashboard →**](https://prateekjain.io/kaizen-trader)

</div>

---

> **Disclaimer:** This is experimental software built for research purposes. Crypto trading involves significant risk of loss. The authors assume no responsibility for trading outcomes. **Always run with `PAPER_TRADING=true` first.**

---

## What it is

A self-improving crypto trading system. Every 60 seconds it ingests 11 free data streams, scores opportunities with a rule-based brain (or optionally Claude Haiku), executes on Binance Futures or OKX, and adapts its own parameters after every loss.

**Live results:** $37.36 → $44.17 (+18.2%) with 1x leverage. Best strategy: funding squeeze (ENJ +26%, MBOX +25%).

---

## Architecture

```
                          ┌──────────────────────┐
                          │   Convex (DB)        │
                          │   positions, trades  │
                          │   logs, diagnoses    │
                          └──────┬───────────────┘
                                 │
┌────────────────────────────────┼────────────────────────────────┐
│  Trading Engine (python -m src.engine.runner)                   │
│                                │                                │
│  ┌─────────────┐    ┌─────────┴──────┐    ┌─────────────────┐  │
│  │ DataStreams  │───>│ SignalDetector  │───>│  Brain          │  │
│  │ 11 free APIs│    │ filter + rank   │    │  RuleBrain ($0) │  │
│  └─────────────┘    └────────────────┘    │  or ClaudeBrain │  │
│                                           │  (~$0.50/day)   │  │
│  ┌──────────────────┐                     └────────┬────────┘  │
│  │ CorrelationScanner│                              │           │
│  │ hourly, 197% CAGR │─────────────────────────────>│           │
│  └──────────────────┘                              │           │
│                                           ┌────────┴────────┐  │
│                                           │    Executor     │  │
│                                           │ Binance / OKX   │  │
│                                           │ paper or live   │  │
│                                           └─────────────────┘  │
│                                                                 │
│  ┌────────────────────┐    ┌────────────────────────────────┐  │
│  │ Self-Healing       │    │ watchdog.py                    │  │
│  │ loss → diagnose →  │    │ stop-loss safety net between   │  │
│  │ patch parameters   │    │ sessions                       │  │
│  └────────────────────┘    └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/your-org/kaizen-trader.git
cd kaizen-trader
pip install -r requirements.txt

# 2. Copy env template and fill in your keys
cp .env.example .env

# 3. Paper trading (no API keys needed)
python -m src.engine.runner --tick 60

# 4. Live trading on Binance
python -m src.engine.runner --live --auto-balance --tick 60

# 5. Live trading on OKX
EXCHANGE=okx python -m src.engine.runner --live --auto-balance --tick 60

# 6. Watchdog (safety net between sessions)
python watchdog.py
```

---

## Data streams

All 11 streams are free and require no authentication:

| # | Source | Data | Refresh |
|---|--------|------|---------|
| 1 | Binance WebSocket | Price, volume, order book, funding | Real-time |
| 2 | CoinGecko trending | Hottest tokens | 10 min |
| 3 | DexScreener | DEX volume spikes, new pairs | 5 min |
| 4 | Alternative.me | Fear & Greed Index | 1 hour |
| 5 | Binance funding | Extreme funding rates | 1 min |
| 6 | Binance listings | New token announcements | 1 min |
| 7 | Coinbase listings | New Coinbase products | 1 min |
| 8 | LunarCrush | Social sentiment, galaxy scores | Periodic |
| 9 | Reddit | Crypto subreddit sentiment | Periodic |
| 10 | Global market | BTC dominance, total cap | Periodic |
| 11 | Top movers | Binance 24h gainers/losers | Periodic |

---

## Brains

| Brain | Activation | Cost | How it works |
|---|---|---|---|
| **RuleBrain** | Default (no `ANTHROPIC_API_KEY`) | $0 | 12-factor scoring: 1h acceleration, funding squeeze detection, late-pump penalty, chop exit, strategy-specific stops/targets |
| **ClaudeBrain** | Set `ANTHROPIC_API_KEY` | ~$0.50/day | Haiku sees all positions, signals, and market regime every tick; returns structured BUY/CLOSE/hold decisions |

---

## Key strategies

| Strategy | Description | Live results |
|---|---|---|
| Correlation break | Hourly BTC-alt regression divergence | 197% CAGR (backtested) |
| Funding squeeze | Extreme perp funding + acceleration | ENJ +26%, MBOX +25% |
| Momentum breakout | 1h acceleration as primary signal | Fresh breakouts only |
| Listing pump | New exchange listings within 30 min | Event-driven |

Protective mechanisms:
- **Chop exit** -- cuts trades after 1h with <2% movement
- **Late-pump penalty** -- skips tokens already up +100% in 24h
- **1x leverage** always enforced
- **Server-side stops** on Binance

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAPER_TRADING` | No | `true` | Set to `false` for live execution |
| `EXCHANGE` | No | `binance` | `binance` or `okx` |
| `BINANCE_API_KEY` | For live Binance | -- | Binance Futures API key |
| `BINANCE_API_SECRET` | For live Binance | -- | Binance Futures API secret |
| `OKX_API_KEY` | For live OKX | -- | OKX API key |
| `OKX_API_SECRET` | For live OKX | -- | OKX API secret |
| `ANTHROPIC_API_KEY` | No | -- | Enables ClaudeBrain (~$0.50/day). Without it, RuleBrain is used at $0 cost |
| `CONVEX_URL` | For prod | -- | Convex deployment URL for persistent storage |

---

## Self-healing loops

| Loop | Frequency | What it does |
|---|---|---|
| **L1 -- Rule-based healer** | After every loss | Classifies why the trade lost and patches the responsible parameter |
| **L2 -- Claude analysis** | Every 60 min | Reads trades + diagnoses, computes metrics, returns structured JSON patches |
| **L3 -- Delta evaluator** | Every 2 hours | Auto-reverts parameter changes that worsened performance after 10+ trades |
| **L4 -- Darwinian selector** | Every 1 hour | Disables underperformers, re-trials previously disabled strategies |

---

## Project structure

```
src/
├── engine/                              Live trading engine (primary)
│   ├── runner.py                        Entry point: DataStreams → Brain → Executor
│   ├── rule_brain.py                    RuleBrain — $0, 12-factor scoring
│   ├── claude_brain.py                  ClaudeBrain — LLM-powered decisions
│   ├── data_streams.py                  11 free data stream ingestion
│   ├── signal_detector.py               Signal filtering and ranking
│   ├── correlation_scanner.py           Hourly correlation break scanner
│   ├── executor.py                      Order execution (Binance/OKX/paper)
│   ├── binance_ws.py                    Binance WebSocket client
│   └── log.py                           Structured logging
│
├── main.py                              Legacy self-healing loop (kept for reference)
├── types.py                             All shared types (dataclasses)
├── config.py                            Parameter defaults, bounds, env vars
│
├── strategies/                          One file per strategy (14 total)
│   ├── registry.py                      Auto-discovery via scan_*/on_* functions
│   ├── momentum.py, mean_reversion.py, funding_extreme.py, ...
│   └── correlation_break.py, listing_pump.py, narrative_momentum.py, ...
│
├── self_healing/
│   ├── healer.py                        Rule-based loss diagnosis + parameter patching
│   ├── log_analyzer.py                  Claude-powered deep analysis
│   ├── delta_evaluator.py               Auto-revert bad parameter changes
│   └── blind_spots.py                   Unknown loss fingerprinting
│
├── signals/                             External API integrations (circuit breakers)
├── execution/                           Exchange providers (Binance, OKX, paper)
├── risk/                                Portfolio tracking, position sizing, protections
├── indicators/                          ATR, EMA, Bollinger, MACD, RSI, CVD, regime
├── evaluation/                          Sharpe/Sortino/Calmar, strategy selector
├── storage/                             Convex database facade
└── utils/                               Math guards, symbol helpers, cache

watchdog.py                              Stop-loss watchdog between sessions
scripts/                                 analyze_logs.py, performance.py, backtest.py
convex/                                  Serverless functions, schema, crons
```

---

## License

MIT
