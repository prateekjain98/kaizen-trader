"""StorageBackend protocol and DualWriteBackend for Convex + SQLite."""

from __future__ import annotations

import traceback
from typing import Protocol, Optional, runtime_checkable

from src.types import Position, Trade, TradeDiagnosis


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that both SQLite and Convex storage backends implement."""

    def insert_position(self, p: Position) -> None: ...

    def update_position_close(self, id: str, exit_price: float, pnl_usd: float,
                              pnl_pct: float, exit_reason: str) -> None: ...

    def insert_trade(self, t: Trade) -> None: ...

    def log(self, level: str, message: str, symbol: str | None = None,
            strategy: str | None = None, data: dict | None = None) -> None: ...

    def insert_diagnosis(self, d: TradeDiagnosis) -> None: ...

    def snapshot_config(self, config: object, reason: str) -> None: ...


class DualWriteBackend:
    """Writes to Convex (primary) + SQLite (fallback).

    If the primary write fails, the fallback still executes so no data is lost.
    Errors from the primary are logged but do not propagate.
    """

    def __init__(self, primary: StorageBackend, fallback: StorageBackend):
        self.primary = primary
        self.fallback = fallback

    def _write_both(self, method_name: str, *args, **kwargs) -> None:
        """Call method on both backends; primary errors are caught."""
        # Always write to fallback (SQLite) first for safety
        getattr(self.fallback, method_name)(*args, **kwargs)
        try:
            getattr(self.primary, method_name)(*args, **kwargs)
        except Exception:
            # Log primary failure but don't propagate — fallback already persisted
            traceback.print_exc()

    def insert_position(self, p: Position) -> None:
        self._write_both("insert_position", p)

    def update_position_close(self, id: str, exit_price: float, pnl_usd: float,
                              pnl_pct: float, exit_reason: str) -> None:
        self._write_both("update_position_close", id, exit_price, pnl_usd,
                         pnl_pct, exit_reason)

    def insert_trade(self, t: Trade) -> None:
        self._write_both("insert_trade", t)

    def log(self, level: str, message: str, symbol: str | None = None,
            strategy: str | None = None, data: dict | None = None) -> None:
        self._write_both("log", level, message, symbol=symbol,
                         strategy=strategy, data=data)

    def insert_diagnosis(self, d: TradeDiagnosis) -> None:
        self._write_both("insert_diagnosis", d)

    def snapshot_config(self, config: object, reason: str) -> None:
        self._write_both("snapshot_config", config, reason)
