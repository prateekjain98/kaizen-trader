/**
 * Paper trading executor — simulates order fills at current market price.
 *
 * Simulates realistic execution by applying:
 *  - Slippage model: 0.05% on buys, 0.03% on sells (conservative estimate)
 *  - Per-account balance tracking (starts at $10,000 USDC by default)
 *  - Execution latency simulation (50-150ms random delay)
 *
 * All paper trades are logged identically to live trades, just with
 * paperTrading=true. The self-healing engine treats them the same.
 */

import { randomUUID } from 'crypto';
import { log } from '../storage/database.js';
import type { Trade } from '../types.js';

const INITIAL_BALANCE_USD = 10_000;
const SLIPPAGE_BUY  = 0.0005; // 0.05%
const SLIPPAGE_SELL = 0.0003; // 0.03%
const COMMISSION    = 0.006;  // 0.6% Coinbase Advanced maker fee (conservative)

let balance = INITIAL_BALANCE_USD;
const holdings = new Map<string, number>(); // symbol → quantity

function simulateDelay(): Promise<void> {
  const ms = 50 + Math.random() * 100;
  return new Promise(resolve => setTimeout(resolve, ms));
}

export async function paperBuy(
  symbol: string,
  productId: string,
  sizeUsd: number,
  positionId: string,
  marketPrice: number,
): Promise<Trade> {
  await simulateDelay();

  if (sizeUsd > balance) {
    sizeUsd = balance;
    log('warn', `Paper: insufficient balance, capped order to $${sizeUsd.toFixed(2)}`, { symbol });
  }

  const fillPrice = marketPrice * (1 + SLIPPAGE_BUY);
  const commission = sizeUsd * COMMISSION;
  const netSizeUsd = sizeUsd - commission;
  const quantity = netSizeUsd / fillPrice;

  balance -= sizeUsd;
  holdings.set(symbol, (holdings.get(symbol) ?? 0) + quantity);

  const trade: Trade = {
    id: randomUUID(),
    positionId,
    side: 'buy',
    symbol,
    quantity,
    sizeUsd,
    price: fillPrice,
    status: 'paper',
    paperTrading: true,
    placedAt: Date.now(),
  };

  log('trade', `[PAPER] BUY ${symbol} $${sizeUsd.toFixed(0)} @ ${fillPrice.toFixed(4)} (slip +${(SLIPPAGE_BUY * 100).toFixed(2)}% fee ${commission.toFixed(2)})`, {
    symbol, data: { fillPrice, quantity, commission, balanceAfter: balance },
  });

  return trade;
}

export async function paperSell(
  symbol: string,
  productId: string,
  quantity: number,
  positionId: string,
  marketPrice: number,
): Promise<Trade> {
  await simulateDelay();

  const held = holdings.get(symbol) ?? 0;
  const actualQty = Math.min(quantity, held);

  const fillPrice = marketPrice * (1 - SLIPPAGE_SELL);
  const grossProceeds = actualQty * fillPrice;
  const commission = grossProceeds * COMMISSION;
  const netProceeds = grossProceeds - commission;

  holdings.set(symbol, held - actualQty);
  balance += netProceeds;

  const trade: Trade = {
    id: randomUUID(),
    positionId,
    side: 'sell',
    symbol,
    quantity: actualQty,
    sizeUsd: netProceeds,
    price: fillPrice,
    status: 'paper',
    paperTrading: true,
    placedAt: Date.now(),
  };

  log('trade', `[PAPER] SELL ${symbol} ${actualQty.toFixed(6)} @ ${fillPrice.toFixed(4)} net $${netProceeds.toFixed(2)} (slip -${(SLIPPAGE_SELL * 100).toFixed(2)}%)`, {
    symbol, data: { fillPrice, quantity: actualQty, commission, balanceAfter: balance },
  });

  return trade;
}

export function getPaperBalance(): number { return balance; }
export function getPaperHoldings(): Map<string, number> { return new Map(holdings); }
export function resetPaperAccount(): void {
  balance = INITIAL_BALANCE_USD;
  holdings.clear();
}
