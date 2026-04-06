/**
 * DeFiLlama protocol revenue fetcher.
 *
 * Endpoint: https://api.llama.fi/overview/fees
 * Returns daily fee revenue for 2000+ DeFi protocols.
 *
 * Signal logic:
 *   - Today's fees / 7-day average fee > threshold → revenue spike
 *   - Token price hasn't moved yet (price change < 12%) → mispricing window
 *   - TVL is stable or growing → protocol retaining users
 *
 * This is a fundamentals-driven signal — holds hours to days, not minutes.
 * Maps to the protocol_revenue strategy in strategies/protocol-revenue.ts.
 */

import { log } from '../storage/database.js';

// ─── Types ────────────────────────────────────────────────────────────────────

interface LlamaProtocol {
  name: string;
  displayName?: string;
  disabled?: boolean;
  total24h: number | null;
  total7d: number | null;
  totalAllTime: number | null;
  chains: string[];
  gecko_id?: string;
  symbol?: string;
}

interface LlamaFeesResponse {
  protocols: LlamaProtocol[];
}

export interface ProtocolRevenueData {
  protocol: string;
  symbol: string;
  revenue24h: number;
  revenue7dAvg: number;
  revenueMultiple: number; // 24h / 7dAvg
  sampledAt: number;
}

// ─── Symbol overrides for tokens not in DeFiLlama gecko_id ───────────────────

const PROTOCOL_SYMBOL_MAP: Record<string, string> = {
  uniswap:   'UNI',
  aave:      'AAVE',
  curve:     'CRV',
  compound:  'COMP',
  lido:      'LDO',
  makerdao:  'MKR',
  synthetix: 'SNX',
  gmx:       'GMX',
  dydx:      'DYDX',
  ondo:      'ONDO',
  maple:     'MPL',
};

function resolveSymbol(p: LlamaProtocol): string | undefined {
  const name = p.name.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (PROTOCOL_SYMBOL_MAP[name]) return PROTOCOL_SYMBOL_MAP[name];
  if (p.symbol && p.symbol.length > 0) return p.symbol.toUpperCase();
  return undefined;
}

// ─── Fetch ────────────────────────────────────────────────────────────────────

let cached: ProtocolRevenueData[] = [];
let lastFetchAt = 0;
const CACHE_TTL_MS = 3_600_000; // 1 hour — data updates daily

export async function fetchProtocolRevenue(): Promise<ProtocolRevenueData[]> {
  if (Date.now() - lastFetchAt < CACHE_TTL_MS) return cached;

  try {
    const res = await fetch('https://api.llama.fi/overview/fees?excludeTotalDataChartBreakdown=true', {
      signal: AbortSignal.timeout(15_000),
    });
    if (!res.ok) {
      log('warn', `DeFiLlama fees fetch failed: ${res.status}`);
      return cached;
    }

    const data = await res.json() as LlamaFeesResponse;
    const results: ProtocolRevenueData[] = [];

    for (const protocol of data.protocols) {
      if (protocol.disabled) continue;
      if (!protocol.total24h || !protocol.total7d) continue;
      if (protocol.total24h < 1_000) continue; // filter micro-protocols

      const symbol = resolveSymbol(protocol);
      if (!symbol) continue;

      const revenue7dAvg = protocol.total7d / 7;
      const revenueMultiple = revenue7dAvg > 0 ? protocol.total24h / revenue7dAvg : 0;

      results.push({
        protocol: protocol.displayName ?? protocol.name,
        symbol,
        revenue24h: protocol.total24h,
        revenue7dAvg,
        revenueMultiple,
        sampledAt: Date.now(),
      });
    }

    // Sort by revenue multiple descending — highest spike at top
    results.sort((a, b) => b.revenueMultiple - a.revenueMultiple);

    lastFetchAt = Date.now();
    cached = results;
    log('info', `DeFiLlama: loaded ${results.length} protocol revenue records`);
    return results;
  } catch (err) {
    log('warn', `DeFiLlama network error: ${String(err)}`);
    return cached;
  }
}
