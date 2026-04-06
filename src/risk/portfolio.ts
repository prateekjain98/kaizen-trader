/**
 * Portfolio-level risk manager.
 *
 * Responsibilities:
 *  1. Track daily P&L (resets at UTC midnight)
 *  2. Circuit breaker: halt new entries when drawdown > MAX_DAILY_LOSS_USD
 *  3. Enforce MAX_OPEN_POSITIONS concurrency cap
 *  4. Track total portfolio value (USDC balance + open position mark-to-market)
 *  5. Compute running Sharpe ratio for the self-healing log analyzer
 */

import { env } from '../config.js';
import { log } from '../storage/database.js';
import type { Position } from '../types.js';

// ─── State ────────────────────────────────────────────────────────────────────

interface DailyStats {
  date: string;   // YYYY-MM-DD UTC
  realizedPnl: number;
  tradeCount: number;
}

let dailyStats: DailyStats = { date: todayUtc(), realizedPnl: 0, tradeCount: 0 };
let circuitBreakerOpen = false;

const openPositions = new Map<string, Position>(); // positionId → Position
const dailyReturns: number[] = [];                 // for Sharpe computation

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

function maybeResetDay(): void {
  const today = todayUtc();
  if (dailyStats.date !== today) {
    if (dailyStats.realizedPnl !== 0) {
      dailyReturns.push(dailyStats.realizedPnl);
      if (dailyReturns.length > 365) dailyReturns.shift();
    }
    dailyStats = { date: today, realizedPnl: 0, tradeCount: 0 };
    circuitBreakerOpen = false;
    log('info', `Daily stats reset for ${today}`);
  }
}

// ─── Public API ────────────────────────────────────────────────────────────────

export function canOpenPosition(): boolean {
  maybeResetDay();

  if (circuitBreakerOpen) {
    log('warn', `Circuit breaker OPEN — daily loss $${(-dailyStats.realizedPnl).toFixed(2)} exceeded $${env.maxDailyLossUsd}`);
    return false;
  }

  if (openPositions.size >= env.maxOpenPositions) {
    log('info', `Position cap reached (${openPositions.size}/${env.maxOpenPositions})`);
    return false;
  }

  return true;
}

export function registerOpen(position: Position): void {
  openPositions.set(position.id, position);
}

export function registerClose(position: Position, pnlUsd: number): void {
  openPositions.delete(position.id);
  maybeResetDay();

  dailyStats.realizedPnl += pnlUsd;
  dailyStats.tradeCount++;

  if (dailyStats.realizedPnl < -env.maxDailyLossUsd) {
    circuitBreakerOpen = true;
    log('warn', `CIRCUIT BREAKER TRIGGERED — daily loss $${(-dailyStats.realizedPnl).toFixed(2)} > $${env.maxDailyLossUsd} — halting new trades until UTC midnight`);
  }
}

export function updatePositionPrice(positionId: string, currentPrice: number): void {
  const pos = openPositions.get(positionId);
  if (pos) pos.currentPrice = currentPrice;
}

export function getOpenPositions(): Position[] {
  return Array.from(openPositions.values());
}

export function getDailyStats(): DailyStats {
  maybeResetDay();
  return { ...dailyStats };
}

export function isCircuitBreakerOpen(): boolean {
  maybeResetDay();
  return circuitBreakerOpen;
}

// ─── Sharpe ratio ─────────────────────────────────────────────────────────────

/**
 * Annualized Sharpe ratio from daily realized P&L history.
 * Returns null if insufficient data (<30 days).
 */
export function computeSharpe(riskFreeRateAnnual = 0.05): number | null {
  if (dailyReturns.length < 30) return null;

  const n = dailyReturns.length;
  const mean = dailyReturns.reduce((a, b) => a + b, 0) / n;
  const variance = dailyReturns.reduce((s, r) => s + Math.pow(r - mean, 2), 0) / (n - 1);
  const stdDev = Math.sqrt(variance);

  if (stdDev === 0) return null;

  const dailyRiskFree = riskFreeRateAnnual / 365;
  const dailySharpe = (mean - dailyRiskFree) / stdDev;
  return dailySharpe * Math.sqrt(365); // annualized
}

/**
 * Maximum drawdown from equity curve.
 */
export function computeMaxDrawdown(): number {
  if (dailyReturns.length === 0) return 0;
  let peak = 0, equity = 0, maxDD = 0;
  for (const r of dailyReturns) {
    equity += r;
    if (equity > peak) peak = equity;
    const dd = peak > 0 ? (peak - equity) / peak : 0;
    if (dd > maxDD) maxDD = dd;
  }
  return maxDD;
}
