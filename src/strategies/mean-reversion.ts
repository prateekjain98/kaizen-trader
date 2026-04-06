/**
 * Mean Reversion Strategy (NEW — not in v1)
 *
 * Thesis: When price deviates significantly from VWAP or deviates below a
 * rolling moving average AND RSI is oversold, price tends to snap back.
 *
 * Entry conditions (long):
 *  - Price is >vwapDeviationPct% BELOW the rolling VWAP
 *  - RSI(14) < rsiOversold threshold (default 30)
 *  - Volume is NOT spiking (we want consolidation, not a crash continuation)
 *  - Market phase is NOT 'bear' (we avoid catching falling knives)
 *
 * Entry conditions (short):
 *  - Price is >vwapDeviationPct% ABOVE VWAP
 *  - RSI(14) > rsiOverbought (default 70)
 *  - Market phase is NOT 'bull'
 *
 * Exit: fixed target of 50% reversion to VWAP, tight 2% initial stop
 */

import type { TradeSignal, ScannerConfig, MarketContext } from '../types.js';
import { randomUUID } from 'crypto';

interface OHLCVSample {
  close: number;
  volume: number;
  ts: number;
}

const ohlcvBuffers = new Map<string, OHLCVSample[]>();
const MAX_SAMPLES = 200;

export function pushOHLCVSample(symbol: string, close: number, volume: number): void {
  if (!ohlcvBuffers.has(symbol)) ohlcvBuffers.set(symbol, []);
  const buf = ohlcvBuffers.get(symbol)!;
  buf.push({ close, volume, ts: Date.now() });
  if (buf.length > MAX_SAMPLES) buf.shift();
}

function computeVWAP(samples: OHLCVSample[]): number | null {
  if (samples.length === 0) return null;
  let sumPV = 0, sumV = 0;
  for (const s of samples) {
    sumPV += s.close * s.volume;
    sumV += s.volume;
  }
  return sumV === 0 ? null : sumPV / sumV;
}

function computeRSI(samples: OHLCVSample[], period = 14): number | null {
  if (samples.length < period + 1) return null;
  const recent = samples.slice(-(period + 1));
  let gains = 0, losses = 0;
  for (let i = 1; i < recent.length; i++) {
    const diff = recent[i]!.close - recent[i - 1]!.close;
    if (diff > 0) gains += diff;
    else losses += -diff;
  }
  const avgGain = gains / period;
  const avgLoss = losses / period;
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}

function computeAvgVolume(samples: OHLCVSample[]): number {
  if (samples.length === 0) return 0;
  return samples.reduce((s, x) => s + x.volume, 0) / samples.length;
}

export function scanMeanReversion(
  symbol: string,
  productId: string,
  currentPrice: number,
  config: ScannerConfig,
  ctx: MarketContext,
): TradeSignal | null {
  const buf = ohlcvBuffers.get(symbol);
  if (!buf || buf.length < 30) return null;

  const vwap = computeVWAP(buf);
  const rsi = computeRSI(buf);
  const avgVolume = computeAvgVolume(buf.slice(-20));
  const currentVolume = buf[buf.length - 1]?.volume ?? 0;

  if (!vwap || !rsi) return null;

  const deviationFromVwap = (currentPrice - vwap) / vwap;
  const volumeRatio = currentVolume / avgVolume;

  // ── Long entry: price below VWAP + RSI oversold ─────────────────────────
  if (
    deviationFromVwap < -config.vwapDeviationPct &&
    rsi < config.rsiOversold &&
    volumeRatio < 1.5 && // not a crash continuation
    ctx.phase !== 'bear' &&
    ctx.phase !== 'extreme_fear'
  ) {
    const deviationScore = Math.min(30, Math.abs(deviationFromVwap) * 500);
    const rsiScore = Math.min(20, (config.rsiOversold - rsi));
    const score = Math.min(90, 40 + deviationScore + rsiScore);
    const targetPrice = vwap; // target full reversion to VWAP

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'mean_reversion',
      side: 'long',
      tier: 'swing',
      score,
      confidence: score > 70 ? 'medium' : 'low',
      sources: ['price_action'],
      reasoning: `${symbol} ${(deviationFromVwap * 100).toFixed(1)}% below VWAP, RSI=${rsi.toFixed(0)} oversold — reversion expected`,
      entryPrice: currentPrice,
      targetPrice,
      stopPrice: currentPrice * 0.98, // tight 2% initial stop
      suggestedSizeUsd: 80,
      expiresAt: Date.now() + 1_800_000, // 30 min
      createdAt: Date.now(),
    };
  }

  // ── Short entry: price above VWAP + RSI overbought ───────────────────────
  if (
    deviationFromVwap > config.vwapDeviationPct &&
    rsi > config.rsiOverbought &&
    volumeRatio < 1.5 &&
    ctx.phase !== 'bull' &&
    ctx.phase !== 'extreme_greed'
  ) {
    const deviationScore = Math.min(30, deviationFromVwap * 500);
    const rsiScore = Math.min(20, rsi - config.rsiOverbought);
    const score = Math.min(88, 38 + deviationScore + rsiScore);
    const targetPrice = vwap;

    return {
      id: randomUUID(),
      symbol,
      productId,
      strategy: 'mean_reversion',
      side: 'short',
      tier: 'swing',
      score,
      confidence: score > 68 ? 'medium' : 'low',
      sources: ['price_action'],
      reasoning: `${symbol} ${(deviationFromVwap * 100).toFixed(1)}% above VWAP, RSI=${rsi.toFixed(0)} overbought — mean reversion short`,
      entryPrice: currentPrice,
      targetPrice,
      stopPrice: currentPrice * 1.02,
      suggestedSizeUsd: 60,
      expiresAt: Date.now() + 1_800_000,
      createdAt: Date.now(),
    };
  }

  return null;
}
