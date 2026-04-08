"""SQLite storage layer. All tables are append-only for immutable audit trail."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from src.types import Position, Trade, LogEntry, TradeDiagnosis

if TYPE_CHECKING:
    from src.storage.backend import StorageBackend

DB_PATH = os.environ.get("DB_PATH", "trader.db")

_conn: Optional[sqlite3.Connection] = None
_write_lock = threading.Lock()

# Optional dual-write backend (SQLite + Convex). Set via init_dual_write().
_backend: Optional[StorageBackend] = None

# Per-thread batch flag — prevents concurrent batch_writes() from interfering.
_tls = threading.local()


@contextmanager
def batch_writes():
    """Context manager that defers commits until the block exits.

    Usage:
        with batch_writes():
            insert_position(p1)
            insert_position(p2)
            insert_trade(t1)
        # single commit happens here
    """
    _tls.batch_active = True
    try:
        yield
    except Exception:
        db().rollback()
        raise
    finally:
        _tls.batch_active = False
    with _write_lock:
        db().commit()


def _auto_commit() -> None:
    """Commit unless we are inside a batch_writes() block."""
    if not getattr(_tls, "batch_active", False):
        db().commit()


def db() -> sqlite3.Connection:
    global _conn
    if _conn:
        return _conn
    try:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute("PRAGMA foreign_keys = ON")
        _migrate(_conn)
    except sqlite3.DatabaseError as exc:
        _conn = None
        raise RuntimeError(
            f"Failed to open database at {DB_PATH!r}: {exc}. "
            "The file may be corrupted or the disk may be full."
        ) from exc
    except OSError as exc:
        _conn = None
        raise RuntimeError(
            f"OS error opening database at {DB_PATH!r}: {exc}. "
            "Check disk space and file permissions."
        ) from exc
    return _conn


def close() -> None:
    """Close the database connection for graceful shutdown."""
    global _conn, _backend
    if _backend is not None:
        try:
            # If the primary backend has a close/stop method, call it
            primary = getattr(_backend, 'primary', None)
            if primary and hasattr(primary, 'close'):
                primary.close()
        except Exception:
            pass
        _backend = None
    if _conn:
        try:
            _conn.close()
        finally:
            _conn = None


class _SQLiteBackend:
    """Wraps this module's SQLite functions as a StorageBackend for DualWriteBackend."""

    def insert_position(self, p: Position) -> None:
        _sqlite_insert_position(p)

    def update_position_close(self, id: str, exit_price: float, pnl_usd: float,
                              pnl_pct: float, exit_reason: str) -> None:
        _sqlite_update_position_close(id, exit_price, pnl_usd, pnl_pct, exit_reason)

    def insert_trade(self, t: Trade) -> None:
        _sqlite_insert_trade(t)

    def log(self, level: str, message: str, symbol: str | None = None,
            strategy: str | None = None, data: dict | None = None) -> None:
        _sqlite_log(level, message, symbol=symbol, strategy=strategy, data=data)

    def insert_diagnosis(self, d: TradeDiagnosis) -> None:
        _sqlite_insert_diagnosis(d)

    def snapshot_config(self, config: object, reason: str) -> None:
        _sqlite_snapshot_config(config, reason)


def init_dual_write(convex_url: str) -> None:
    """Initialize dual-write backend (SQLite + Convex). Opt-in via CONVEX_URL."""
    from src.storage.convex_client import ConvexStorage
    from src.storage.backend import DualWriteBackend

    global _backend
    convex = ConvexStorage(convex_url)
    convex.start()
    _backend = DualWriteBackend(primary=_SQLiteBackend(), fallback=convex)


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

        CREATE TABLE IF NOT EXISTS trade_journal (
            id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            r_multiple REAL,
            hold_hours REAL,
            mae_pct REAL,
            mfe_pct REAL,
            partial_exit_pct REAL,
            exit_reason TEXT,
            pnl_pct REAL,
            regime_at_entry TEXT,
            regime_at_exit TEXT,
            was_partial_beneficial INTEGER,
            timestamp REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);
        CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
        CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
        CREATE INDEX IF NOT EXISTS idx_logs_symbol ON logs(symbol);
    """)


# ─── Positions ─────────────────────────────────────────────────────────────

def _sqlite_insert_position(p: Position) -> None:
    with _write_lock:
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
        _auto_commit()


def insert_position(p: Position) -> None:
    if _backend is not None:
        _backend.insert_position(p)
    else:
        _sqlite_insert_position(p)


def _sqlite_update_position_close(id: str, exit_price: float, pnl_usd: float,
                                   pnl_pct: float, exit_reason: str) -> None:
    now = int(time.time() * 1000)
    with _write_lock:
        db().execute(
            """UPDATE positions SET
                status='closed', exit_price=?, closed_at=?, pnl_usd=?, pnl_pct=?, exit_reason=?
            WHERE id=?""",
            (exit_price, now, pnl_usd, pnl_pct, exit_reason, id),
        )
        _auto_commit()


def update_position_close(id: str, exit_price: float, pnl_usd: float,
                          pnl_pct: float, exit_reason: str) -> None:
    if _backend is not None:
        _backend.update_position_close(id, exit_price, pnl_usd, pnl_pct, exit_reason)
    else:
        _sqlite_update_position_close(id, exit_price, pnl_usd, pnl_pct, exit_reason)


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

def _sqlite_insert_trade(t: Trade) -> None:
    with _write_lock:
        db().execute(
            """INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.id, t.position_id, t.side, t.symbol, t.quantity, t.size_usd,
             t.price, t.order_id, t.status, t.error,
             1 if t.paper_trading else 0, t.placed_at),
        )
        _auto_commit()


def insert_trade(t: Trade) -> None:
    if _backend is not None:
        _backend.insert_trade(t)
    else:
        _sqlite_insert_trade(t)


# ─── Logs ──────────────────────────────────────────────────────────────────

def _sqlite_log(level: str, message: str, symbol: str | None = None,
                strategy: str | None = None, data: dict | None = None) -> None:
    now = int(time.time() * 1000)
    entry_id = str(uuid.uuid4())
    with _write_lock:
        db().execute(
            "INSERT INTO logs VALUES (?,?,?,?,?,?,?)",
            (entry_id, level, message, symbol, strategy,
             json.dumps(data) if data else None, now),
        )
        _auto_commit()

    ts_str = datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat()
    sym_tag = f" [{symbol}]" if symbol else ""
    print(f"[{ts_str}] [{level.upper()}]{sym_tag} {message}")


def log(level: str, message: str, symbol: str | None = None,
        strategy: str | None = None, data: dict | None = None) -> None:
    if _backend is not None:
        _backend.log(level, message, symbol=symbol, strategy=strategy, data=data)
    else:
        _sqlite_log(level, message, symbol=symbol, strategy=strategy, data=data)


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

def _sqlite_insert_diagnosis(d: TradeDiagnosis) -> None:
    with _write_lock:
        db().execute(
            """INSERT INTO diagnoses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), d.position_id, d.symbol, d.strategy, d.pnl_pct,
             d.hold_ms, d.exit_reason, d.loss_reason, d.entry_qual_score,
             d.market_phase_at_entry, d.action, json.dumps(d.parameter_changes),
             d.timestamp),
        )
        _auto_commit()


def insert_diagnosis(d: TradeDiagnosis) -> None:
    if _backend is not None:
        _backend.insert_diagnosis(d)
    else:
        _sqlite_insert_diagnosis(d)


def get_recent_diagnoses(limit: int = 50) -> list[TradeDiagnosis]:
    rows = db().execute(
        "SELECT * FROM diagnoses ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_diagnosis(r) for r in rows]


# ─── Config snapshots ──────────────────────────────────────────────────────

def _sqlite_snapshot_config(config: object, reason: str) -> None:
    import dataclasses
    config_dict = dataclasses.asdict(config) if dataclasses.is_dataclass(config) else config
    with _write_lock:
        db().execute(
            "INSERT INTO scanner_config_history VALUES (?,?,?,?)",
            (str(uuid.uuid4()), json.dumps(config_dict), reason, int(time.time() * 1000)),
        )
        _auto_commit()


def snapshot_config(config: object, reason: str) -> None:
    if _backend is not None:
        _backend.snapshot_config(config, reason)
    else:
        _sqlite_snapshot_config(config, reason)


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
    raw_data = r["data"]
    parsed_data = None
    if raw_data:
        try:
            parsed_data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            parsed_data = None
    return LogEntry(
        id=r["id"], level=r["level"], message=r["message"],
        symbol=r["symbol"], strategy=r["strategy"],
        data=parsed_data,
        ts=r["ts"],
    )


# ─── Trade Journal ────────────────────────────────────────────────────────

def insert_trade_journal(entry: dict) -> None:
    """Insert a structured trade journal entry."""
    with _write_lock:
        db().execute(
            """INSERT INTO trade_journal (id, position_id, symbol, strategy, r_multiple,
               hold_hours, mae_pct, mfe_pct, partial_exit_pct, exit_reason, pnl_pct,
               regime_at_entry, regime_at_exit, was_partial_beneficial, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry["id"], entry["position_id"], entry["symbol"], entry["strategy"],
             entry.get("r_multiple"), entry.get("hold_hours"), entry.get("mae_pct"),
             entry.get("mfe_pct"), entry.get("partial_exit_pct"), entry.get("exit_reason"),
             entry.get("pnl_pct"), entry.get("regime_at_entry"), entry.get("regime_at_exit"),
             entry.get("was_partial_beneficial"), entry["timestamp"]),
        )
        _auto_commit()


def get_trade_journal(limit: int = 50) -> list[dict]:
    """Get recent trade journal entries."""
    rows = db().execute(
        "SELECT * FROM trade_journal ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = ["id", "position_id", "symbol", "strategy", "r_multiple", "hold_hours",
            "mae_pct", "mfe_pct", "partial_exit_pct", "exit_reason", "pnl_pct",
            "regime_at_entry", "regime_at_exit", "was_partial_beneficial", "timestamp"]
    return [dict(zip(cols, row)) for row in rows]


# ─── Row converters ────────────────────────────────────────────────────────

def _row_to_diagnosis(r: sqlite3.Row) -> TradeDiagnosis:
    raw_changes = r["parameter_changes"]
    try:
        param_changes = json.loads(raw_changes) if raw_changes else {}
    except (json.JSONDecodeError, TypeError):
        param_changes = {}
    return TradeDiagnosis(
        position_id=r["position_id"], symbol=r["symbol"],
        strategy=r["strategy"], pnl_pct=r["pnl_pct"],
        hold_ms=r["hold_ms"], exit_reason=r["exit_reason"],
        loss_reason=r["loss_reason"], entry_qual_score=r["entry_qual_score"],
        market_phase_at_entry=r["market_phase_at_entry"], action=r["action"],
        parameter_changes=param_changes,
        timestamp=r["timestamp"],
    )
