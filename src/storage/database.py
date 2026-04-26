"""Storage layer — delegates all operations to Convex.

This module provides the public API that the rest of the codebase imports.
All state is stored in Convex. There is no local database.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from src.types import Position, Trade, LogEntry, TradeDiagnosis
from src.storage.convex_client import ConvexStorage

_storage: Optional[ConvexStorage] = None
_stdout_logger = logging.getLogger("kaizen.fallback")
if not _stdout_logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _stdout_logger.addHandler(h)
    _stdout_logger.setLevel(logging.INFO)
    _stdout_logger.propagate = False


def init(convex_url: str) -> None:
    """Initialize the Convex storage backend. Must be called at startup."""
    global _storage
    _storage = ConvexStorage(convex_url)
    _storage.start()


def close() -> None:
    """Flush pending writes and shut down."""
    global _storage
    if _storage is not None:
        _storage.close()
        _storage = None


def _get() -> ConvexStorage:
    """Get the storage instance, raising if not initialized."""
    if _storage is None:
        raise RuntimeError(
            "Database not initialized. Call database.init(convex_url) at startup. "
            "Set CONVEX_URL in your .env file."
        )
    return _storage


# ─── Context manager for batch writes ─────────────────────────────────────
# Convex mutations are individually atomic. This is kept for API compatibility
# but is a no-op — each write is independently queued and flushed.

@contextmanager
def batch_writes():
    """Context manager for grouped writes (no-op with Convex — each write is atomic)."""
    yield


# ─── Write operations ─────────────────────────────────────────────────────

def insert_position(p: Position) -> None:
    _get().insert_position(p)


def update_position_price(position_id: str, current_price: float,
                          high_watermark: float, low_watermark: float,
                          stop_price: float, quantity: float | None = None) -> None:
    _get().update_position_price(position_id, current_price, high_watermark,
                                  low_watermark, stop_price, quantity)


def update_position_close(id: str, exit_price: float, pnl_usd: float,
                          pnl_pct: float, exit_reason: str) -> None:
    _get().update_position_close(id, exit_price, pnl_usd, pnl_pct, exit_reason)


def insert_trade(t: Trade) -> None:
    _get().insert_trade(t)


def log(level: str, message: str, symbol: str | None = None,
        strategy: str | None = None, data: dict | None = None) -> None:
    """Log to Convex when initialized; fall back to stdout otherwise.

    Logging is best-effort — never let a logging failure crash the trading
    process. Constructor-time logs (e.g. OKXProvider's exchange-info load)
    can fire before database.init() runs in some test/dev paths.
    """
    if _storage is not None:
        try:
            _storage.log(level, message, symbol=symbol, strategy=strategy, data=data)
            return
        except Exception:
            pass  # fall through to stdout
    ts = datetime.now(timezone.utc).isoformat()
    sym = f" [{symbol}]" if symbol else ""
    _stdout_logger.info("[%s] [%s]%s %s", ts, level.upper(), sym, message)


def insert_diagnosis(d: TradeDiagnosis) -> None:
    _get().insert_diagnosis(d)


def snapshot_config(config: object, reason: str) -> None:
    _get().snapshot_config(config, reason)


def insert_trade_journal(entry: dict) -> None:
    _get().insert_trade_journal(entry)


def close_orphaned_positions(exit_reason: str = "orphaned_restart") -> dict:
    return _get().close_orphaned_positions(exit_reason)


# ─── Read operations ──────────────────────────────────────────────────────

def get_open_positions() -> list[Position]:
    return _get().get_open_positions()


def get_closed_trades(limit: int = 200) -> list[Position]:
    return _get().get_closed_trades(limit)


def get_recent_logs(limit: int = 500, level: str | None = None) -> list[LogEntry]:
    return _get().get_recent_logs(limit, level)


def get_recent_diagnoses(limit: int = 50) -> list[TradeDiagnosis]:
    return _get().get_recent_diagnoses(limit)


def get_trade_journal(limit: int = 50) -> list[dict]:
    return _get().get_trade_journal(limit)
