/**
 * Liquidation Cascade Strategy (NEW — not in v1)
 *
 * Thesis: When a large cluster of liquidations is about to trigger, it creates
 * a self-reinforcing cascade. We can profit by:
 *   - Trading WITH the cascade direction (short before a long cascade)
 *   - Or by buying the BOTTOM of a cascade (dip buy after mass liquidations)
 *
 * Signal sources:
 *   - Binance liquidation WebSocket (real-time large liquidation events)
 *   - Coinglass liquidation heatmaps (estimated liquidation levels)
 *   - Open interest drops (confirms cascade is happening)
 *
 * Strategy A — Cascade Rider: Enter short when first large long liquidation fires
 *   and OI starts dropping sharply (more longs will cascade)
 *
 * Strategy B — Cascade Dip Buyer: Wait for OI to stabilize (cascade exhausted),
 *   then enter long if price is >8% below where cascade started
 */

import type { TradeSignal, ScannerConfig, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

export interface LiquidationEvent {
  symbol: string;
  side: 'buy' | 'sell'; // liquidation side ('buy' = short liq, 'sell' = long liq)
  sizeUsd: number;
  price: number;
  ts: number;
}

interface LiquidationWindow {
  events: LiquidationEvent[];
  totalLongLiqsUsd: number;
  totalShortLiqsUsd: number;
  oiAtWindowStart: number;
  currentOI: number;
  windowStartPrice: number;
}

const windows = new Map<string, LiquidationWindow>();

export function onLiquidationEvent(event: LiquidationEvent, currentOI: number): void {
  const now = Date.now();
  if (!windows.has(event.symbol)) {
    windows.set(event.symbol, {
      events: [],
      totalLongLiqsUsd: 0,
      totalShortLiqsUsd: 0,
      oiAtWindowStart: currentOI,
      currentOI,
      windowStartPrice: event.price,
    });
  }

  const win = windows.get(event.symbol)!;
  win.events.push(event);
  win.currentOI = currentOI;

  // Track by direction
  if (event.side === 'sell') win.totalLongLiqsUsd += event.sizeUsd;  // long being liquidated
  else win.totalShortLiqsUsd += event.sizeUsd;                        // short being liquidated

  // Prune events older than 10 minutes
  const cutoff = now - 600_000;
  win.events = win.events.filter(e => e.ts >= cutoff);
  win.totalLongLiqsUsd = win.events.filter(e => e.side === 'sell').reduce((s, e) => s + e.sizeUsd, 0);
  win.totalShortLiqsUsd = win.events.filter(e => e.side === 'buy').reduce((s, e) => s + e.sizeUsd, 0);
}

export function scanLiquidationCascade(
  symbol: string,
  productId: string,
  currentPrice: number,
  config: ScannerConfig,
  ctx: MarketContext,
): TradeSignal | null {
  const win = windows.get(symbol);
  if (!win || win.events.length < 3) return null;

  const oiDrop = win.oiAtWindowStart > 0
    ? (win.oiAtWindowStart - win.currentOI) / win.oiAtWindowStart
    : 0;

  // ── Strategy A: Ride the long liquidation cascade ─────────────────────
  if (
    win.totalLongLiqsUsd > 2_000_000 && // >$2M in long liquidations
    oiDrop > 0.05 && // OI dropped >5% (cascade accelerating)
    ctx.phase !== 'extreme_fear' // don't pile on if already capitulated
  ) {
    const sizeScore = Math.min(30, win.totalLongLiqsUsd / 200_000);
    const oiScore = Math.min(20, oiDrop * 200);
    const score = Math.min(85, 45 + sizeScore + oiScore);

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'liquidation_cascade',
      side: 'short',
      tier: 'scalp',
      score,
      confidence: score > 70 ? 'medium' : 'low',
      sources: ['liquidation_data'],
      reasoning: `${symbol} $${(win.totalLongLiqsUsd / 1e6).toFixed(1)}M longs liquidated in 10m, OI down ${(oiDrop * 100).toFixed(0)}% — cascade rider short`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 1.025,
      suggestedSizeUsd: 50,
      expiresAt: Date.now() + 600_000, // 10 min
      createdAt: Date.now(),
    };
  }

  // ── Strategy B: Buy the dip post-cascade ─────────────────────────────
  const priceDropPct = (win.windowStartPrice - currentPrice) / win.windowStartPrice;
  const cascadeExhausted = oiDrop > 0.10 && win.events[win.events.length - 1]!.ts < Date.now() - 120_000;

  if (
    win.totalLongLiqsUsd > 5_000_000 && // big cascade
    priceDropPct > 0.08 && // price down >8%
    cascadeExhausted && // liquidations stopped
    ctx.phase !== 'bear' // not in a structural downtrend
  ) {
    const dropScore = Math.min(30, priceDropPct * 200);
    const score = Math.min(82, 52 + dropScore);

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'liquidation_cascade',
      side: 'long',
      tier: 'swing',
      score,
      confidence: score > 72 ? 'medium' : 'low',
      sources: ['liquidation_data'],
      reasoning: `${symbol} down ${(priceDropPct * 100).toFixed(0)}% from cascade start, $${(win.totalLongLiqsUsd / 1e6).toFixed(1)}M liquidated, OI stabilizing — dip buy`,
      entryPrice: currentPrice,
      stopPrice: currentPrice * 0.97,
      suggestedSizeUsd: 90,
      expiresAt: Date.now() + 3_600_000, // 1h
      createdAt: Date.now(),
    };
  }

  return null;
}
