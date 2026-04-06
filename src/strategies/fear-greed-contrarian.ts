/**
 * Fear & Greed Contrarian Strategy (NEW — not in v1)
 *
 * v1 used Fear & Greed as a qual modifier (+/- 10 points). This elevates it
 * to a standalone contrarian strategy: buy extreme fear, sell extreme greed.
 *
 * Research: Extreme Fear (<15) historically precedes positive 30-day returns
 * in BTC 76% of the time (2019-2024 data). Extreme Greed (>85) precedes
 * negative 30-day returns 61% of the time.
 *
 * This strategy only fires for BTC/ETH (deepest liquidity, most index-correlated).
 * It's a POSITION tier trade (multi-day to multi-week hold).
 */

import type { TradeSignal, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

const ELIGIBLE_SYMBOLS = new Set(['BTC', 'ETH']);

let prevFearGreedIndex = 50;
let fearGreedActivated = false; // prevent re-entry until index normalizes

export function scanFearGreedContrarian(
  symbol: string,
  productId: string,
  currentPrice: number,
  ctx: MarketContext,
): TradeSignal | null {
  if (!ELIGIBLE_SYMBOLS.has(symbol)) return null;

  const fgi = ctx.fearGreedIndex;
  const delta = fgi - prevFearGreedIndex;
  prevFearGreedIndex = fgi;

  // Wait for normalization after a signal fires
  if (fearGreedActivated && fgi > 20 && fgi < 80) {
    fearGreedActivated = false;
  }

  // ── Extreme Fear: contrarian long ─────────────────────────────────────
  if (fgi <= 15 && !fearGreedActivated) {
    const extremeness = Math.min(30, (15 - fgi) * 2);
    // Bonus if index just hit new low (delta negative = getting worse)
    const momentumBonus = delta < -5 ? 10 : 0;
    const score = Math.min(82, 52 + extremeness + momentumBonus);

    fearGreedActivated = true;
    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'fear_greed_contrarian',
      side: 'long',
      tier: 'swing', // position-like: long hold
      score,
      confidence: score > 72 ? 'medium' : 'low',
      sources: ['fear_greed'],
      reasoning: `Fear & Greed Index at ${fgi} (extreme fear) — contrarian long; historically 76% of such extremes precede positive 30d returns`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 0.88, // wider stop for position trade
      suggestedSizeUsd: 150,
      expiresAt: Date.now() + 7 * 86_400_000, // 7 days
      createdAt: Date.now(),
    };
  }

  // ── Extreme Greed: contrarian short (only if not in bull market)  ──────
  if (fgi >= 85 && !fearGreedActivated && ctx.phase !== 'bull') {
    const extremeness = Math.min(25, (fgi - 85) * 1.5);
    const score = Math.min(75, 45 + extremeness);

    fearGreedActivated = true;
    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'fear_greed_contrarian',
      side: 'short',
      tier: 'swing',
      score,
      confidence: 'low',
      sources: ['fear_greed'],
      reasoning: `Fear & Greed Index at ${fgi} (extreme greed) — contrarian short; historically 61% of such extremes precede corrections`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 1.07,
      suggestedSizeUsd: 80,
      expiresAt: Date.now() + 5 * 86_400_000, // 5 days
      createdAt: Date.now(),
    };
  }

  return null;
}
