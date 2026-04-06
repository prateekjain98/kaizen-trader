# Self-Healing Crypto Trader — Claude Code Instructions

This file tells Claude Code how to analyze trading logs and improve the system.

## What this project is

An autonomous crypto trading system that:
1. Runs multiple trading strategies in parallel
2. Heals itself in real-time: after each loss, it diagnoses WHY and patches its own parameters
3. Periodically calls Claude via the Anthropic SDK to do deeper log analysis
4. Stores all trades, logs, and parameter change history in `trader.db` (SQLite)

## How to analyze trading logs

When asked to analyze logs or improve the trader, do this:

### Step 1: Read the recent trade history
```bash
sqlite3 trader.db "
  SELECT strategy, side, tier, pnl_pct, hold_ms/3600000.0 as hold_h, exit_reason, qual_score
  FROM positions
  WHERE status='closed'
  ORDER BY closed_at DESC
  LIMIT 100;
"
```

### Step 2: Check win rates by strategy
```bash
sqlite3 trader.db "
  SELECT
    strategy,
    COUNT(*) as total,
    SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate_pct,
    ROUND(AVG(pnl_pct) * 100, 2) as avg_pnl_pct,
    ROUND(SUM(pnl_usd), 2) as total_pnl_usd
  FROM positions
  WHERE status='closed'
  GROUP BY strategy
  ORDER BY total_pnl_usd DESC;
"
```

### Step 3: Review self-healer diagnoses
```bash
sqlite3 trader.db "
  SELECT symbol, strategy, pnl_pct, loss_reason, action, timestamp
  FROM diagnoses
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

### Step 4: Look for patterns in errors/warnings
```bash
sqlite3 trader.db "
  SELECT level, message, symbol, ts
  FROM logs
  WHERE level IN ('error', 'warn')
  ORDER BY ts DESC
  LIMIT 50;
"
```

### Step 5: Check config evolution
```bash
sqlite3 trader.db "
  SELECT reason, config, timestamp
  FROM scanner_config_history
  ORDER BY timestamp DESC
  LIMIT 20;
"
```

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

You can directly edit `src/config.ts` to change `defaultScannerConfig` values.
Always stay within the `CONFIG_BOUNDS` defined in that file.

You can add new loss reason patterns to `src/self-healing/index.ts`:
- Add a new `LossReason` type in `src/types.ts`
- Add detection logic in `classifyLossReason()`
- Add an adaptation action in `applyLossAdaptation()`

To add a new strategy:
1. Create `src/strategies/your-strategy.ts` following the existing pattern
2. Export a `scanYourStrategy()` function returning `TradeSignal | null`
3. Register it in `src/strategies/index.ts`
4. Add the `StrategyId` to `src/types.ts`

## Running analysis

```bash
# One-time manual analysis (requires ANTHROPIC_API_KEY in .env)
npm run analyze

# Or let it run automatically every N minutes (set in .env)
LOG_ANALYSIS_INTERVAL_MINS=60 npm start
```

## Key files

| File | Purpose |
|------|---------|
| `src/types.ts` | All TypeScript types — start here |
| `src/config.ts` | Parameter defaults + bounds |
| `src/self-healing/index.ts` | Real-time loss diagnosis + parameter patching |
| `src/self-healing/log-analyzer.ts` | Claude-powered deep analysis loop |
| `src/strategies/` | One file per trading strategy |
| `trader.db` | SQLite: all trades, logs, config history |

## Safety rules for Claude Code

- **Never** change `CONFIG_BOUNDS` — these are hard safety limits
- **Never** disable paper trading (`PAPER_TRADING=false`) without user confirmation
- **Never** change exchange API keys
- **Always** read the current config before suggesting changes
- When suggesting parameter changes, show the current value, proposed value, and evidence from the data
