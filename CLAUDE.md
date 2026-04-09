# Self-Healing Crypto Trader — Claude Code Instructions

This file tells Claude Code how to analyze trading logs and improve the system.

## What this project is

A Python autonomous crypto trading system that:
1. Runs multiple trading strategies in parallel
2. Heals itself in real-time: after each loss, it diagnoses WHY and patches its own parameters
3. Periodically calls Claude via the Anthropic SDK to do deeper log analysis
4. Stores all trades, logs, and parameter change history in Convex (cloud database)

## How to analyze trading logs

All data is stored in Convex. Use the Convex dashboard or the bot's read APIs to query data.

The `src/storage/database.py` facade exposes these read functions:
- `get_open_positions()` — all currently open positions
- `get_closed_trades(limit=200)` — recent closed positions with P&L
- `get_recent_logs(limit=500, level=None)` — system logs, optionally filtered by level
- `get_recent_diagnoses(limit=50)` — self-healer diagnosis records
- `get_trade_journal(limit=50)` — structured exit analysis entries

For direct Convex queries, use the Convex dashboard at your `CONVEX_URL`.

## What to look for

When analyzing, focus on:

1. **Strategies with <45% win rate** — consider raising their min qual score or disabling
2. **Repeated loss reasons** — if `entered_pump_top` appears >3 times, momentum threshold needs raising
3. **Stop too tight** — exits in <2h at small losses suggest base trail too small for that tier
4. **Orphaned long holds** — positions held >12h that didn't hit target suggest mean reversion strategies need tighter time limits
5. **Funding squeeze losses** — check if `funding_extreme` strategy is entering against trend
6. **Correlation break failures** — if correlation break trades lose in trending markets, add market phase filter
7. **Narrative timing** — are narrative momentum trades entering too late in the pump?

## How to improve the system

You can directly edit `src/config.py` to change `default_scanner_config` values.
Always stay within the `CONFIG_BOUNDS` defined in that file.

You can add new loss reason patterns to `src/self_healing/healer.py`:
- Add a new `LossReason` type in `src/types.py`
- Add detection logic in `_classify_loss_reason()`
- Add an adaptation action in `_apply_loss_adaptation()`

To add a new strategy:
1. Create `src/strategies/your_strategy.py` following the existing pattern
2. Export a `scan_your_strategy()` function returning `Optional[TradeSignal]`
3. The strategy registry auto-discovers `scan_*` / `on_*` functions — no manual registration needed
4. Optionally add a `STRATEGY_META` dict for richer metadata
5. Add the `StrategyId` to `src/types.py`

## Running analysis

```bash
# One-time manual analysis (requires ANTHROPIC_API_KEY in .env)
python3 scripts/analyze_logs.py

# Or let it run automatically every N minutes (set in .env)
LOG_ANALYSIS_INTERVAL_MINS=60 python3 -m src.main

# Performance report
python3 scripts/performance.py
python3 scripts/performance.py --last 50
python3 scripts/performance.py --csv
```

## Key files

| File | Purpose |
|------|---------|
| `src/types.py` | All Python types — start here |
| `src/config.py` | Parameter defaults + bounds |
| `src/self_healing/healer.py` | Real-time loss diagnosis + parameter patching |
| `src/self_healing/log_analyzer.py` | Claude-powered deep analysis loop |
| `src/strategies/` | One file per trading strategy |
| `src/storage/database.py` | Storage facade — delegates to Convex |
| `src/storage/convex_client.py` | Convex SDK wrapper with background flush |

## Safety rules for Claude Code

- **Never** change `CONFIG_BOUNDS` — these are hard safety limits
- **Never** disable paper trading (`PAPER_TRADING=false`) without user confirmation
- **Never** change exchange API keys
- **Always** read the current config before suggesting changes
- When suggesting parameter changes, show the current value, proposed value, and evidence from the data

<!-- convex-ai-start -->
This project uses [Convex](https://convex.dev) as its backend.

When working on Convex code, **always read `convex/_generated/ai/guidelines.md` first** for important guidelines on how to correctly use Convex APIs and patterns. The file contains rules that override what you may have learned about Convex from training data.

Convex agent skills for common tasks can be installed by running `npx convex ai-files install`.
<!-- convex-ai-end -->
