/**
 * Binance Futures funding rate + open interest fetcher.
 *
 * Funding rates are the cost of holding a perpetual futures position.
 * Positive rate: longs pay shorts (market is over-leveraged long)
 * Negative rate: shorts pay longs (market is over-leveraged short)
 *
 * Extreme rates are a reliable contrarian signal — not a momentum signal.
 * This module also streams liquidation events via WebSocket.
 */

import { log } from '../storage/database.js';

// ─── Types ────────────────────────────────────────────────────────────────────

interface BinanceFundingRate {
  symbol: string;
  fundingRate: string;
  fundingTime: string;
}

interface BinanceOpenInterest {
  symbol: string;
  openInterest: string;
  time: number;
}

export interface FundingData {
  symbol: string;         // e.g. "SOL"
  binanceSymbol: string;  // e.g. "SOLUSDT"
  fundingRate: number;    // e.g. 0.001 = 0.1% per 8h
  nextFundingTime: number;
  openInterestUsd: number;
  openInterestChange24h: number; // pct change (requires two readings)
  sampledAt: number;
}

// ─── OI change tracker ────────────────────────────────────────────────────────

const oiHistory = new Map<string, { oi: number; ts: number }>();

function computeOIChange(symbol: string, currentOI: number): number {
  const prev = oiHistory.get(symbol);
  oiHistory.set(symbol, { oi: currentOI, ts: Date.now() });
  if (!prev) return 0;
  const ageHours = (Date.now() - prev.ts) / 3_600_000;
  if (ageHours > 26) return 0; // stale
  return prev.oi > 0 ? (currentOI - prev.oi) / prev.oi : 0;
}

// ─── Symbol mapping ───────────────────────────────────────────────────────────

// Maps our symbol names to Binance perp symbols
const SYMBOL_MAP: Record<string, string> = {
  BTC: 'BTCUSDT', ETH: 'ETHUSDT', SOL: 'SOLUSDT', BNB: 'BNBUSDT',
  ARB: 'ARBUSDT', OP: 'OPUSDT', AVAX: 'AVAXUSDT', MATIC: 'MATICUSDT',
  LINK: 'LINKUSDT', UNI: 'UNIUSDT', AAVE: 'AAVEUSDT', DOGE: 'DOGEUSDT',
  SUI: 'SUIUSDT', APT: 'APTUSDT', SEI: 'SEIUSDT', TIA: 'TIAUSDT',
};

function toBinanceSymbol(symbol: string): string | undefined {
  return SYMBOL_MAP[symbol];
}

// ─── Fetch ────────────────────────────────────────────────────────────────────

const BASE = 'https://fapi.binance.com';
let lastFetchAt = 0;
let cached: FundingData[] = [];
const CACHE_TTL_MS = 300_000; // 5 min

export async function fetchFundingData(symbols: string[]): Promise<FundingData[]> {
  if (Date.now() - lastFetchAt < CACHE_TTL_MS) return cached;

  const binanceSymbols = symbols.flatMap(s => {
    const b = toBinanceSymbol(s);
    return b ? [b] : [];
  });

  if (binanceSymbols.length === 0) return [];

  const results: FundingData[] = [];

  try {
    // Fetch funding rates
    const [fundingRes, oiRes] = await Promise.all([
      fetch(`${BASE}/fapi/v1/premiumIndex`, { signal: AbortSignal.timeout(8_000) }),
      fetch(`${BASE}/fapi/v1/openInterest?symbol=BTCUSDT`, { signal: AbortSignal.timeout(8_000) }), // sample to check reachability
    ]);

    if (!fundingRes.ok) {
      log('warn', `Binance funding fetch failed: ${fundingRes.status}`);
      return cached;
    }

    const allFunding = await fundingRes.json() as BinanceFundingRate[];
    const fundingMap = new Map(allFunding.map(f => [f.symbol, f]));

    // Fetch OI for each symbol individually (most reliable approach)
    await Promise.allSettled(
      binanceSymbols.map(async (binanceSym) => {
        const sym = Object.entries(SYMBOL_MAP).find(([, v]) => v === binanceSym)?.[0];
        if (!sym) return;

        const funding = fundingMap.get(binanceSym);
        if (!funding) return;

        const oiRes = await fetch(`${BASE}/fapi/v1/openInterest?symbol=${binanceSym}`, {
          signal: AbortSignal.timeout(5_000),
        });
        if (!oiRes.ok) return;

        const oi = await oiRes.json() as BinanceOpenInterest;
        const oiUsd = parseFloat(oi.openInterest); // in base token; multiply by price for USD
        const oiChange = computeOIChange(binanceSym, oiUsd);

        results.push({
          symbol: sym,
          binanceSymbol: binanceSym,
          fundingRate: parseFloat(funding.fundingRate),
          nextFundingTime: parseInt(funding.fundingTime),
          openInterestUsd: oiUsd,
          openInterestChange24h: oiChange,
          sampledAt: Date.now(),
        });
      })
    );
  } catch (err) {
    log('warn', `Binance funding network error: ${String(err)}`);
    return cached;
  }

  lastFetchAt = Date.now();
  cached = results;
  return results;
}
