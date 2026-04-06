/**
 * Coinbase Advanced Trade WebSocket feed.
 *
 * Manages two subscriptions:
 *  1. ticker   — real-time price ticks for all watched symbols
 *  2. level2   — order book updates (bids/asks) for L2 imbalance strategy
 *
 * Reconnection strategy:
 *  - Exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 30s
 *  - Resubscribes automatically on reconnect
 *  - Logs each reconnection attempt with backoff details
 *
 * Message routing:
 *  - Ticker  → onTick(symbol, price, volume24h)
 *  - Level2  → onBookUpdate(symbol, bids, asks)
 */

import WebSocket from 'ws';
import { log } from '../storage/database.js';

// ─── Types ────────────────────────────────────────────────────────────────────

type TickCallback     = (symbol: string, price: number, volume24h: number) => void;
type BookCallback     = (symbol: string, bids: { price: number; size: number }[], asks: { price: number; size: number }[]) => void;
type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';

interface CoinbaseTicker {
  type: 'ticker';
  product_id: string;
  price: string;
  volume_24h: string;
  best_bid: string;
  best_ask: string;
  time: string;
}

interface CoinbaseL2Update {
  type: 'l2update';
  product_id: string;
  changes: Array<['buy' | 'sell', string, string]>; // [side, price, size]
  time: string;
}

interface CoinbaseSubscriptionsMsg {
  type: 'subscriptions';
  channels: Array<{ name: string; product_ids: string[] }>;
}

type CoinbaseMessage = CoinbaseTicker | CoinbaseL2Update | CoinbaseSubscriptionsMsg | { type: string };

const WS_URL = 'wss://advanced-trade-ws.coinbase.com';
const MAX_BACKOFF_MS = 30_000;

// ─── In-memory order book ─────────────────────────────────────────────────────

const bookState = new Map<string, { bids: Map<string, number>; asks: Map<string, number> }>();

function getBook(productId: string) {
  if (!bookState.has(productId)) {
    bookState.set(productId, { bids: new Map(), asks: new Map() });
  }
  return bookState.get(productId)!;
}

function applyL2Update(update: CoinbaseL2Update, onBook: BookCallback) {
  const book = getBook(update.product_id);
  for (const [side, priceStr, sizeStr] of update.changes) {
    const price = parseFloat(priceStr);
    const size = parseFloat(sizeStr);
    const map = side === 'buy' ? book.bids : book.asks;
    if (size === 0) {
      map.delete(priceStr);
    } else {
      map.set(priceStr, size);
    }
  }

  // Emit sorted top-20 each side
  const symbol = update.product_id.replace('-USD', '');
  const bids = Array.from(book.bids.entries())
    .map(([p, s]) => ({ price: parseFloat(p), size: s }))
    .sort((a, b) => b.price - a.price)
    .slice(0, 20);
  const asks = Array.from(book.asks.entries())
    .map(([p, s]) => ({ price: parseFloat(p), size: s }))
    .sort((a, b) => a.price - b.price)
    .slice(0, 20);

  onBook(symbol, bids, asks);
}

// ─── Connection manager ───────────────────────────────────────────────────────

export class CoinbaseWebSocket {
  private ws: WebSocket | null = null;
  private productIds: string[] = [];
  private onTick: TickCallback;
  private onBook: BookCallback;
  private status: ConnectionStatus = 'disconnected';
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(productIds: string[], onTick: TickCallback, onBook: BookCallback) {
    this.productIds = productIds;
    this.onTick = onTick;
    this.onBook = onBook;
  }

  connect(): void {
    if (this.status === 'connected' || this.status === 'connecting') return;
    this.status = 'connecting';
    log('info', `Coinbase WS connecting (attempt ${this.reconnectAttempts + 1})...`);

    const ws = new WebSocket(WS_URL);
    this.ws = ws;

    ws.on('open', () => {
      this.status = 'connected';
      this.reconnectAttempts = 0;
      log('info', `Coinbase WS connected — subscribing to ${this.productIds.length} products`);
      this.subscribe();
    });

    ws.on('message', (raw: Buffer) => {
      try {
        const msg = JSON.parse(raw.toString()) as CoinbaseMessage;
        this.handleMessage(msg);
      } catch { /* malformed JSON — ignore */ }
    });

    ws.on('error', (err) => {
      log('warn', `Coinbase WS error: ${err.message}`);
    });

    ws.on('close', () => {
      this.status = 'disconnected';
      log('warn', 'Coinbase WS disconnected — scheduling reconnect');
      this.scheduleReconnect();
    });
  }

  private subscribe(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({
      type: 'subscribe',
      product_ids: this.productIds,
      channels: ['ticker', 'level2'],
    }));
  }

  private handleMessage(msg: CoinbaseMessage): void {
    switch (msg.type) {
      case 'ticker': {
        const tick = msg as CoinbaseTicker;
        const symbol = tick.product_id.replace('-USD', '');
        const price = parseFloat(tick.price);
        const volume = parseFloat(tick.volume_24h);
        if (!isNaN(price) && price > 0) {
          this.onTick(symbol, price, volume);
        }
        break;
      }
      case 'l2update': {
        applyL2Update(msg as CoinbaseL2Update, this.onBook);
        break;
      }
      case 'subscriptions': {
        log('info', 'Coinbase WS subscriptions confirmed');
        break;
      }
      // Ignore heartbeat, snapshot, etc.
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    const backoffMs = Math.min(MAX_BACKOFF_MS, 1_000 * Math.pow(2, this.reconnectAttempts));
    this.reconnectAttempts++;
    log('info', `Coinbase WS reconnecting in ${backoffMs}ms (attempt ${this.reconnectAttempts})`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, backoffMs);
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.status = 'disconnected';
  }

  isConnected(): boolean {
    return this.status === 'connected';
  }

  updateProducts(productIds: string[]): void {
    this.productIds = productIds;
    if (this.status === 'connected') this.subscribe();
  }
}
