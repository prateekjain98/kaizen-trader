/**
 * Momentum Breakout Strategy (swing + scalp tiers)
 *
 * Entry conditions:
 *  - Price has moved +momentumPct% within the lookback window
 *  - Volume has spiked volumeMultiplier× above the rolling baseline
 *  - Not in per-symbol cooldown
 *
 * Exit: dynamic trailing stop, widens as price runs, tightens on news
 */

import type { TradeSignal, ScannerConfig, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

interface PriceSample {
  price: number;
  volume24h: number;
  ts: number;
}

// Rolling price buffers per symbol — maintained by the main price feed
const swingBuffers = new Map<string, PriceSample[]>();
const scalpBuffers = new Map<string, PriceSample[]>();
const cooldowns = new Map<string, number>(); // symbol → cooldown expiry ts

function getBuffer(symbol: string, tier: 'swing' | 'scalp'): PriceSample[] {
  const map = tier === 'swing' ? swingBuffers : scalpBuffers;
  if (!map.has(symbol)) map.set(symbol, []);
  return map.get(symbol)!;
}

export function pushPriceSample(symbol: string, price: number, volume24h: number): void {
  const now = Date.now();
  const sample: PriceSample = { price, volume24h, ts: now };

  // Swing: keep 1 hour
  const sw = getBuffer(symbol, 'swing');
  sw.push(sample);
  const swCutoff = now - 3_600_000;
  while (sw[0] && sw[0].ts < swCutoff) sw.shift();

  // Scalp: keep 5 minutes
  const sc = getBuffer(symbol, 'scalp');
  sc.push(sample);
  const scCutoff = now - 300_000;
  while (sc[0] && sc[0].ts < scCutoff) sc.shift();
}

function computeMomentum(samples: PriceSample[]): { pct: number; avgVolume: number; currentVolume: number } | null {
  if (samples.length < 5) return null;
  const first = samples[0]!;
  const last = samples[samples.length - 1]!;
  const pct = (last.price - first.price) / first.price;
  const avgVolume = samples.reduce((s, x) => s + x.volume24h, 0) / samples.length;
  const currentVolume = last.volume24h;
  return { pct, avgVolume, currentVolume };
}

function hasCooldown(symbol: string): boolean {
  const expiry = cooldowns.get(symbol);
  return !!expiry && expiry > Date.now();
}

function setCooldown(symbol: string, ms: number): void {
  cooldowns.set(symbol, Date.now() + ms);
}

export function scanMomentum(
  symbol: string,
  productId: string,
  currentPrice: number,
  config: ScannerConfig,
  ctx: MarketContext,
): TradeSignal | null {
  if (hasCooldown(symbol)) return null;

  // ── Swing tier ──────────────────────────────────────────────────────────
  const swing = computeMomentum(getBuffer(symbol, 'swing'));
  if (swing && swing.pct >= config.momentumPctSwing) {
    const volumeOk = swing.currentVolume >= swing.avgVolume * config.volumeMultiplierSwing;
    if (volumeOk) {
      const marketBonus = ctx.phase === 'bull' ? 10 : ctx.phase === 'bear' ? -15 : 0;
      const score = Math.min(95, 55 + swing.pct * 200 + marketBonus);
      if (score >= config.minQualScoreSwing) {
        setCooldown(symbol, config.cooldownMsSwing);
        return {
          id: randomUUID(),
          symbol,
          productId,
          strategy: 'momentum_swing',
          side: 'long',
          tier: 'swing',
          score,
          confidence: score > 75 ? 'high' : score > 60 ? 'medium' : 'low',
          sources: ['price_action'],
          reasoning: `${symbol} +${(swing.pct * 100).toFixed(1)}% in 1h with ${(swing.currentVolume / swing.avgVolume).toFixed(1)}× volume spike`,
          entryPrice: currentPrice,
          stopPrice: currentPrice * (1 - config.baseTrailPctSwing),
          suggestedSizeUsd: 100,
          expiresAt: Date.now() + 300_000, // 5 min
          createdAt: Date.now(),
        };
      }
    }
  }

  // ── Scalp tier ──────────────────────────────────────────────────────────
  const scalp = computeMomentum(getBuffer(symbol, 'scalp'));
  if (scalp && scalp.pct >= config.momentumPctScalp) {
    const volumeOk = scalp.currentVolume >= scalp.avgVolume * config.volumeMultiplierScalp;
    // Freshness: at least 40% of the move happened in the last 2 minutes
    const buf = getBuffer(symbol, 'scalp');
    const recent2m = buf.filter(s => s.ts >= Date.now() - 120_000);
    const freshnessPct = recent2m.length > 0
      ? (recent2m[recent2m.length - 1]!.price - recent2m[0]!.price) / recent2m[0]!.price
      : 0;
    const freshEnough = freshnessPct / scalp.pct >= 0.4;

    if (volumeOk && freshEnough) {
      const score = Math.min(90, 50 + scalp.pct * 150);
      if (score >= config.minQualScoreScalp) {
        setCooldown(symbol, config.cooldownMsScalp);
        return {
          id: randomUUID(),
          symbol,
          productId,
          strategy: 'momentum_scalp',
          side: 'long',
          tier: 'scalp',
          score,
          confidence: score > 70 ? 'medium' : 'low',
          sources: ['price_action'],
          reasoning: `${symbol} +${(scalp.pct * 100).toFixed(1)}% in 5m with ${(scalp.currentVolume / scalp.avgVolume).toFixed(1)}× volume, fresh move`,
          entryPrice: currentPrice,
          stopPrice: currentPrice * (1 - config.baseTrailPctScalp),
          suggestedSizeUsd: 40,
          expiresAt: Date.now() + 60_000, // 1 min
          createdAt: Date.now(),
        };
      }
    }
  }

  return null;
}
