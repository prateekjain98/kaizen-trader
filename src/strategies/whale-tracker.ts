/**
 * Whale Accumulation/Distribution Strategy
 *
 * Tracks NET whale flow per symbol over a rolling 2h window.
 * Distinguishes accumulation (exchange → cold wallet) from distribution (cold wallet → exchange).
 * Weights alerts by transfer size — only fires on $3M+ moves.
 */

import type { TradeSignal } from '../types.js';
import { randomUUID } from 'crypto';

export interface WhaleTransfer {
  symbol: string;
  amountUsd: number;
  fromType: 'exchange' | 'unknown_wallet' | 'known_fund' | 'miner';
  toType: 'exchange' | 'unknown_wallet' | 'known_fund' | 'miner';
  knownWallet?: string;
  ts: number;
}

interface NetFlowState {
  inflowToExchange: number;    // selling pressure
  outflowFromExchange: number; // accumulation signal
  lastTs: number;
}

const flowWindows = new Map<string, NetFlowState>();
const WINDOW_MS = 7_200_000; // 2 hours
const MIN_ALERT_SIZE_USD = 3_000_000; // $3M+

export function onWhaleTransfer(tx: WhaleTransfer): void {
  if (tx.amountUsd < MIN_ALERT_SIZE_USD) return;

  if (!flowWindows.has(tx.symbol)) {
    flowWindows.set(tx.symbol, { inflowToExchange: 0, outflowFromExchange: 0, lastTs: Date.now() });
  }

  const state = flowWindows.get(tx.symbol)!;

  // Reset if window expired
  if (Date.now() - state.lastTs > WINDOW_MS) {
    state.inflowToExchange = 0;
    state.outflowFromExchange = 0;
  }

  state.lastTs = Date.now();

  // Classify flow
  const movingToExchange = tx.toType === 'exchange';
  const movingFromExchange = tx.fromType === 'exchange';

  if (movingToExchange) {
    state.inflowToExchange += tx.amountUsd; // bearish (selling prep)
  } else if (movingFromExchange || tx.toType === 'unknown_wallet') {
    state.outflowFromExchange += tx.amountUsd; // bullish (accumulation)
  }
}

export function scanWhaleAccumulation(
  symbol: string,
  productId: string,
  currentPrice: number,
): TradeSignal | null {
  const state = flowWindows.get(symbol);
  if (!state) return null;

  const net = state.outflowFromExchange - state.inflowToExchange;
  const totalFlow = state.outflowFromExchange + state.inflowToExchange;

  if (totalFlow < MIN_ALERT_SIZE_USD) return null;

  const netRatio = net / totalFlow; // -1 = pure distribution, +1 = pure accumulation

  // ── Accumulation signal ──────────────────────────────────────────────────
  if (netRatio > 0.4 && state.outflowFromExchange > 5_000_000) {
    const flowScore = Math.min(30, state.outflowFromExchange / 1_000_000);
    const ratioScore = Math.min(20, netRatio * 20);
    const score = Math.min(88, 45 + flowScore + ratioScore);

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'whale_accumulation',
      side: 'long',
      tier: 'swing',
      score,
      confidence: score > 70 ? 'medium' : 'low',
      sources: ['whale_alert'],
      reasoning: `${symbol} $${(state.outflowFromExchange / 1e6).toFixed(0)}M net outflow from exchanges in 2h (accumulation signal)`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 0.93,
      suggestedSizeUsd: 100,
      expiresAt: Date.now() + 21_600_000, // 6h
      createdAt: Date.now(),
    };
  }

  // ── Distribution signal (short) ──────────────────────────────────────────
  if (netRatio < -0.5 && state.inflowToExchange > 10_000_000) {
    const flowScore = Math.min(25, state.inflowToExchange / 1_000_000);
    const ratioScore = Math.min(20, -netRatio * 20);
    const score = Math.min(84, 40 + flowScore + ratioScore);

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'whale_accumulation',
      side: 'short',
      tier: 'swing',
      score,
      confidence: score > 68 ? 'medium' : 'low',
      sources: ['whale_alert'],
      reasoning: `${symbol} $${(state.inflowToExchange / 1e6).toFixed(0)}M whale inflows to exchanges in 2h (distribution signal)`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 1.04,
      suggestedSizeUsd: 80,
      expiresAt: Date.now() + 14_400_000, // 4h
      createdAt: Date.now(),
    };
  }

  return null;
}
