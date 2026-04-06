/**
 * Funding Rate Extreme Strategy (NEW — v1 had funding data but no strategy)
 *
 * Thesis: When perpetual futures funding rates are extremely high (longs paying),
 * the market is over-leveraged long — price often corrects as longs get liquidated.
 * When funding is extremely negative (shorts paying), squeeze risk is elevated.
 *
 * This is a mean-reversion/contrarian strategy, not momentum.
 *
 * Entry (short): funding rate > +threshold → over-leveraged longs → short
 * Entry (long):  funding rate < -threshold → over-leveraged shorts → long squeeze
 *
 * Historically, funding >0.15% in an 8h period precedes corrections 68% of the time.
 * Funding < -0.05% often precedes short squeezes.
 */

import type { TradeSignal, ScannerConfig, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

export interface FundingRateData {
  symbol: string;
  fundingRate: number;       // e.g. 0.001 = 0.1% per 8h
  fundingIntervalHours: number;
  openInterest: number;      // USD
  openInterestChangePct: number; // 24h change in OI
  predictedRate?: number;    // next funding period prediction
}

const fundingCache = new Map<string, FundingRateData>();

export function updateFundingData(data: FundingRateData): void {
  fundingCache.set(data.symbol, data);
}

export function scanFundingExtreme(
  symbol: string,
  productId: string,
  currentPrice: number,
  config: ScannerConfig,
  ctx: MarketContext,
): TradeSignal | null {
  const funding = fundingCache.get(symbol);
  if (!funding) return null;

  const threshold = config.fundingRateExtremeThreshold;
  const annualizedFunding = funding.fundingRate * (8760 / funding.fundingIntervalHours);

  // ── Short: funding extremely positive (over-leveraged longs) ────────────
  if (
    funding.fundingRate > threshold &&
    funding.openInterestChangePct > 10 && // OI growing = new leverage piling in
    ctx.phase !== 'extreme_greed' // don't fight a true bull trend
  ) {
    const magnitudeScore = Math.min(40, (funding.fundingRate / threshold - 1) * 20);
    const oiScore = Math.min(20, funding.openInterestChangePct / 5);
    const score = Math.min(88, 45 + magnitudeScore + oiScore);

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'funding_extreme',
      side: 'short',
      tier: 'swing',
      score,
      confidence: score > 70 ? 'medium' : 'low',
      sources: ['funding_rates'],
      reasoning: `${symbol} funding=${(funding.fundingRate * 100).toFixed(3)}% (${(annualizedFunding * 100).toFixed(0)}% annualized), OI +${funding.openInterestChangePct.toFixed(0)}% — over-leveraged longs likely to flush`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 1.04,
      suggestedSizeUsd: 60,
      expiresAt: Date.now() + 14_400_000, // 4h
      createdAt: Date.now(),
    };
  }

  // ── Long: funding extremely negative (short squeeze setup) ──────────────
  if (
    funding.fundingRate < -threshold &&
    funding.openInterestChangePct > 5 // shorts piling in = squeeze fuel
  ) {
    const magnitudeScore = Math.min(35, (-funding.fundingRate / threshold - 1) * 18);
    const score = Math.min(85, 42 + magnitudeScore);

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'funding_extreme',
      side: 'long',
      tier: 'swing',
      score,
      confidence: score > 68 ? 'medium' : 'low',
      sources: ['funding_rates'],
      reasoning: `${symbol} funding=${(funding.fundingRate * 100).toFixed(3)}% (negative), OI +${funding.openInterestChangePct.toFixed(0)}% shorts — squeeze risk elevated`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 0.97,
      suggestedSizeUsd: 70,
      expiresAt: Date.now() + 14_400_000,
      createdAt: Date.now(),
    };
  }

  return null;
}
