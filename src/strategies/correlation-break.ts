/**
 * Correlation Break Strategy
 *
 * Thesis: Most altcoins are highly correlated with BTC. When an altcoin
 * diverges significantly from BTC's move (either outperforming or
 * underperforming), it often mean-reverts to the correlation baseline.
 *
 * Two plays:
 *  A. Altcoin underperforms BTC → long the relative value catchup
 *  B. Altcoin overperforms BTC → short the mean reversion (or exit)
 *
 * This is NOT momentum — it bets on the correlation holding.
 * Works best in neutral to mild markets, not trending markets.
 */

import type { TradeSignal, ScannerConfig, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

interface PricePoint {
  btcPct: number;   // BTC 1h price change
  altPct: number;   // alt 1h price change
  ts: number;
}

const correlationHistory = new Map<string, PricePoint[]>();

export function updateCorrelationPoint(symbol: string, btcPct: number, altPct: number): void {
  if (!correlationHistory.has(symbol)) correlationHistory.set(symbol, []);
  const hist = correlationHistory.get(symbol)!;
  hist.push({ btcPct, altPct, ts: Date.now() });
  // Keep last 48 hours of hourly data
  const cutoff = Date.now() - 172_800_000;
  correlationHistory.set(symbol, hist.filter(p => p.ts >= cutoff));
}

function computeExpectedAltMove(symbol: string, btcPct: number): number | null {
  const hist = correlationHistory.get(symbol);
  if (!hist || hist.length < 24) return null;

  // Simple linear regression: altPct = beta * btcPct + alpha
  const n = hist.length;
  const sumX = hist.reduce((s, p) => s + p.btcPct, 0);
  const sumY = hist.reduce((s, p) => s + p.altPct, 0);
  const sumXY = hist.reduce((s, p) => s + p.btcPct * p.altPct, 0);
  const sumXX = hist.reduce((s, p) => s + p.btcPct * p.btcPct, 0);

  const denom = n * sumXX - sumX * sumX;
  if (Math.abs(denom) < 1e-10) return null;

  const beta = (n * sumXY - sumX * sumY) / denom;
  const alpha = (sumY - beta * sumX) / n;

  return alpha + beta * btcPct;
}

export function scanCorrelationBreak(
  symbol: string,
  productId: string,
  currentPrice: number,
  btc1hPct: number,     // BTC 1h price change
  alt1hPct: number,     // alt 1h price change
  config: ScannerConfig,
  ctx: MarketContext,
): TradeSignal | null {
  // Don't run in strongly trending markets (correlation breaks persist in trends)
  if (ctx.phase === 'extreme_greed' || ctx.phase === 'extreme_fear') return null;

  updateCorrelationPoint(symbol, btc1hPct, alt1hPct);

  const expected = computeExpectedAltMove(symbol, btc1hPct);
  if (!expected) return null;

  const divergence = alt1hPct - expected;

  // ── Underperformance: alt lagging expected → long catchup ───────────────
  if (divergence < -0.03 && Math.abs(divergence) > 0.02) {
    const divScore = Math.min(30, Math.abs(divergence) * 500);
    const score = Math.min(80, 42 + divScore);

    if (score >= config.minQualScoreSwing - 5) {
      return {
        id: randomUUID(),
        symbol,
        productId,
        strategy: 'correlation_break',
        side: 'long',
        tier: 'swing',
        score,
        confidence: 'low',
        sources: ['correlation'],
        reasoning: `${symbol} underperforming BTC correlation by ${(divergence * 100).toFixed(1)}% (expected ${(expected * 100).toFixed(1)}%, got ${(alt1hPct * 100).toFixed(1)}%) — catchup long`,
        entryPrice: currentPrice,
        stopPrice: currentPrice * 0.97,
        suggestedSizeUsd: 70,
        expiresAt: Date.now() + 7_200_000, // 2h
        createdAt: Date.now(),
      };
    }
  }

  // ── Overperformance: alt leading expected → reversion short ─────────────
  if (divergence > 0.04 && ctx.phase !== 'bull') {
    const divScore = Math.min(28, divergence * 450);
    const score = Math.min(78, 38 + divScore);

    if (score >= config.minQualScoreSwing - 5) {
      return {
        id: randomUUID(),
        symbol,
        productId,
        strategy: 'correlation_break',
        side: 'short',
        tier: 'swing',
        score,
        confidence: 'low',
        sources: ['correlation'],
        reasoning: `${symbol} overperforming BTC correlation by ${(divergence * 100).toFixed(1)}% — reversion short`,
        entryPrice: currentPrice,
        stopPrice: currentPrice * 1.03,
        suggestedSizeUsd: 60,
        expiresAt: Date.now() + 7_200_000,
        createdAt: Date.now(),
      };
    }
  }

  return null;
}
