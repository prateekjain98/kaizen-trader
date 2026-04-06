/**
 * Simple SQLite-backed storage layer.
 * All tables are append-only — we never UPDATE rows, only INSERT + query.
 * This makes the log history immutable and safe to ship to Claude for analysis.
 */
import Database from 'better-sqlite3';
import { randomUUID } from 'crypto';
import type { Position, Trade, LogEntry, TradeDiagnosis, LogLevel } from '../types.js';

const DB_PATH = process.env['DB_PATH'] ?? 'trader.db';

let _db: Database.Database | null = null;

export function db(): Database.Database {
  if (_db) return _db;
  _db = new Database(DB_PATH);
  _db.pragma('journal_mode = WAL');
  _db.pragma('foreign_keys = ON');
  migrate(_db);
  return _db;
}

function migrate(d: Database.Database): void {
  d.exec(`
    CREATE TABLE IF NOT EXISTS positions (
      id TEXT PRIMARY KEY,
      symbol TEXT NOT NULL,
      product_id TEXT NOT NULL,
      strategy TEXT NOT NULL,
      side TEXT NOT NULL,
      tier TEXT NOT NULL,
      entry_price REAL NOT NULL,
      quantity REAL NOT NULL,
      size_usd REAL NOT NULL,
      opened_at INTEGER NOT NULL,
      high_watermark REAL NOT NULL,
      low_watermark REAL NOT NULL,
      current_price REAL NOT NULL,
      trail_pct REAL NOT NULL,
      stop_price REAL NOT NULL,
      max_hold_ms INTEGER NOT NULL,
      qual_score REAL NOT NULL,
      signal_id TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      exit_price REAL,
      closed_at INTEGER,
      pnl_usd REAL,
      pnl_pct REAL,
      exit_reason TEXT,
      paper_trading INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS trades (
      id TEXT PRIMARY KEY,
      position_id TEXT NOT NULL,
      side TEXT NOT NULL,
      symbol TEXT NOT NULL,
      quantity REAL NOT NULL,
      size_usd REAL NOT NULL,
      price REAL NOT NULL,
      order_id TEXT,
      status TEXT NOT NULL,
      error TEXT,
      paper_trading INTEGER NOT NULL DEFAULT 1,
      placed_at INTEGER NOT NULL,
      FOREIGN KEY (position_id) REFERENCES positions(id)
    );

    CREATE TABLE IF NOT EXISTS logs (
      id TEXT PRIMARY KEY,
      level TEXT NOT NULL,
      message TEXT NOT NULL,
      symbol TEXT,
      strategy TEXT,
      data TEXT,
      ts INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS diagnoses (
      id TEXT PRIMARY KEY,
      position_id TEXT NOT NULL,
      symbol TEXT NOT NULL,
      strategy TEXT NOT NULL,
      pnl_pct REAL NOT NULL,
      hold_ms INTEGER NOT NULL,
      exit_reason TEXT NOT NULL,
      loss_reason TEXT NOT NULL,
      entry_qual_score REAL NOT NULL,
      market_phase_at_entry TEXT NOT NULL,
      action TEXT NOT NULL,
      parameter_changes TEXT NOT NULL,
      timestamp INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS scanner_config_history (
      id TEXT PRIMARY KEY,
      config TEXT NOT NULL,
      reason TEXT NOT NULL,
      timestamp INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
    CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
    CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);
    CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
    CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
    CREATE INDEX IF NOT EXISTS idx_logs_symbol ON logs(symbol);
  `);
}

// ─── Positions ─────────────────────────────────────────────────────────────

export function insertPosition(p: Position): void {
  db().prepare(`
    INSERT INTO positions VALUES (
      @id, @symbol, @productId, @strategy, @side, @tier,
      @entryPrice, @quantity, @sizeUsd, @openedAt,
      @highWatermark, @lowWatermark, @currentPrice, @trailPct, @stopPrice, @maxHoldMs,
      @qualScore, @signalId, @status,
      @exitPrice, @closedAt, @pnlUsd, @pnlPct, @exitReason, @paperTrading
    )
  `).run({
    ...p,
    paperTrading: p.paperTrading ? 1 : 0,
    exitPrice: p.exitPrice ?? null,
    closedAt: p.closedAt ?? null,
    pnlUsd: p.pnlUsd ?? null,
    pnlPct: p.pnlPct ?? null,
    exitReason: p.exitReason ?? null,
  });
}

export function updatePositionClose(
  id: string,
  exitPrice: number,
  pnlUsd: number,
  pnlPct: number,
  exitReason: string,
): void {
  db().prepare(`
    UPDATE positions SET
      status='closed', exit_price=?, closed_at=?, pnl_usd=?, pnl_pct=?, exit_reason=?
    WHERE id=?
  `).run(exitPrice, Date.now(), pnlUsd, pnlPct, exitReason, id);
}

export function getOpenPositions(): Position[] {
  return (db().prepare(`SELECT * FROM positions WHERE status='open' OR status='closing'`).all() as RawPosition[])
    .map(rowToPosition);
}

export function getClosedTrades(limit = 200): Position[] {
  return (db().prepare(`SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?`).all(limit) as RawPosition[])
    .map(rowToPosition);
}

// ─── Trades ────────────────────────────────────────────────────────────────

export function insertTrade(t: Trade): void {
  db().prepare(`
    INSERT INTO trades VALUES (
      @id, @positionId, @side, @symbol, @quantity, @sizeUsd, @price,
      @orderId, @status, @error, @paperTrading, @placedAt
    )
  `).run({ ...t, paperTrading: t.paperTrading ? 1 : 0, orderId: t.orderId ?? null, error: t.error ?? null });
}

// ─── Logs ──────────────────────────────────────────────────────────────────

export function log(level: LogLevel, message: string, opts?: {
  symbol?: string;
  strategy?: string;
  data?: Record<string, unknown>;
}): void {
  const entry: LogEntry = {
    id: randomUUID(),
    level,
    message,
    symbol: opts?.symbol,
    strategy: opts?.strategy,
    data: opts?.data,
    ts: Date.now(),
  };
  db().prepare(`
    INSERT INTO logs VALUES (@id, @level, @message, @symbol, @strategy, @data, @ts)
  `).run({
    ...entry,
    symbol: entry.symbol ?? null,
    strategy: entry.strategy ?? null,
    data: entry.data ? JSON.stringify(entry.data) : null,
  });

  const prefix = `[${new Date(entry.ts).toISOString()}] [${level.toUpperCase()}]`;
  const suffix = entry.symbol ? ` [${entry.symbol}]` : '';
  console.log(`${prefix}${suffix} ${message}`);
}

export function getRecentLogs(limit = 500, level?: LogLevel): LogEntry[] {
  const rows = level
    ? db().prepare(`SELECT * FROM logs WHERE level=? ORDER BY ts DESC LIMIT ?`).all(level, limit)
    : db().prepare(`SELECT * FROM logs ORDER BY ts DESC LIMIT ?`).all(limit);
  return (rows as RawLog[]).map(rowToLog);
}

// ─── Diagnoses ─────────────────────────────────────────────────────────────

export function insertDiagnosis(d: TradeDiagnosis): void {
  db().prepare(`
    INSERT INTO diagnoses VALUES (
      @id, @positionId, @symbol, @strategy, @pnlPct, @holdMs, @exitReason,
      @lossReason, @entryQualScore, @marketPhaseAtEntry, @action, @parameterChanges, @timestamp
    )
  `).run({
    id: randomUUID(),
    ...d,
    parameterChanges: JSON.stringify(d.parameterChanges),
  });
}

export function getRecentDiagnoses(limit = 50): TradeDiagnosis[] {
  return (db().prepare(`SELECT * FROM diagnoses ORDER BY timestamp DESC LIMIT ?`).all(limit) as RawDiagnosis[])
    .map(rowToDiagnosis);
}

// ─── Config snapshots ──────────────────────────────────────────────────────

export function snapshotConfig(config: object, reason: string): void {
  db().prepare(`INSERT INTO scanner_config_history VALUES (?, ?, ?, ?)`).run(
    randomUUID(), JSON.stringify(config), reason, Date.now()
  );
}

// ─── Raw row types ─────────────────────────────────────────────────────────

interface RawPosition {
  id: string; symbol: string; product_id: string; strategy: string;
  side: string; tier: string; entry_price: number; quantity: number;
  size_usd: number; opened_at: number; high_watermark: number;
  low_watermark: number; current_price: number; trail_pct: number;
  stop_price: number; max_hold_ms: number; qual_score: number;
  signal_id: string; status: string; exit_price: number | null;
  closed_at: number | null; pnl_usd: number | null; pnl_pct: number | null;
  exit_reason: string | null; paper_trading: number;
}

interface RawLog {
  id: string; level: string; message: string; symbol: string | null;
  strategy: string | null; data: string | null; ts: number;
}

interface RawDiagnosis {
  id: string; position_id: string; symbol: string; strategy: string;
  pnl_pct: number; hold_ms: number; exit_reason: string; loss_reason: string;
  entry_qual_score: number; market_phase_at_entry: string; action: string;
  parameter_changes: string; timestamp: number;
}

function rowToPosition(r: RawPosition): Position {
  return {
    id: r.id, symbol: r.symbol, productId: r.product_id,
    strategy: r.strategy as Position['strategy'],
    side: r.side as Position['side'], tier: r.tier as Position['tier'],
    entryPrice: r.entry_price, quantity: r.quantity, sizeUsd: r.size_usd,
    openedAt: r.opened_at, highWatermark: r.high_watermark,
    lowWatermark: r.low_watermark, currentPrice: r.current_price,
    trailPct: r.trail_pct, stopPrice: r.stop_price, maxHoldMs: r.max_hold_ms,
    qualScore: r.qual_score, signalId: r.signal_id,
    status: r.status as Position['status'],
    exitPrice: r.exit_price ?? undefined,
    closedAt: r.closed_at ?? undefined,
    pnlUsd: r.pnl_usd ?? undefined,
    pnlPct: r.pnl_pct ?? undefined,
    exitReason: r.exit_reason as Position['exitReason'] ?? undefined,
    paperTrading: r.paper_trading === 1,
  };
}

function rowToLog(r: RawLog): LogEntry {
  return {
    id: r.id, level: r.level as LogLevel, message: r.message,
    symbol: r.symbol ?? undefined, strategy: r.strategy ?? undefined,
    data: r.data ? (JSON.parse(r.data) as Record<string, unknown>) : undefined,
    ts: r.ts,
  };
}

function rowToDiagnosis(r: RawDiagnosis): TradeDiagnosis {
  return {
    positionId: r.position_id, symbol: r.symbol,
    strategy: r.strategy as TradeDiagnosis['strategy'],
    pnlPct: r.pnl_pct, holdMs: r.hold_ms,
    exitReason: r.exit_reason as TradeDiagnosis['exitReason'],
    lossReason: r.loss_reason as TradeDiagnosis['lossReason'],
    entryQualScore: r.entry_qual_score,
    marketPhaseAtEntry: r.market_phase_at_entry as TradeDiagnosis['marketPhaseAtEntry'],
    action: r.action,
    parameterChanges: JSON.parse(r.parameter_changes) as Partial<import('../types.js').ScannerConfig>,
    timestamp: r.timestamp,
  };
}
