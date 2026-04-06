/**
 * Order Book Imbalance Strategy (NEW — not in v1)
 *
 * Thesis: When the order book has a heavily imbalanced bid/ask wall within
 * 1% of the current price, it creates a magnetic pull — price tends to move
 * toward large walls (support/resistance) before blowing through them.
 *
 * Two plays:
 *  A. Bid wall support: large bids stacked below price → buy the support
 *  B. Ask wall absorption: price pressing into a large ask wall →
 *     wait for the wall to be absorbed (eaten), then ride the breakout
 *
 * This requires L2 order book data (Coinbase Advanced WebSocket).
 */

import type { TradeSignal, ScannerConfig } from '../types.js';
import { randomUUID } from 'crypto';

interface OrderBookLevel {
  price: number;
  size: number; // in base currency
}

interface OrderBookSnapshot {
  bids: OrderBookLevel[]; // sorted descending
  asks: OrderBookLevel[]; // sorted ascending
  lastUpdated: number;
}

const orderBooks = new Map<string, OrderBookSnapshot>();

export function updateOrderBook(symbol: string, bids: OrderBookLevel[], asks: OrderBookLevel[]): void {
  orderBooks.set(symbol, {
    bids: [...bids].sort((a, b) => b.price - a.price),
    asks: [...asks].sort((a, b) => a.price - b.price),
    lastUpdated: Date.now(),
  });
}

function sumBookDepth(levels: OrderBookLevel[], from: number, pricePct: number): number {
  const limit = from * (1 + pricePct);
  return levels
    .filter(l => Math.abs(l.price - from) / from <= pricePct)
    .reduce((s, l) => s + l.size * l.price, 0);
}

export function scanOrderBookImbalance(
  symbol: string,
  productId: string,
  currentPrice: number,
  config: ScannerConfig,
): TradeSignal | null {
  const book = orderBooks.get(symbol);
  if (!book || Date.now() - book.lastUpdated > 30_000) return null; // stale

  // Measure bid/ask depth within 1% of price
  const bidDepthUsd = sumBookDepth(book.bids, currentPrice, 0.01);
  const askDepthUsd = sumBookDepth(book.asks, currentPrice, 0.01);
  const totalDepth = bidDepthUsd + askDepthUsd;

  if (totalDepth < 500_000) return null; // not enough liquidity to signal

  const imbalanceRatio = bidDepthUsd / (askDepthUsd + 1); // >1 = bid heavy

  // ── Bid wall support play ─────────────────────────────────────────────
  if (imbalanceRatio > 3.0 && bidDepthUsd > 2_000_000) {
    const wallScore = Math.min(30, imbalanceRatio * 5);
    const sizeScore = Math.min(20, bidDepthUsd / 200_000);
    const score = Math.min(82, 42 + wallScore + sizeScore);

    if (score >= config.minQualScoreScalp) {
      return {
        id: randomUUID(),
        symbol,
        productId,
        strategy: 'orderbook_imbalance',
        side: 'long',
        tier: 'scalp',
        score,
        confidence: 'low',
        sources: ['orderbook'],
        reasoning: `${symbol} bid/ask ratio ${imbalanceRatio.toFixed(1)}× within 1%, $${(bidDepthUsd / 1e6).toFixed(1)}M bid wall support`,
        entryPrice: currentPrice,
        stopPrice: currentPrice * 0.99,
        suggestedSizeUsd: 40,
        expiresAt: Date.now() + 300_000, // 5 min
        createdAt: Date.now(),
      };
    }
  }

  // ── Ask wall short (distribution wall) ───────────────────────────────
  if (imbalanceRatio < 0.33 && askDepthUsd > 2_000_000) {
    const wallScore = Math.min(25, (1 / (imbalanceRatio + 0.01)) * 3);
    const sizeScore = Math.min(20, askDepthUsd / 200_000);
    const score = Math.min(80, 40 + wallScore + sizeScore);

    if (score >= config.minQualScoreScalp) {
      return {
        id: randomUUID(),
        symbol,
        productId,
        strategy: 'orderbook_imbalance',
        side: 'short',
        tier: 'scalp',
        score,
        confidence: 'low',
        sources: ['orderbook'],
        reasoning: `${symbol} ask/bid ratio ${(1 / imbalanceRatio).toFixed(1)}× within 1%, $${(askDepthUsd / 1e6).toFixed(1)}M ask wall resistance`,
        entryPrice: currentPrice,
        stopPrice: currentPrice * 1.01,
        suggestedSizeUsd: 35,
        expiresAt: Date.now() + 300_000,
        createdAt: Date.now(),
      };
    }
  }

  return null;
}
