"""Convex Python SDK wrapper with background flush queue."""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.types import Position, Trade, LogEntry, TradeDiagnosis


class ConvexStorage:
    """Wraps the Convex Python SDK. Mirrors database.py function signatures.

    Writes are queued and flushed in a background thread every 1 second
    to avoid blocking the trading hot path. The queue is thread-safe.
    """

    def __init__(self, url: Optional[str] = None, client: Any = None):
        """Initialize ConvexStorage.

        Args:
            url: Convex deployment URL. Falls back to CONVEX_URL env var.
            client: Optional pre-built Convex client (useful for testing).
        """
        self.url = url or os.environ.get("CONVEX_URL", "")
        self._client = client
        self._queue: queue.Queue = queue.Queue()
        self._flush_thread: Optional[threading.Thread] = None
        self._running = False
        self._flush_interval = 1.0  # seconds

    def _get_client(self) -> Any:
        """Lazily initialize the Convex client."""
        if self._client is None:
            try:
                from convex import ConvexClient
                self._client = ConvexClient(self.url)
            except ImportError:
                raise RuntimeError(
                    "The 'convex' package is not installed. "
                    "Install it with: pip install convex"
                )
        return self._client

    def start(self) -> None:
        """Start the background flush thread."""
        if self._running:
            return
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="convex-flush"
        )
        self._flush_thread.start()

    def stop(self) -> None:
        """Flush remaining items and stop."""
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=10.0)
        # Final drain of any remaining items
        self._drain_queue()

    def _flush_loop(self) -> None:
        """Flush queued writes to Convex every flush_interval seconds."""
        while self._running:
            time.sleep(self._flush_interval)
            self._drain_queue()

    def _drain_queue(self) -> None:
        """Process all items currently in the queue."""
        items: list[tuple[str, dict]] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break

        client = self._get_client()
        for mutation_name, args in items:
            try:
                client.mutation(mutation_name, args)
            except Exception as exc:
                # Print error but don't crash the flush loop
                print(f"[CONVEX ERROR] Failed to call {mutation_name}: {exc}")

    def _enqueue(self, mutation_name: str, args: dict) -> None:
        """Add a mutation call to the flush queue."""
        self._queue.put((mutation_name, args))

    # ─── Write operations ─────────────────────────────────────────────────

    def insert_position(self, p: Position) -> None:
        """Queue a position insert."""
        self._enqueue("mutations:insertPosition", {
            "positionId": p.id,
            "symbol": p.symbol,
            "productId": p.product_id,
            "strategy": p.strategy,
            "side": p.side,
            "tier": p.tier,
            "entryPrice": p.entry_price,
            "quantity": p.quantity,
            "sizeUsd": p.size_usd,
            "openedAt": p.opened_at,
            "highWatermark": p.high_watermark,
            "lowWatermark": p.low_watermark,
            "currentPrice": p.current_price,
            "trailPct": p.trail_pct,
            "stopPrice": p.stop_price,
            "maxHoldMs": p.max_hold_ms,
            "qualScore": p.qual_score,
            "signalId": p.signal_id,
            "status": p.status,
            "exitPrice": p.exit_price,
            "closedAt": p.closed_at,
            "pnlUsd": p.pnl_usd,
            "pnlPct": p.pnl_pct,
            "exitReason": p.exit_reason,
            "paperTrading": p.paper_trading,
        })

    def update_position_close(self, id: str, exit_price: float, pnl_usd: float,
                              pnl_pct: float, exit_reason: str) -> None:
        """Queue a position close update."""
        now = int(time.time() * 1000)
        self._enqueue("mutations:updatePositionClose", {
            "positionId": id,
            "exitPrice": exit_price,
            "pnlUsd": pnl_usd,
            "pnlPct": pnl_pct,
            "exitReason": exit_reason,
            "closedAt": float(now),
        })

    def insert_trade(self, t: Trade) -> None:
        """Queue a trade insert."""
        self._enqueue("mutations:insertTrade", {
            "tradeId": t.id,
            "positionId": t.position_id,
            "side": t.side,
            "symbol": t.symbol,
            "quantity": t.quantity,
            "sizeUsd": t.size_usd,
            "price": t.price,
            "orderId": t.order_id,
            "status": t.status,
            "error": t.error,
            "paperTrading": t.paper_trading,
            "placedAt": t.placed_at,
        })

    def log(self, level: str, message: str, symbol: str | None = None,
            strategy: str | None = None, data: dict | None = None) -> None:
        """Queue a log entry insert."""
        now = int(time.time() * 1000)
        self._enqueue("mutations:insertLog", {
            "logId": str(uuid.uuid4()),
            "level": level,
            "message": message,
            "symbol": symbol,
            "strategy": strategy,
            "data": json.dumps(data) if data else None,
            "ts": float(now),
        })

        # Also print to stdout like the SQLite logger does
        ts_str = datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat()
        sym_tag = f" [{symbol}]" if symbol else ""
        print(f"[{ts_str}] [{level.upper()}]{sym_tag} {message}")

    def insert_diagnosis(self, d: TradeDiagnosis) -> None:
        """Queue a diagnosis insert."""
        self._enqueue("mutations:insertDiagnosis", {
            "positionId": d.position_id,
            "symbol": d.symbol,
            "strategy": d.strategy,
            "pnlPct": d.pnl_pct,
            "holdMs": d.hold_ms,
            "exitReason": d.exit_reason,
            "lossReason": d.loss_reason,
            "entryQualScore": d.entry_qual_score,
            "marketPhaseAtEntry": d.market_phase_at_entry,
            "action": d.action,
            "parameterChanges": json.dumps(d.parameter_changes),
            "timestamp": d.timestamp,
        })

    def snapshot_config(self, config: object, reason: str) -> None:
        """Queue a config snapshot."""
        config_dict = (
            dataclasses.asdict(config)
            if dataclasses.is_dataclass(config) and not isinstance(config, type)
            else config
        )
        self._enqueue("mutations:snapshotConfig", {
            "config": json.dumps(config_dict),
            "reason": reason,
            "timestamp": float(int(time.time() * 1000)),
        })

    def close(self) -> None:
        """Alias for stop() — flush and shut down."""
        self.stop()

    @property
    def pending_count(self) -> int:
        """Number of items waiting to be flushed. Useful for monitoring."""
        return self._queue.qsize()
