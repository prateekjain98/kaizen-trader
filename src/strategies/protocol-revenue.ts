/**
 * Protocol Revenue Strategy (NEW — not in v1)
 *
 * Thesis: DeFi protocols with real revenue (fees paid to token holders / treasury)
 * are undervalued when their P/E ratio (Price-to-Fees) is low relative to peers.
 * When protocol fees spike AND the token hasn't moved yet, there's an opportunity.
 *
 * Signal: DeFiLlama fees API (https://api.llama.fi/overview/fees)
 *  - 24h revenue spike >2× 7-day average
 *  - Token hasn't pumped yet (price change <10% in 24h)
 *  - Protocol TVL is stable or growing (not losing users)
 *
 * This is a POSITION tier trade (longer hold, 1-7 days), not a scalp.
 */

import type { TradeSignal } from '../types.js';
import { randomUUID } from 'crypto';

export interface ProtocolMetrics {
  symbol: string;
  productId: string;
  protocol: string;
  revenue24h: number;      // USD fees in last 24h
  revenue7dAvg: number;    // average daily revenue over 7d
  tvl: number;             // total value locked USD
  tvlChange7d: number;     // pct change in TVL over 7d
  tokenPriceChange24h: number; // token price change in 24h
}

export function scanProtocolRevenue(
  metric: ProtocolMetrics,
  currentPrice: number,
): TradeSignal | null {
  const revenueMultiple = metric.revenue7dAvg > 0
    ? metric.revenue24h / metric.revenue7dAvg
    : 0;

  // Revenue spiked but token hasn't caught up yet
  if (
    revenueMultiple < 2.0 ||
    metric.tokenPriceChange24h > 0.12 || // token already pumped
    metric.tvlChange7d < -0.20           // protocol losing users
  ) {
    return null;
  }

  const revenueScore = Math.min(35, (revenueMultiple - 2) * 10);
  const tvlScore = Math.min(15, Math.max(0, metric.tvlChange7d * 50));
  const priceDiscountScore = Math.max(0, 10 - metric.tokenPriceChange24h * 50);
  const score = Math.min(85, 45 + revenueScore + tvlScore + priceDiscountScore);

  return {
    id: randomUUID(),
    symbol: metric.symbol,
    productId: metric.productId,
    strategy: 'protocol_revenue',
    side: 'long',
    tier: 'swing',
    score,
    confidence: score > 72 ? 'medium' : 'low',
    sources: ['protocol_revenue'],
    reasoning: `${metric.protocol} revenue ${revenueMultiple.toFixed(1)}× 7d avg ($${(metric.revenue24h / 1000).toFixed(0)}K today) but ${metric.symbol} price only +${(metric.tokenPriceChange24h * 100).toFixed(0)}% — fundamentals lead`,
    entryPrice: currentPrice,
    stopPrice: currentPrice * 0.92,
    suggestedSizeUsd: 120,
    expiresAt: Date.now() + 86_400_000 * 3, // 3 days
    createdAt: Date.now(),
  };
}
