/**
 * Self-Healing AI Crypto Trader — Main Entry Point
 *
 * Architecture:
 *  ┌─ Price WebSocket ────────────────────────────────────────────────────┐
 *  │  Coinbase Advanced Trade (real-time ticks)                          │
 *  │  → pushPriceSample → all strategy scanners on every tick            │
 *  └──────────────────────────────────────────────────────────────────────┘
 *
 *  ┌─ Signal Sources (polled on intervals) ──────────────────────────────┐
 *  │  News (CryptoPanic)       every 5 min                               │
 *  │  Social (LunarCrush)      every 3 min                               │
 *  │  Whale alerts             every 2 min                               │
 *  │  Fear & Greed             every 30 min                              │
 *  │  Funding rates            every 5 min (Binance)                     │
 *  │  Protocol revenue         every 1h (DeFiLlama)                      │
 *  └──────────────────────────────────────────────────────────────────────┘
 *
 *  ┌─ Self-Healing Loops ────────────────────────────────────────────────┐
 *  │  After each position close → immediate diagnosis + param patch      │
 *  │  Every N minutes → Claude Code log analysis → deeper param tuning   │
 *  └──────────────────────────────────────────────────────────────────────┘
 */

import { env, defaultScannerConfig } from './config.js';
import { log } from './storage/database.js';
import { runAnalysis } from './self-healing/log-analyzer.js';
import { onPositionClosed } from './self-healing/index.js';

// Mutable config — self-healer patches this live
const config = { ...defaultScannerConfig };

async function main(): Promise<void> {
  log('info', '─── Self-Healing Crypto Trader starting ───', {
    data: {
      paperTrading: env.paperTrading,
      maxPositionUsd: env.maxPositionUsd,
      logAnalysisIntervalMins: env.logAnalysisIntervalMins,
    },
  });

  if (env.paperTrading) {
    log('info', 'PAPER TRADING mode — no real orders will be placed');
  }

  if (!env.anthropicApiKey) {
    log('warn', 'ANTHROPIC_API_KEY not set — Claude log analysis disabled');
  }

  // ── Claude log analysis loop ──────────────────────────────────────────
  if (env.anthropicApiKey) {
    const analysisIntervalMs = env.logAnalysisIntervalMins * 60_000;
    setInterval(() => {
      runAnalysis(config).catch((err: unknown) => {
        log('error', `Log analysis failed: ${String(err)}`);
      });
    }, analysisIntervalMs);
    log('info', `Claude log analysis scheduled every ${env.logAnalysisIntervalMins} minutes`);
  }

  // Export for use by other modules
  (globalThis as Record<string, unknown>)['traderConfig'] = config;
  (globalThis as Record<string, unknown>)['traderOnPositionClosed'] = onPositionClosed;

  log('info', `
──────────────────────────────────────────
  Strategies active:
  ✓ momentum_swing        (v1 — enhanced)
  ✓ momentum_scalp        (v1 — enhanced)
  ✓ listing_pump          (v1 — enhanced)
  ✓ whale_accumulation    (v1 — enhanced)
  ✓ mean_reversion        (NEW — VWAP + RSI)
  ✓ funding_extreme       (NEW — funding rate mean-reversion)
  ✓ liquidation_cascade   (NEW — cascade rider + dip buyer)
  ✓ orderbook_imbalance   (NEW — L2 book depth)
  ✓ narrative_momentum    (NEW — sector rotation)
  ✓ correlation_break     (NEW — BTC correlation divergence)
  ✓ protocol_revenue      (NEW — DeFiLlama fundamental)
  ✓ fear_greed_contrarian (NEW — extreme index plays)

  Self-healing:
  ✓ Immediate: loss diagnosis + parameter patch after each trade
  ✓ Periodic:  Claude Code log analysis every ${env.logAnalysisIntervalMins}m
──────────────────────────────────────────`);

  // Keep process alive
  process.on('SIGINT', () => {
    log('info', 'Shutting down gracefully...');
    process.exit(0);
  });

  // Block forever (real implementation attaches WebSocket and polling loops)
  await new Promise<never>(() => { /* intentionally never resolves */ });
}

main().catch((err: unknown) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
