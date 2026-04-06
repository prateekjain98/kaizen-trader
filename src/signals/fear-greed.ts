/**
 * Fear & Greed Index fetcher (Alternative.me).
 *
 * The index is computed from:
 *  - Volatility (25%)
 *  - Market momentum / volume (25%)
 *  - Social media (15%)
 *  - Surveys (15%)
 *  - Bitcoin dominance (10%)
 *  - Google Trends (10%)
 *
 * We fetch both current and yesterday's value to detect direction of movement
 * (a falling index that's already at 20 is different from one still in freefall).
 *
 * Refreshed every 30 minutes — the index only updates daily, but we cache
 * appropriately and expose the movement delta.
 */

import { log } from '../storage/database.js';
import type { MarketContext, MarketPhase } from '../types.js';

interface FGIDataPoint {
  value: string;
  value_classification: string;
  timestamp: string;
}

interface FGIResponse {
  data: FGIDataPoint[];
}

export interface FearGreedReading {
  index: number;        // 0-100
  label: string;        // "Extreme Fear" | "Fear" | "Neutral" | "Greed" | "Extreme Greed"
  delta1d: number;      // today - yesterday (direction of movement)
  fetchedAt: number;
}

let cached: FearGreedReading | null = null;
let lastFetchAt = 0;
const CACHE_TTL_MS = 30 * 60_000; // 30 min

export async function fetchFearGreed(): Promise<FearGreedReading | null> {
  if (cached && Date.now() - lastFetchAt < CACHE_TTL_MS) return cached;

  try {
    const res = await fetch('https://api.alternative.me/fng/?limit=2', {
      signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) {
      log('warn', `Fear & Greed fetch failed: ${res.status}`);
      return cached;
    }
    const data = await res.json() as FGIResponse;
    if (!data.data || data.data.length < 1) return cached;

    const today = parseInt(data.data[0]!.value);
    const yesterday = data.data.length > 1 ? parseInt(data.data[1]!.value) : today;

    cached = {
      index: today,
      label: data.data[0]!.value_classification,
      delta1d: today - yesterday,
      fetchedAt: Date.now(),
    };
    lastFetchAt = Date.now();
    return cached;
  } catch (err) {
    log('warn', `Fear & Greed network error: ${String(err)}`);
    return cached;
  }
}

export function fearGreedToMarketPhase(fgi: number): MarketPhase {
  if (fgi <= 20) return 'extreme_fear';
  if (fgi >= 80) return 'extreme_greed';
  if (fgi <= 40) return 'bear';
  if (fgi >= 60) return 'bull';
  return 'neutral';
}

export function buildMarketContext(fgi: FearGreedReading, btcDominance: number): MarketContext {
  return {
    phase: fearGreedToMarketPhase(fgi.index),
    btcDominance,
    fearGreedIndex: fgi.index,
    totalMarketCapChangeD1: 0, // populated separately from CoinGecko
    timestamp: Date.now(),
  };
}
