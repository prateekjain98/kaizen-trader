/**
 * Position sizing — Kelly Criterion + fixed fractional hybrid.
 *
 * The Kelly Criterion gives the theoretically optimal fraction of capital
 * to risk on a bet given edge (win rate) and odds (win/loss ratio):
 *
 *   f* = (bp - q) / b
 *
 * Where:
 *   b = win/loss ratio (avg win pct / avg loss pct)
 *   p = probability of winning (historical win rate for this strategy)
 *   q = 1 - p
 *
 * We apply "fractional Kelly" at 25% to avoid the well-known overbetting
 * problem, and cap the resulting size at MAX_POSITION_USD regardless.
 *
 * When win rate history is insufficient (<10 trades), we fall back to
 * a conservative 1% fixed-fractional sizing.
 *
 * Position sizing formula:
 *   rawKelly   = (b*p - q) / b          // Kelly fraction of portfolio
 *   kellySized = rawKelly * 0.25         // fractional Kelly
 *   usdSize    = kellySized * portfolioUsd
 *   finalSize  = clamp(usdSize, MIN_USD, MAX_POSITION_USD)
 */

import { env } from '../config.js';
import { getClosedTrades } from '../storage/database.js';
import type { StrategyId } from '../types.js';

const MIN_SIZE_USD = 10;
const MIN_HISTORY  = 10; // trades before Kelly activates
const KELLY_FRACTION = 0.25; // quarter-Kelly

interface StrategyStats {
  winRate: number;
  avgWinPct: number;
  avgLossPct: number;
  sampleSize: number;
}

const statsCache = new Map<StrategyId, { stats: StrategyStats; computedAt: number }>();
const STATS_TTL_MS = 600_000; // recompute every 10 min

function computeStrategyStats(strategy: StrategyId): StrategyStats {
  const cached = statsCache.get(strategy);
  if (cached && Date.now() - cached.computedAt < STATS_TTL_MS) return cached.stats;

  const trades = getClosedTrades(200).filter(t => t.strategy === strategy && t.pnlPct != null);

  if (trades.length < MIN_HISTORY) {
    const stats: StrategyStats = { winRate: 0.5, avgWinPct: 0.04, avgLossPct: 0.03, sampleSize: trades.length };
    statsCache.set(strategy, { stats, computedAt: Date.now() });
    return stats;
  }

  const wins  = trades.filter(t => (t.pnlPct ?? 0) > 0);
  const losses = trades.filter(t => (t.pnlPct ?? 0) <= 0);

  const winRate    = wins.length / trades.length;
  const avgWinPct  = wins.length > 0  ? wins.reduce((s, t) => s + (t.pnlPct ?? 0), 0) / wins.length   : 0.04;
  const avgLossPct = losses.length > 0 ? Math.abs(losses.reduce((s, t) => s + (t.pnlPct ?? 0), 0) / losses.length) : 0.03;

  const stats: StrategyStats = { winRate, avgWinPct, avgLossPct, sampleSize: trades.length };
  statsCache.set(strategy, { stats, computedAt: Date.now() });
  return stats;
}

export function kellySize(
  strategy: StrategyId,
  portfolioUsd: number,
  qualScore: number,
): number {
  const stats = computeStrategyStats(strategy);

  let fraction: number;

  if (stats.sampleSize < MIN_HISTORY) {
    // Insufficient history — use fixed 1% fractional
    fraction = 0.01;
  } else {
    const b = stats.avgWinPct / stats.avgLossPct; // win/loss ratio
    const p = stats.winRate;
    const q = 1 - p;

    const rawKelly = (b * p - q) / b;

    // Negative Kelly = no edge — skip trade
    if (rawKelly <= 0) return 0;

    fraction = rawKelly * KELLY_FRACTION;
  }

  // Scale by qual score: a 90-score trade gets 1.5× sizing vs a 60-score trade
  const qualMultiplier = 0.5 + (qualScore / 100);
  const rawUsd = fraction * portfolioUsd * qualMultiplier;

  return Math.max(MIN_SIZE_USD, Math.min(env.maxPositionUsd, rawUsd));
}

export function logKellyRationale(strategy: StrategyId): string {
  const stats = computeStrategyStats(strategy);
  if (stats.sampleSize < MIN_HISTORY) {
    return `${strategy}: insufficient history (${stats.sampleSize}/${MIN_HISTORY} trades) — using 1% fixed-fractional`;
  }
  const b = stats.avgWinPct / stats.avgLossPct;
  const p = stats.winRate;
  const q = 1 - p;
  const rawKelly = (b * p - q) / b;
  return `${strategy}: win_rate=${(p * 100).toFixed(0)}% avg_win=${(stats.avgWinPct * 100).toFixed(1)}% avg_loss=${(stats.avgLossPct * 100).toFixed(1)}% kelly=${(rawKelly * 100).toFixed(1)}% → quarter_kelly=${(rawKelly * KELLY_FRACTION * 100).toFixed(2)}%`;
}
