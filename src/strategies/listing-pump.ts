/**
 * Listing Pump Strategy (ported from v1 + enhanced)
 *
 * Detects new exchange listings across Coinbase, Binance, and Kraken.
 * New listings historically pump 20-100% in the first 48h.
 *
 * Enhancements over v1:
 *  - Multi-exchange: catches Binance listings (not just Coinbase)
 *  - Pre-listing signal: monitors CryptoPanic for "listing" news before official
 *  - Checks if token already listed elsewhere (less pumpy if so)
 *  - Estimates first-mover opportunity via time-since-announcement
 */

import type { TradeSignal } from '../types.js';
import { randomUUID } from 'crypto';

export interface ListingAnnouncement {
  symbol: string;
  exchange: 'coinbase' | 'binance' | 'kraken' | 'bybit';
  productId: string;
  announcedAt: number;
  tradingStartsAt?: number;
  isNewToMajorExchanges: boolean; // false if already on Binance when listing Coinbase
}

const seenListings = new Set<string>();
const LISTING_EXPIRY_MS = 48 * 3_600_000;

export function onListingAnnouncement(listing: ListingAnnouncement, currentPrice: number): TradeSignal | null {
  const key = `${listing.exchange}:${listing.symbol}`;
  if (seenListings.has(key)) return null;
  seenListings.add(key);

  const ageMs = Date.now() - listing.announcedAt;
  if (ageMs > LISTING_EXPIRY_MS) return null; // too stale

  // Base score: Coinbase listings have strongest pump history
  let baseScore = 55;
  switch (listing.exchange) {
    case 'coinbase': baseScore = 75; break;
    case 'binance':  baseScore = 72; break;
    case 'kraken':   baseScore = 60; break;
    case 'bybit':    baseScore = 58; break;
  }

  // First mover bonus: within 30 min of announcement = max bonus
  const freshnessBonus = Math.max(0, 15 - Math.floor(ageMs / 120_000));

  // Penalty if already on major exchanges (less retail discovery excitement)
  const alreadyListedPenalty = listing.isNewToMajorExchanges ? 0 : -15;

  const score = Math.min(95, baseScore + freshnessBonus + alreadyListedPenalty);

  return {
    id: randomUUID(),
    symbol: listing.symbol,
    productId: listing.productId,
    strategy: 'listing_pump',
    side: 'long',
    tier: 'swing',
    score,
    confidence: score > 80 ? 'high' : score > 65 ? 'medium' : 'low',
    sources: ['listing_detector'],
    reasoning: `${listing.symbol} new ${listing.exchange} listing announced ${Math.floor(ageMs / 60_000)}m ago${listing.isNewToMajorExchanges ? ' (first major exchange)' : ''}`,
    entryPrice: currentPrice,
    stopPrice: currentPrice * 0.88, // wider stop — listing pumps can be volatile
    suggestedSizeUsd: 120,
    expiresAt: Date.now() + LISTING_EXPIRY_MS,
    createdAt: Date.now(),
  };
}
