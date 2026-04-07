"""SQLite storage layer. All tables are append-only for immutable audit trail."""

import json
import os
import sqlite3
import uuid
import time
from datetime import datetime, timezone
from typing import Optional

from src.types import Position, Trade, LogEntry, TradeDiagnosis

DB_PATH = os.environ.get("DB_PATH", "trader.db")

_conn: Optional[sqlite3.Connection] = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn:
        return _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode = WAL")
    _conn.execute("PRAGMA foreign_keys = ON")
    _migrate(_conn)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
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
    """)


# ─── Positions ─────────────────────────────────────────────────────────────

def insert_position(p: Position) -> None:
    db().execute(
        """INSERT INTO positions VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )""",
        (p.id, p.symbol, p.product_id, p.strategy, p.side, p.tier,
         p.entry_price, p.quantity, p.size_usd, p.opened_at,
         p.high_watermark, p.low_watermark, p.current_price, p.trail_pct,
         p.stop_price, p.max_hold_ms, p.qual_score, p.signal_id, p.status,
         p.exit_price, p.closed_at, p.pnl_usd, p.pnl_pct, p.exit_reason,
         1 if p.paper_trading else 0),
    )
    db().commit()


def update_position_close(id: str, exit_price: float, pnl_usd: float,
                          pnl_pct: float, exit_reason: str) -> None:
    now = int(time.time() * 1000)
    db().execute(
        """UPDATE positions SET
            status='closed', exit_price=?, closed_at=?, pnl_usd=?, pnl_pct=?, exit_reason=?
        WHERE id=?""",
        (exit_price, now, pnl_usd, pnl_pct, exit_reason, id),
    )
    db().commit()


def get_open_positions() -> list[Position]:
    rows = db().execute(
        "SELECT * FROM positions WHERE status='open' OR status='closing'"
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def get_closed_trades(limit: int = 200) -> list[Position]:
    rows = db().execute(
        "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_position(r) for r in rows]


# ─── Trades ────────────────────────────────────────────────────────────────

def insert_trade(t: Trade) -> None:
    db().execute(
        """INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t.id, t.position_id, t.side, t.symbol, t.quantity, t.size_usd,
         t.price, t.order_id, t.status, t.error,
         1 if t.paper_trading else 0, t.placed_at),
    )
    db().commit()


# ─── Logs ──────────────────────────────────────────────────────────────────

def log(level: str, message: str, symbol: str | None = None,
        strategy: str | None = None, data: dict | None = None) -> None:
    now = int(time.time() * 1000)
    entry_id = str(uuid.uuid4())
    db().execute(
        "INSERT INTO logs VALUES (?,?,?,?,?,?,?)",
        (entry_id, level, message, symbol, strategy,
         json.dumps(data) if data else None, now),
    )
    db().commit()

    ts_str = datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat()
    sym_tag = f" [{symbol}]" if symbol else ""
    print(f"[{ts_str}] [{level.upper()}]{sym_tag} {message}")


def get_recent_logs(limit: int = 500, level: str | None = None) -> list[LogEntry]:
    if level:
        rows = db().execute(
            "SELECT * FROM logs WHERE level=? ORDER BY ts DESC LIMIT ?",
            (level, limit),
        ).fetchall()
    else:
        rows = db().execute(
            "SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_log(r) for r in rows]


# ─── Diagnoses ─────────────────────────────────────────────────────────────

def insert_diagnosis(d: TradeDiagnosis) -> None:
    db().execute(
        """INSERT INTO diagnoses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), d.position_id, d.symbol, d.strategy, d.pnl_pct,
         d.hold_ms, d.exit_reason, d.loss_reason, d.entry_qual_score,
         d.market_phase_at_entry, d.action, json.dumps(d.parameter_changes),
         d.timestamp),
    )
    db().commit()


def get_recent_diagnoses(limit: int = 50) -> list[TradeDiagnosis]:
    rows = db().execute(
        "SELECT * FROM diagnoses ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_diagnosis(r) for r in rows]


# ─── Config snapshots ──────────────────────────────────────────────────────

def snapshot_config(config: object, reason: str) -> None:
    import dataclasses
    config_dict = dataclasses.asdict(config) if dataclasses.is_dataclass(config) else config
    db().execute(
        "INSERT INTO scanner_config_history VALUES (?,?,?,?)",
        (str(uuid.uuid4()), json.dumps(config_dict), reason, int(time.time() * 1000)),
    )
    db().commit()


# ─── Row converters ────────────────────────────────────────────────────────

def _row_to_position(r: sqlite3.Row) -> Position:
    return Position(
        id=r["id"], symbol=r["symbol"], product_id=r["product_id"],
        strategy=r["strategy"], side=r["side"], tier=r["tier"],
        entry_price=r["entry_price"], quantity=r["quantity"],
        size_usd=r["size_usd"], opened_at=r["opened_at"],
        high_watermark=r["high_watermark"], low_watermark=r["low_watermark"],
        current_price=r["current_price"], trail_pct=r["trail_pct"],
        stop_price=r["stop_price"], max_hold_ms=r["max_hold_ms"],
        qual_score=r["qual_score"], signal_id=r["signal_id"],
        status=r["status"], exit_price=r["exit_price"],
        closed_at=r["closed_at"], pnl_usd=r["pnl_usd"],
        pnl_pct=r["pnl_pct"], exit_reason=r["exit_reason"],
        paper_trading=bool(r["paper_trading"]),
    )


def _row_to_log(r: sqlite3.Row) -> LogEntry:
    return LogEntry(
        id=r["id"], level=r["level"], message=r["message"],
        symbol=r["symbol"], strategy=r["strategy"],
        data=json.loads(r["data"]) if r["data"] else None,
        ts=r["ts"],
    )


def _row_to_diagnosis(r: sqlite3.Row) -> TradeDiagnosis:
    return TradeDiagnosis(
        position_id=r["position_id"], symbol=r["symbol"],
        strategy=r["strategy"], pnl_pct=r["pnl_pct"],
        hold_ms=r["hold_ms"], exit_reason=r["exit_reason"],
        loss_reason=r["loss_reason"], entry_qual_score=r["entry_qual_score"],
        market_phase_at_entry=r["market_phase_at_entry"], action=r["action"],
        parameter_changes=json.loads(r["parameter_changes"]),
        timestamp=r["timestamp"],
    )
