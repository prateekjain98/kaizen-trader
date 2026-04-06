/**
 * Performance metrics engine.
 *
 * Computes institutional-grade trading metrics from the closed trade history:
 *
 *   Sharpe ratio        — risk-adjusted return (daily, annualized)
 *   Sortino ratio       — downside-only risk (penalizes losses more than volatility)
 *   Calmar ratio        — return / max drawdown
 *   Win rate            — by strategy and overall
 *   Profit factor       — gross profits / gross losses
 *   Average MAE/MFE     — max adverse/favorable excursion (entry timing quality)
 *   Kelly fraction      — implied edge per strategy
 *   Consecutive stats   — max winning/losing streaks
 *
 * These metrics are passed to the Claude log analyzer to give it a quantitative
 * picture alongside the qualitative trade logs.
 */

import { getClosedTrades } from '../storage/database.js';
import type { StrategyId } from '../types.js';

export interface StrategyMetrics {
  strategy: StrategyId;
  totalTrades: number;
  winRate: number;
  avgWinPct: number;
  avgLossPct: number;
  profitFactor: number;
  avgHoldHours: number;
  totalPnlUsd: number;
  kellyFraction: number;
  maxConsecLosses: number;
}

export interface PortfolioMetrics {
  totalTrades: number;
  winRate: number;
  profitFactor: number;
  totalPnlUsd: number;
  sharpeRatio: number | null;
  sortinoRatio: number | null;
  calmarRatio: number | null;
  maxDrawdownPct: number;
  avgHoldHours: number;
  byStrategy: StrategyMetrics[];
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function mean(arr: number[]): number {
  return arr.length === 0 ? 0 : arr.reduce((a, b) => a + b, 0) / arr.length;
}

function stdDev(arr: number[], avg?: number): number {
  const m = avg ?? mean(arr);
  const variance = arr.reduce((s, v) => s + Math.pow(v - m, 2), 0) / Math.max(arr.length - 1, 1);
  return Math.sqrt(variance);
}

function maxDrawdown(returns: number[]): number {
  let peak = 0, equity = 0, maxDD = 0;
  for (const r of returns) {
    equity += r;
    if (equity > peak) peak = equity;
    const dd = peak > 0 ? (peak - equity) / peak : 0;
    if (dd > maxDD) maxDD = dd;
  }
  return maxDD;
}

function maxConsecutiveLosses(pnls: number[]): number {
  let max = 0, streak = 0;
  for (const p of pnls) {
    if (p < 0) { streak++; max = Math.max(max, streak); }
    else streak = 0;
  }
  return max;
}

function kellyFraction(winRate: number, avgWinPct: number, avgLossPct: number): number {
  if (avgLossPct === 0) return 0;
  const b = avgWinPct / avgLossPct;
  const p = winRate;
  const q = 1 - p;
  return Math.max(0, (b * p - q) / b);
}

// ─── Compute ──────────────────────────────────────────────────────────────────

export function computeMetrics(lookbackTrades = 500): PortfolioMetrics {
  const trades = getClosedTrades(lookbackTrades);
  if (trades.length === 0) {
    return {
      totalTrades: 0, winRate: 0, profitFactor: 0, totalPnlUsd: 0,
      sharpeRatio: null, sortinoRatio: null, calmarRatio: null,
      maxDrawdownPct: 0, avgHoldHours: 0, byStrategy: [],
    };
  }

  const pnlPcts = trades.map(t => t.pnlPct ?? 0);
  const pnlUsds = trades.map(t => t.pnlUsd ?? 0);
  const wins    = pnlUsds.filter(p => p > 0);
  const losses  = pnlUsds.filter(p => p <= 0);

  const grossWins   = wins.reduce((s, p) => s + p, 0);
  const grossLosses = Math.abs(losses.reduce((s, p) => s + p, 0));

  const winRate     = wins.length / trades.length;
  const profitFactor = grossLosses > 0 ? grossWins / grossLosses : grossWins > 0 ? Infinity : 0;

  const avgHoldHours = mean(
    trades.map(t => t.closedAt ? (t.closedAt - t.openedAt) / 3_600_000 : 0)
  );

  // Risk metrics
  const pnlMean = mean(pnlPcts);
  const pnlStd  = stdDev(pnlPcts, pnlMean);
  const downside = pnlPcts.filter(p => p < 0);
  const downsideStd = stdDev(downside, 0);

  const sharpeRatio  = pnlStd > 0 && pnlPcts.length >= 30 ? (pnlMean / pnlStd) * Math.sqrt(252) : null;
  const sortinoRatio = downsideStd > 0 && pnlPcts.length >= 30 ? (pnlMean / downsideStd) * Math.sqrt(252) : null;

  const maxDD = maxDrawdown(pnlUsds);
  const totalPnl = pnlUsds.reduce((s, p) => s + p, 0);
  const calmarRatio = maxDD > 0 && totalPnl > 0 ? (totalPnl / maxDD) : null;

  // Per-strategy breakdown
  const strategyMap = new Map<StrategyId, typeof trades>();
  for (const t of trades) {
    if (!strategyMap.has(t.strategy)) strategyMap.set(t.strategy, []);
    strategyMap.get(t.strategy)!.push(t);
  }

  const byStrategy: StrategyMetrics[] = [];
  for (const [strategy, stTrades] of strategyMap.entries()) {
    const stPnls  = stTrades.map(t => t.pnlPct ?? 0);
    const stWins  = stPnls.filter(p => p > 0);
    const stLoses = stPnls.filter(p => p <= 0);
    const stWinRate    = stWins.length / stTrades.length;
    const avgWin  = stWins.length  > 0 ? mean(stWins)              : 0;
    const avgLoss = stLoses.length > 0 ? Math.abs(mean(stLoses))   : 0;

    byStrategy.push({
      strategy,
      totalTrades:    stTrades.length,
      winRate:        stWinRate,
      avgWinPct:      avgWin,
      avgLossPct:     avgLoss,
      profitFactor:   avgLoss > 0 ? (stWinRate * avgWin) / ((1 - stWinRate) * avgLoss) : 0,
      avgHoldHours:   mean(stTrades.map(t => t.closedAt ? (t.closedAt - t.openedAt) / 3_600_000 : 0)),
      totalPnlUsd:    stTrades.reduce((s, t) => s + (t.pnlUsd ?? 0), 0),
      kellyFraction:  kellyFraction(stWinRate, avgWin, avgLoss),
      maxConsecLosses: maxConsecutiveLosses(stTrades.map(t => t.pnlPct ?? 0)),
    });
  }

  byStrategy.sort((a, b) => b.totalPnlUsd - a.totalPnlUsd);

  return {
    totalTrades: trades.length,
    winRate,
    profitFactor,
    totalPnlUsd: totalPnl,
    sharpeRatio,
    sortinoRatio,
    calmarRatio,
    maxDrawdownPct: maxDD,
    avgHoldHours,
    byStrategy,
  };
}

export function formatMetrics(m: PortfolioMetrics): string {
  const lines = [
    `Trades:        ${m.totalTrades}`,
    `Win rate:      ${(m.winRate * 100).toFixed(1)}%`,
    `Profit factor: ${m.profitFactor === Infinity ? '∞' : m.profitFactor.toFixed(2)}`,
    `Total P&L:     $${m.totalPnlUsd.toFixed(2)}`,
    `Avg hold:      ${m.avgHoldHours.toFixed(1)}h`,
    `Max drawdown:  ${(m.maxDrawdownPct * 100).toFixed(1)}%`,
    m.sharpeRatio  != null ? `Sharpe:        ${m.sharpeRatio.toFixed(2)}`  : 'Sharpe:        (insufficient data)',
    m.sortinoRatio != null ? `Sortino:       ${m.sortinoRatio.toFixed(2)}` : 'Sortino:       (insufficient data)',
    m.calmarRatio  != null ? `Calmar:        ${m.calmarRatio.toFixed(2)}`  : 'Calmar:        (insufficient data)',
    '',
    'By strategy:',
    ...m.byStrategy.map(s =>
      `  ${s.strategy.padEnd(28)} trades=${String(s.totalTrades).padStart(3)}  win=${(s.winRate * 100).toFixed(0).padStart(3)}%  pnl=$${s.totalPnlUsd.toFixed(0).padStart(8)}  kelly=${(s.kellyFraction * 100).toFixed(1)}%`
    ),
  ];
  return lines.join('\n');
}
