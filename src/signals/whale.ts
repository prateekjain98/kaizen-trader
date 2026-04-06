/**
 * Whale Alert signal fetcher.
 *
 * Fetches transactions >$3M from the Whale Alert API and classifies them
 * as accumulation or distribution signals by destination type.
 *
 * Key insight: not all large transfers are equal.
 *   - Exchange inflow  = likely selling (bearish for that token)
 *   - Exchange outflow = likely accumulation into cold storage (bullish)
 *   - Unknown wallet   = ambiguous, weighted lower
 *   - Miner wallet     = selling pressure (bearish)
 *
 * We maintain a rolling 2h net flow window per symbol.
 * The whale tracker strategy (strategies/whale-tracker.ts) reads from this.
 */

import { env } from '../config.js';
import { log } from '../storage/database.js';
import { onWhaleTransfer } from '../strategies/whale-tracker.js';

// ─── Types ────────────────────────────────────────────────────────────────────

type WalletType = 'exchange' | 'unknown_wallet' | 'known_fund' | 'miner';

interface WATransaction {
  blockchain: string;
  symbol: string;
  id: string;
  transaction_type: string;
  hash: string;
  from: { address: string; owner?: string; owner_type: string };
  to: { address: string; owner?: string; owner_type: string };
  timestamp: number;
  amount: number;
  amount_usd: number;
}

interface WAResponse {
  result: string;
  count: number;
  transactions: WATransaction[];
}

// ─── Owner type normalizer ────────────────────────────────────────────────────

function toWalletType(ownerType: string): WalletType {
  switch (ownerType.toLowerCase()) {
    case 'exchange':   return 'exchange';
    case 'fund':
    case 'custodian':  return 'known_fund';
    case 'miner':      return 'miner';
    default:           return 'unknown_wallet';
  }
}

// ─── Fetch loop ────────────────────────────────────────────────────────────────

const MIN_USD = 3_000_000;
const seenTxIds = new Set<string>();
let lastCursor = 0;
let lastFetchAt = 0;
const POLL_INTERVAL_MS = 120_000; // 2 min

export async function pollWhaleAlerts(symbols: string[]): Promise<void> {
  if (!env.whaleAlertApiKey) return;
  if (Date.now() - lastFetchAt < POLL_INTERVAL_MS) return;

  const since = lastCursor || Math.floor((Date.now() - 7_200_000) / 1000); // default: last 2h
  const url = `https://api.whale-alert.io/v1/transactions?api_key=${env.whaleAlertApiKey}&min_value=${MIN_USD}&start=${since}&limit=100`;

  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(8_000) });
    if (!res.ok) {
      log('warn', `Whale Alert fetch failed: ${res.status}`);
      return;
    }
    const data = await res.json() as WAResponse;
    if (data.result !== 'success') return;

    let newCursor = lastCursor;

    for (const tx of data.transactions) {
      if (seenTxIds.has(tx.id)) continue;
      seenTxIds.add(tx.id);

      const sym = tx.symbol.toUpperCase();
      if (!symbols.includes(sym)) continue;

      newCursor = Math.max(newCursor, tx.timestamp);

      onWhaleTransfer({
        symbol: sym,
        amountUsd: tx.amount_usd,
        fromType: toWalletType(tx.from.owner_type),
        toType: toWalletType(tx.to.owner_type),
        knownWallet: tx.from.owner ?? tx.to.owner,
        ts: tx.timestamp * 1000,
      });

      log('info', `Whale: $${(tx.amount_usd / 1e6).toFixed(0)}M ${sym} ${tx.from.owner_type} → ${tx.to.owner_type}`, {
        symbol: sym,
        data: { amountUsd: tx.amount_usd, from: tx.from.owner_type, to: tx.to.owner_type },
      });
    }

    lastCursor = newCursor;
    lastFetchAt = Date.now();

    // Prevent unbounded growth
    if (seenTxIds.size > 5_000) {
      const arr = Array.from(seenTxIds);
      seenTxIds.clear();
      for (const id of arr.slice(-2_000)) seenTxIds.add(id);
    }
  } catch (err) {
    log('warn', `Whale Alert network error: ${String(err)}`);
  }
}
