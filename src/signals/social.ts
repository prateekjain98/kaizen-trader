/**
 * LunarCrush social signal fetcher.
 *
 * LunarCrush aggregates signals across Twitter/X, Reddit, YouTube, TikTok,
 * and Instagram — one key covers all social intelligence.
 *
 * We use three signals per token:
 *  - galaxy_score  (0–100): overall social health composite
 *  - alt_rank      (1–N):   relative social rank (lower = hotter)
 *  - social_volume (number): raw interactions in last 24h
 *
 * We also detect velocity spikes by comparing current volume to our
 * rolling 24h average — a 3× spike on top of a rising galaxy_score
 * is a strong early entry signal.
 */

import { env } from '../config.js';
import { log } from '../storage/database.js';

// ─── Types ────────────────────────────────────────────────────────────────────

interface LCAsset {
  symbol: string;
  galaxy_score: number;
  alt_rank: number;
  social_volume: number;
  social_score: number;
  market_cap_rank: number;
}

interface LCResponse {
  data: LCAsset[];
}

export interface SocialSentiment {
  symbol: string;
  galaxyScore: number;     // 0-100
  altRank: number;         // lower is better
  socialVolume: number;
  velocityMultiple: number;  // current vs 24h rolling avg
  sentiment: number;         // -1 to +1 derived from galaxy_score
  sampledAt: number;
}

// ─── Velocity tracker ─────────────────────────────────────────────────────────

const volumeHistory = new Map<string, number[]>(); // symbol → hourly volumes (24 buckets)

function computeVelocity(symbol: string, currentVolume: number): number {
  if (!volumeHistory.has(symbol)) volumeHistory.set(symbol, []);
  const hist = volumeHistory.get(symbol)!;

  const avg = hist.length > 0 ? hist.reduce((a, b) => a + b, 0) / hist.length : currentVolume;
  hist.push(currentVolume);
  if (hist.length > 24) hist.shift();

  return avg > 0 ? currentVolume / avg : 1;
}

// ─── Galaxy score → sentiment ─────────────────────────────────────────────────

function galaxyToSentiment(score: number): number {
  if (score >= 70) return 0.7;
  if (score >= 55) return 0.3;
  if (score >= 35) return 0.0;
  if (score >= 20) return -0.3;
  return -0.7;
}

// ─── Fetch ────────────────────────────────────────────────────────────────────

let lastFetchAt = 0;
let cached: SocialSentiment[] = [];
const CACHE_TTL_MS = 180_000; // 3 min

export async function fetchSocialSentiment(symbols: string[]): Promise<SocialSentiment[]> {
  if (!env.lunarCrushApiKey) return [];
  if (Date.now() - lastFetchAt < CACHE_TTL_MS) return cached;

  // LunarCrush free tier: query up to 10 symbols per request
  const results: SocialSentiment[] = [];

  const chunks: string[][] = [];
  for (let i = 0; i < symbols.length; i += 10) chunks.push(symbols.slice(i, i + 10));

  for (const chunk of chunks) {
    const url = `https://lunarcrush.com/api4/public/coins/list/v2?symbols=${chunk.join(',')}&key=${env.lunarCrushApiKey}`;
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(8_000) });
      if (!res.ok) {
        log('warn', `LunarCrush fetch failed: ${res.status}`);
        continue;
      }
      const data = await res.json() as LCResponse;
      for (const asset of data.data) {
        const sym = asset.symbol.toUpperCase();
        if (!chunk.includes(sym)) continue;
        const velocity = computeVelocity(sym, asset.social_volume);
        results.push({
          symbol: sym,
          galaxyScore: asset.galaxy_score,
          altRank: asset.alt_rank,
          socialVolume: asset.social_volume,
          velocityMultiple: velocity,
          sentiment: galaxyToSentiment(asset.galaxy_score) + (velocity > 3 ? 0.2 : 0),
          sampledAt: Date.now(),
        });
      }
    } catch (err) {
      log('warn', `LunarCrush network error: ${String(err)}`);
    }
  }

  lastFetchAt = Date.now();
  cached = results;
  return results;
}
