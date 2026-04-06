/**
 * Narrative Momentum Strategy
 *
 * Thesis: Crypto markets move in narratives (AI tokens, DeFi, RWA, L2s, etc.).
 * When a narrative catches fire — measured by correlated social velocity across
 * multiple tokens in the same sector — the whole sector tends to run.
 *
 * This strategy:
 *  1. Groups tokens into narratives/sectors
 *  2. Tracks social mention velocity for each sector
 *  3. When a sector's velocity spikes >narrativeVelocityThreshold×, find the
 *     sector laggard (lowest price appreciation vs sector average) and enter long
 *
 * Why laggard: sector leaders have already pumped; the laggard hasn't "caught up" yet.
 * This mimics how retail rotates within a hot sector.
 */

import type { TradeSignal, ScannerConfig } from '../types.js';
import { randomUUID } from 'crypto';

export type NarrativeId =
  | 'ai_tokens'
  | 'defi_bluechip'
  | 'layer2'
  | 'rwa'
  | 'gaming_metaverse'
  | 'meme'
  | 'depin'
  | 'liquid_staking'
  | 'btc_ecosystem'
  | 'privacy';

export const NARRATIVE_MEMBERS: Record<NarrativeId, string[]> = {
  ai_tokens:        ['FET', 'AGIX', 'OCEAN', 'RENDER', 'TAO', 'WLD'],
  defi_bluechip:    ['UNI', 'AAVE', 'CRV', 'MKR', 'SNX', 'COMP'],
  layer2:           ['ARB', 'OP', 'MATIC', 'IMX', 'STRK', 'MANTA'],
  rwa:              ['ONDO', 'POLYX', 'CFG', 'TRU', 'MPL'],
  gaming_metaverse: ['AXS', 'SAND', 'MANA', 'GALA', 'ILV', 'YGG'],
  meme:             ['DOGE', 'SHIB', 'PEPE', 'WIF', 'BONK', 'FLOKI'],
  depin:            ['HNT', 'MOBILE', 'IOT', 'RNDR', 'FIL', 'AR'],
  liquid_staking:   ['LDO', 'RPL', 'SFRXETH', 'ANKR', 'PENDLE'],
  btc_ecosystem:    ['STX', 'ORDI', 'SATS', 'RATS', 'MUBI'],
  privacy:          ['XMR', 'ZEC', 'SCRT', 'DUSK', 'CTXC'],
};

interface NarrativeState {
  id: NarrativeId;
  socialVelocity: number;       // current velocity (relative to 30d baseline)
  baselineVelocity: number;
  memberPriceChanges: Map<string, number>; // symbol → 24h price change pct
  lastUpdated: number;
}

const narrativeStates = new Map<NarrativeId, NarrativeState>();

export function updateNarrativeSocialData(
  narrativeId: NarrativeId,
  currentMentions: number,
  baselineMentions: number,
): void {
  const state = narrativeStates.get(narrativeId) ?? {
    id: narrativeId,
    socialVelocity: 1,
    baselineVelocity: baselineMentions,
    memberPriceChanges: new Map(),
    lastUpdated: Date.now(),
  };
  state.socialVelocity = baselineMentions > 0 ? currentMentions / baselineMentions : 1;
  state.baselineVelocity = baselineMentions;
  state.lastUpdated = Date.now();
  narrativeStates.set(narrativeId, state);
}

export function updateNarrativeMemberPrice(narrativeId: NarrativeId, symbol: string, priceChangePct: number): void {
  const state = narrativeStates.get(narrativeId);
  if (state) {
    state.memberPriceChanges.set(symbol, priceChangePct);
  }
}

function findLaggard(state: NarrativeState, productIdMap: Map<string, string>): {
  symbol: string; productId: string; priceChangePct: number;
} | null {
  let laggard: { symbol: string; productId: string; priceChangePct: number } | null = null;

  for (const [sym, pct] of state.memberPriceChanges.entries()) {
    const pid = productIdMap.get(sym);
    if (!pid) continue;
    if (!laggard || pct < laggard.priceChangePct) {
      laggard = { symbol: sym, productId: pid, priceChangePct: pct };
    }
  }

  return laggard;
}

export function scanNarrativeMomentum(
  productIdMap: Map<string, string>, // symbol → productId (only available tokens)
  config: ScannerConfig,
  currentPrices: Map<string, number>,
): TradeSignal | null {
  let bestSignal: TradeSignal | null = null;
  let bestScore = 0;

  for (const [narrativeId, state] of narrativeStates.entries()) {
    if (state.socialVelocity < config.narrativeVelocityThreshold) continue;
    if (Date.now() - state.lastUpdated > 1_800_000) continue; // stale data

    const laggard = findLaggard(state, productIdMap);
    if (!laggard) continue;

    const currentPrice = currentPrices.get(laggard.symbol);
    if (!currentPrice) continue;

    // Score: higher velocity = higher score; laggard being more negative = more upside
    const velocityScore = Math.min(35, (state.socialVelocity - config.narrativeVelocityThreshold) * 10);
    const laggardScore = Math.min(20, Math.max(0, -laggard.priceChangePct * 100));
    const score = Math.min(88, 48 + velocityScore + laggardScore);

    if (score > bestScore) {
      bestScore = score;
      bestSignal = {
        id: randomUUID(),
        symbol: laggard.symbol,
        productId: laggard.productId,
        strategy: 'narrative_momentum',
        side: 'long',
        tier: 'swing',
        score,
        confidence: score > 72 ? 'medium' : 'low',
        sources: ['social'],
        reasoning: `${narrativeId.replace(/_/g, ' ')} narrative velocity ${state.socialVelocity.toFixed(1)}× baseline; ${laggard.symbol} lagging sector by ${(laggard.priceChangePct * 100).toFixed(1)}% — rotation play`,
        entryPrice: currentPrice,
        stopPrice: currentPrice * 0.94,
        suggestedSizeUsd: 80,
        expiresAt: Date.now() + 7_200_000, // 2h
        createdAt: Date.now(),
      };
    }
  }

  return bestSignal;
}
