/**
 * Coinbase Advanced Trade REST executor.
 *
 * Implements HMAC-SHA256 request signing per the Coinbase Advanced Trade API spec.
 * https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-auth
 *
 * Order placement flow:
 *  1. Validate API credentials are present
 *  2. Build the order body (market order with quote-currency size for buys)
 *  3. Sign the request with timestamp + method + path + body
 *  4. Execute and return the order ID or throw a typed error
 *
 * We use market orders exclusively — limit orders introduce slippage timing risk
 * in a momentum system where entry speed matters more than fill price.
 */

import { createHmac } from 'crypto';
import { env } from '../config.js';
import { log } from '../storage/database.js';
import type { Trade } from '../types.js';
import { randomUUID } from 'crypto';

// ─── Types ────────────────────────────────────────────────────────────────────

interface CoinbaseOrderResponse {
  success: boolean;
  order_id?: string;
  failure_reason?: string;
  error_response?: { error: string; message: string; preview_failure_reason?: string };
  order?: {
    order_id: string;
    product_id: string;
    status: string;
    average_filled_price: string;
    filled_size: string;
  };
}

interface CoinbasePreviewResponse {
  order_total: string;
  commission_total: string;
  slippage: string;
}

export class InsufficientFundsError extends Error {
  constructor(productId: string, sizeUsd: number) {
    super(`Insufficient funds to buy ${productId} for $${sizeUsd}`);
    this.name = 'InsufficientFundsError';
  }
}

export class ExchangeError extends Error {
  constructor(public readonly code: string, message: string) {
    super(message);
    this.name = 'ExchangeError';
  }
}

// ─── HMAC signing ─────────────────────────────────────────────────────────────

const BASE_URL = 'https://api.coinbase.com';

function sign(timestamp: number, method: string, path: string, body: string): string {
  const message = `${timestamp}${method.toUpperCase()}${path}${body}`;
  return createHmac('sha256', env.coinbaseApiSecret ?? '')
    .update(message)
    .digest('hex');
}

async function cbRequest<T>(method: 'GET' | 'POST', path: string, body?: object): Promise<T> {
  if (!env.coinbaseApiKey || !env.coinbaseApiSecret) {
    throw new ExchangeError('MISSING_CREDENTIALS', 'Coinbase API key/secret not configured');
  }

  const timestamp = Math.floor(Date.now() / 1000);
  const bodyStr = body ? JSON.stringify(body) : '';
  const signature = sign(timestamp, method, path, bodyStr);

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers: {
      'CB-ACCESS-KEY':       env.coinbaseApiKey,
      'CB-ACCESS-SIGN':      signature,
      'CB-ACCESS-TIMESTAMP': String(timestamp),
      'Content-Type':        'application/json',
    },
    body: bodyStr || undefined,
    signal: AbortSignal.timeout(10_000),
  });

  const text = await res.text();
  let parsed: T;
  try {
    parsed = JSON.parse(text) as T;
  } catch {
    throw new ExchangeError('PARSE_ERROR', `Non-JSON response (${res.status}): ${text.slice(0, 200)}`);
  }

  if (!res.ok) {
    const err = parsed as { error?: string; message?: string };
    throw new ExchangeError(err.error ?? 'HTTP_ERROR', err.message ?? `HTTP ${res.status}`);
  }

  return parsed;
}

// ─── Order placement ──────────────────────────────────────────────────────────

export async function placeBuyOrder(
  productId: string,
  sizeUsd: number,
  positionId: string,
): Promise<Trade> {
  const orderConfig = {
    market_market_ioc: {
      quote_size: sizeUsd.toFixed(2), // buy $X worth
    },
  };

  const body = {
    client_order_id: randomUUID(),
    product_id: productId,
    side: 'BUY',
    order_configuration: orderConfig,
  };

  log('info', `Placing BUY order: ${productId} $${sizeUsd}`, {
    symbol: productId.replace('-USD', ''),
    data: { productId, sizeUsd, positionId },
  });

  const response = await cbRequest<CoinbaseOrderResponse>('POST', '/api/v3/brokerage/orders', body);

  if (!response.success || !response.order_id) {
    const reason = response.failure_reason ?? response.error_response?.preview_failure_reason ?? 'unknown';
    if (reason.includes('INSUFFICIENT_FUND')) throw new InsufficientFundsError(productId, sizeUsd);
    throw new ExchangeError(reason, `Order failed: ${reason}`);
  }

  const order = response.order;
  const avgPrice = order ? parseFloat(order.average_filled_price) : 0;
  const filledSize = order ? parseFloat(order.filled_size) : sizeUsd / avgPrice;

  log('trade', `BUY filled: ${productId} $${sizeUsd} @ avg ${avgPrice.toFixed(4)}`, {
    symbol: productId.replace('-USD', ''),
    data: { orderId: response.order_id, avgPrice, filledSize },
  });

  return {
    id: randomUUID(),
    positionId,
    side: 'buy',
    symbol: productId.replace('-USD', ''),
    quantity: filledSize,
    sizeUsd,
    price: avgPrice,
    orderId: response.order_id,
    status: 'filled',
    paperTrading: false,
    placedAt: Date.now(),
  };
}

export async function placeSellOrder(
  productId: string,
  quantity: number,
  positionId: string,
): Promise<Trade> {
  const body = {
    client_order_id: randomUUID(),
    product_id: productId,
    side: 'SELL',
    order_configuration: {
      market_market_ioc: {
        base_size: quantity.toFixed(8), // sell X units
      },
    },
  };

  log('info', `Placing SELL order: ${productId} ${quantity} units`, {
    symbol: productId.replace('-USD', ''),
    data: { productId, quantity, positionId },
  });

  const response = await cbRequest<CoinbaseOrderResponse>('POST', '/api/v3/brokerage/orders', body);

  if (!response.success || !response.order_id) {
    const reason = response.failure_reason ?? 'unknown';
    throw new ExchangeError(reason, `Sell order failed: ${reason}`);
  }

  const order = response.order;
  const avgPrice = order ? parseFloat(order.average_filled_price) : 0;

  log('trade', `SELL filled: ${productId} ${quantity} units @ avg ${avgPrice.toFixed(4)}`, {
    symbol: productId.replace('-USD', ''),
    data: { orderId: response.order_id, avgPrice },
  });

  return {
    id: randomUUID(),
    positionId,
    side: 'sell',
    symbol: productId.replace('-USD', ''),
    quantity,
    sizeUsd: quantity * avgPrice,
    price: avgPrice,
    orderId: response.order_id,
    status: 'filled',
    paperTrading: false,
    placedAt: Date.now(),
  };
}

export async function getAccountBalances(): Promise<Record<string, number>> {
  const res = await cbRequest<{ accounts: Array<{ currency: string; available_balance: { value: string } }> }>(
    'GET', '/api/v3/brokerage/accounts?limit=250'
  );
  return Object.fromEntries(
    res.accounts
      .filter(a => parseFloat(a.available_balance.value) > 0)
      .map(a => [a.currency, parseFloat(a.available_balance.value)])
  );
}
