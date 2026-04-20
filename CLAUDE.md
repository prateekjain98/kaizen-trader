# Kaizen Trader -- Claude Code Instructions

This file tells Claude Code how to work with the kaizen-trader codebase.

## What this project is

A Python autonomous crypto trading system that:
1. Runs a live trading engine with 11 free data streams and rule-based or Claude-powered decision-making
2. Supports Binance Futures (default) and OKX via the `EXCHANGE` env var
3. Heals itself in real-time: after each loss, it diagnoses WHY and patches its own parameters
4. Optionally calls Claude via the Anthropic SDK for deeper analysis (~$0.50/day with Haiku)
5. Stores all trades, logs, and parameter change history in Convex (cloud database)

## Entry points

There are two entry points:

| Entry point | Purpose |
|---|---|
| `python -m src.engine.runner` | **Primary.** Live trading engine: DataStreams -> SignalDetector -> Brain -> Executor |
| `python -m src.main` | Legacy self-healing loop with Coinbase WS. Kept for reference but not the active path. |

## Brains

| Brain | When used | Cost |
|---|---|---|
| **RuleBrain** (`src/engine/rule_brain.py`) | Default -- when no `ANTHROPIC_API_KEY` is set | $0. Deterministic 12-factor scoring with hard-won rules from live trading |
| **ClaudeBrain** (`src/engine/claude_brain.py`) | When `ANTHROPIC_API_KEY` is set | ~$0.50/day with Haiku. LLM sees positions, signals, regime, and decides BUY/CLOSE/hold |

## Data streams (all free, no auth required)

1. Binance WebSocket -- real-time price, volume, order book, funding
2. CoinGecko trending -- hottest tokens (every 10 min)
3. DexScreener -- DEX volume spikes, new pairs (every 5 min)
4. Alternative.me Fear & Greed Index (every 1 hour)
5. Binance funding rates -- extreme funding (every 1 min)
6. Binance announcements -- new listings (every 1 min)
7. Coinbase products -- new Coinbase listings (every 1 min)
8. LunarCrush -- social sentiment and galaxy scores
9. Reddit -- crypto subreddit sentiment
10. Global market data -- BTC dominance, total market cap
11. Top movers -- Binance 24h top gainers/losers

## Key strategies

- **Correlation scanner** (`src/engine/correlation_scanner.py`) -- runs hourly, pure math, 197% CAGR backtested
- **Funding squeeze** -- highest conviction live setup (ENJ +26%, MBOX +25%)
- **Chop exit** -- cuts dead trades after 1h with <2% movement
- **Late-pump penalty** -- prevents chasing tokens already up +100%

Live trading results: $37.36 -> $44.17 (+18.2%). 1x leverage always enforced. Server-side stops on Binance.

## Running the system

```bash
# Live trading (Binance) -- primary usage
python -m src.engine.runner --live --auto-balance --tick 60

# Live trading (OKX)
EXCHANGE=okx python -m src.engine.runner --live --auto-balance --tick 60

# Paper trading (default, no API keys needed)
python -m src.engine.runner --tick 60

# Watchdog -- safety net for stop-loss between sessions
python watchdog.py

# Performance report
python3 scripts/performance.py
python3 scripts/performance.py --last 50
python3 scripts/performance.py --csv

# One-time Claude analysis (requires ANTHROPIC_API_KEY)
python3 scripts/analyze_logs.py

# Backtesting
python3 scripts/backtest.py
```

## How to analyze trading logs

All data is stored in Convex. Use the Convex dashboard or the bot's read APIs to query data.

The `src/storage/database.py` facade exposes these read functions:
- `get_open_positions()` -- all currently open positions
- `get_closed_trades(limit=200)` -- recent closed positions with P&L
- `get_recent_logs(limit=500, level=None)` -- system logs, optionally filtered by level
- `get_recent_diagnoses(limit=50)` -- self-healer diagnosis records
- `get_trade_journal(limit=50)` -- structured exit analysis entries

For direct Convex queries, use the Convex dashboard at your `CONVEX_URL`.

## What to look for

When analyzing, focus on:

1. **Strategies with <45% win rate** -- consider raising their min qual score or disabling
2. **Repeated loss reasons** -- if `entered_pump_top` appears >3 times, momentum threshold needs raising
3. **Stop too tight** -- exits in <2h at small losses suggest base trail too small for that tier
4. **Orphaned long holds** -- positions held >12h that didn't hit target suggest mean reversion strategies need tighter time limits
5. **Funding squeeze losses** -- check if `funding_extreme` strategy is entering against trend
6. **Correlation break failures** -- if correlation break trades lose in trending markets, add market phase filter
7. **Narrative timing** -- are narrative momentum trades entering too late in the pump?

## How to improve the system

You can directly edit `src/config.py` to change `default_scanner_config` values.
Always stay within the `CONFIG_BOUNDS` defined in that file.

To tune the RuleBrain:
- Edit constants in `src/engine/rule_brain.py` (score thresholds, stop/target percentages, chop timeout)
- The 12-factor scoring system and strategy risk tables are all in that file

To tune the live engine:
- Edit `src/engine/signal_detector.py` for signal filtering
- Edit `src/engine/correlation_scanner.py` for correlation break thresholds
- Edit `src/engine/executor.py` for execution behavior (stops, leverage)

You can add new loss reason patterns to `src/self_healing/healer.py`:
- Add a new `LossReason` type in `src/types.py`
- Add detection logic in `_classify_loss_reason()`
- Add an adaptation action in `_apply_loss_adaptation()`

To add a new strategy:
1. Create `src/strategies/your_strategy.py` following the existing pattern
2. Export a `scan_your_strategy()` function returning `Optional[TradeSignal]`
3. The strategy registry auto-discovers `scan_*` / `on_*` functions -- no manual registration needed
4. Optionally add a `STRATEGY_META` dict for richer metadata
5. Add the `StrategyId` to `src/types.py`

## Key files

| File | Purpose |
|------|---------|
| `src/engine/runner.py` | **Primary entry point** -- live trading loop |
| `src/engine/rule_brain.py` | RuleBrain -- $0 cost, 12-factor deterministic scoring |
| `src/engine/claude_brain.py` | ClaudeBrain -- LLM-powered decisions (~$0.50/day) |
| `src/engine/data_streams.py` | 11 free data stream ingestion |
| `src/engine/signal_detector.py` | Signal filtering and packaging |
| `src/engine/correlation_scanner.py` | Hourly correlation break scanner (197% CAGR) |
| `src/engine/executor.py` | Order execution (Binance/OKX, paper mode) |
| `src/types.py` | All Python types -- start here |
| `src/config.py` | Parameter defaults + bounds + env vars |
| `src/self_healing/healer.py` | Real-time loss diagnosis + parameter patching |
| `src/self_healing/log_analyzer.py` | Claude-powered deep analysis loop |
| `src/strategies/` | One file per strategy (14 total) |
| `src/storage/database.py` | Storage facade -- delegates to Convex |
| `src/storage/convex_client.py` | Convex SDK wrapper with background flush |
| `watchdog.py` | Stop-loss watchdog for between sessions |

## Safety rules for Claude Code

- **Never** change `CONFIG_BOUNDS` -- these are hard safety limits
- **Never** disable paper trading (`PAPER_TRADING=false`) without user confirmation
- **Never** change exchange API keys
- **Always** read the current config before suggesting changes
- When suggesting parameter changes, show the current value, proposed value, and evidence from the data

<!-- convex-ai-start -->
This project uses [Convex](https://convex.dev) as its backend.

When working on Convex code, **always read `convex/_generated/ai/guidelines.md` first** for important guidelines on how to correctly use Convex APIs and patterns. The file contains rules that override what you may have learned about Convex from training data.

Convex agent skills for common tasks can be installed by running `npx convex ai-files install`.
<!-- convex-ai-end -->
