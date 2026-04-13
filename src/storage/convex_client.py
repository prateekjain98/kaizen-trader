"""Convex storage client — the single database backend.

Writes are queued and flushed in a background thread every 1 second
to avoid blocking the trading hot path. Reads are synchronous.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import threading
import time
import uuid

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from typing import Any, Optional

from src.types import Position, Trade, LogEntry, TradeDiagnosis


class ConvexStorage:
    """Wraps the Convex Python SDK.

    Writes are async (queued and flushed in background).
    Reads are sync (blocking call to Convex query).
    """

    def __init__(self, url: Optional[str] = None, client: Any = None):
        self.url = url or os.environ.get("CONVEX_URL", "")
        self._client = client
        self._queue: queue.Queue = queue.Queue()
        self._flush_thread: Optional[threading.Thread] = None
        self._running = False
        self._flush_interval = 1.0
        # Source paper_trading from the canonical config.env to avoid divergence
        from src.config import env as _env
        self._paper_trading = _env.paper_trading

    def _get_client(self) -> Any:
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
        if self._running:
            return
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="convex-flush"
        )
        self._flush_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=10.0)
        self._drain_queue()

    def close(self) -> None:
        self.stop()

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(self._flush_interval)
            self._drain_queue()

    # Mutations that must not be silently dropped
    _CRITICAL_MUTATIONS = frozenset({
        "mutations:insertPosition",
        "mutations:updatePositionClose",
        "mutations:insertTrade",
    })

    def _drain_queue(self) -> None:
        items: list[tuple[str, dict]] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if not items:
            return

        client = self._get_client()
        for mutation_name, args in items:
            retries = 3 if mutation_name in self._CRITICAL_MUTATIONS else 1
            for attempt in range(retries):
                try:
                    client.mutation(mutation_name, args)
                    break
                except Exception as exc:
                    if attempt < retries - 1:
                        time.sleep(0.5 * (attempt + 1))
                    else:
                        logger.error(f"Failed to call {mutation_name} "
                                     f"after {retries} attempt(s): {exc}")
                        # Dead-letter queue: persist failed critical mutations to disk
                        if mutation_name in self._CRITICAL_MUTATIONS:
                            self._write_dead_letter(mutation_name, args, str(exc))

    def _write_dead_letter(self, mutation_name: str, args: dict, error: str) -> None:
        """Persist failed critical mutations to a local file for recovery."""
        try:
            from pathlib import Path
            dead_letter_path = Path(__file__).resolve().parents[2] / ".dead_letters.jsonl"
            # Size cap: skip writes if file exceeds 50 MB to prevent disk exhaustion
            if dead_letter_path.exists() and dead_letter_path.stat().st_size > 50 * 1024 * 1024:
                logger.warning("Dead letter file exceeds 50MB, skipping write")
                return
            entry = {
                "mutation": mutation_name,
                "args": args,
                "error": error,
                "timestamp": time.time(),
            }
            with open(dead_letter_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as write_err:
            logger.error(f"Dead letter write also failed: {write_err}")

    @staticmethod
    def _strip_none(d: dict) -> dict:
        """Remove keys with None values — Convex rejects null for optional fields."""
        return {k: v for k, v in d.items() if v is not None}

    def _enqueue(self, mutation_name: str, args: dict) -> None:
        self._queue.put((mutation_name, self._strip_none(args)))

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    # ─── Write operations (async, queued) ──────────────────────────────────

    def insert_position(self, p: Position) -> None:
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
            # New fields for P&L and risk tracking
            "maePct": p.mae_pct,
            "mfePct": p.mfe_pct,
            "partialExitPct": p.partial_exit_pct,
            "trancheCount": p.tranche_count,
            "avgEntryPrice": p.avg_entry_price,
            "originalQuantity": p.original_quantity,
            "entrySizeUsd": p.entry_size_usd,
            "totalCommission": p.total_commission,
            "initialStopPrice": p.initial_stop_price,
        })

    def update_position_price(self, position_id: str, current_price: float,
                              high_watermark: float, low_watermark: float,
                              stop_price: float, quantity: float | None = None) -> None:
        args: dict[str, Any] = {
            "positionId": position_id,
            "currentPrice": current_price,
            "highWatermark": high_watermark,
            "lowWatermark": low_watermark,
            "stopPrice": stop_price,
        }
        if quantity is not None:
            args["quantity"] = quantity
        self._enqueue("mutations:updatePositionPrice", args)

    def update_position_close(self, id: str, exit_price: float, pnl_usd: float,
                              pnl_pct: float, exit_reason: str) -> None:
        now = int(time.time() * 1000)
        self._enqueue("mutations:updatePositionClose", {
            "positionId": id,
            "exitPrice": exit_price,
            "pnlUsd": pnl_usd,
            "pnlPct": pnl_pct,
            "exitReason": exit_reason,
            "closedAt": now,
        })

    def insert_trade(self, t: Trade) -> None:
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
        now = int(time.time() * 1000)
        self._enqueue("mutations:insertLog", {
            "logId": str(uuid.uuid4()),
            "level": level,
            "message": message,
            "symbol": symbol,
            "strategy": strategy,
            "data": json.dumps(data) if data else None,
            "ts": now,
        })

        ts_str = datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat()
        sym_tag = f" [{symbol}]" if symbol else ""
        logger.info("[%s] [%s]%s %s", ts_str, level.upper(), sym_tag, message)

    def insert_diagnosis(self, d: TradeDiagnosis) -> None:
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
        config_dict = (
            dataclasses.asdict(config)
            if dataclasses.is_dataclass(config) and not isinstance(config, type)
            else config
        )
        self._enqueue("mutations:snapshotConfig", {
            "config": json.dumps(config_dict),
            "reason": reason,
            "timestamp": int(time.time() * 1000),
        })

    def insert_trade_journal(self, entry: dict) -> None:
        self._enqueue("mutations:insertTradeJournal", {
            "positionId": entry["position_id"],
            "symbol": entry["symbol"],
            "strategy": entry["strategy"],
            "rMultiple": entry.get("r_multiple"),
            "holdHours": entry.get("hold_hours"),
            "maePct": entry.get("mae_pct"),
            "mfePct": entry.get("mfe_pct"),
            "partialExitPct": entry.get("partial_exit_pct"),
            "exitReason": entry.get("exit_reason"),
            "pnlPct": entry.get("pnl_pct"),
            "regimeAtEntry": entry.get("regime_at_entry"),
            "regimeAtExit": entry.get("regime_at_exit"),
            "wasPartialBeneficial": entry.get("was_partial_beneficial"),
            "timestamp": entry["timestamp"],
        })

    def close_orphaned_positions(self, exit_reason: str = "orphaned_restart") -> dict:
        """Close all open positions in Convex (from a previous bot run)."""
        client = self._get_client()
        result = client.mutation("mutations:closeOrphanedPositions", {
            "exitReason": exit_reason,
            "closedAt": time.time() * 1000,
        })
        return result or {"closed": 0, "positionIds": []}

    # ─── Read operations (sync) ────────────────────────────────────────────

    def get_open_positions(self) -> list[Position]:
        client = self._get_client()
        rows = client.query("queries:getOpenPositions", {
            "paperTrading": self._paper_trading,
        })
        return [self._row_to_position(r) for r in (rows or [])]

    def get_closed_trades(self, limit: int = 200) -> list[Position]:
        client = self._get_client()
        rows = client.query("queries:getClosedTrades", {
            "limit": limit,
            "paperTrading": self._paper_trading,
        })
        return [self._row_to_position(r) for r in (rows or [])]

    def get_recent_logs(self, limit: int = 500, level: str | None = None) -> list[LogEntry]:
        client = self._get_client()
        args: dict[str, Any] = {"limit": limit}
        if level:
            args["level"] = level
        rows = client.query("queries:getRecentLogs", args)
        return [self._row_to_log(r) for r in (rows or [])]

    def get_recent_diagnoses(self, limit: int = 50) -> list[TradeDiagnosis]:
        client = self._get_client()
        rows = client.query("queries:getRecentDiagnoses", {"limit": limit})
        return [self._row_to_diagnosis(r) for r in (rows or [])]

    def get_trade_journal(self, limit: int = 50) -> list[dict]:
        client = self._get_client()
        rows = client.query("queries:getTradeJournal", {"limit": limit})
        return rows or []

    # ─── Row converters (Convex camelCase → Python dataclass) ──────────────

    @staticmethod
    def _row_to_position(r: dict) -> Position:
        entry_price = r.get("entryPrice", 0)
        size_usd = r.get("sizeUsd", 0)
        quantity = r.get("quantity", 0)
        return Position(
            id=r.get("positionId", ""), symbol=r.get("symbol", ""),
            product_id=r.get("productId", ""),
            strategy=r.get("strategy", "momentum_swing"),
            side=r.get("side", "long"), tier=r.get("tier", "swing"),
            entry_price=entry_price, quantity=quantity,
            size_usd=size_usd, opened_at=r.get("openedAt", 0),
            high_watermark=r.get("highWatermark", entry_price),
            low_watermark=r.get("lowWatermark", entry_price),
            current_price=r.get("currentPrice", entry_price),
            trail_pct=r.get("trailPct", 0.07),
            stop_price=r.get("stopPrice", 0), max_hold_ms=r.get("maxHoldMs", 0),
            qual_score=r.get("qualScore", 0), signal_id=r.get("signalId", ""),
            status=r.get("status", "open"), exit_price=r.get("exitPrice"),
            closed_at=r.get("closedAt"), pnl_usd=r.get("pnlUsd"),
            pnl_pct=r.get("pnlPct"), exit_reason=r.get("exitReason"),
            paper_trading=r.get("paperTrading", True),
            # New tracked fields
            mae_pct=r.get("maePct", 0.0),
            mfe_pct=r.get("mfePct", 0.0),
            partial_exit_pct=r.get("partialExitPct", 0.0),
            tranche_count=r.get("trancheCount", 1),
            avg_entry_price=r.get("avgEntryPrice", entry_price),
            original_quantity=r.get("originalQuantity", quantity),
            entry_size_usd=r.get("entrySizeUsd", size_usd),
            total_commission=r.get("totalCommission", 0.0),
            initial_stop_price=r.get("initialStopPrice", 0.0),
        )

    @staticmethod
    def _row_to_log(r: dict) -> LogEntry:
        raw_data = r.get("data")
        parsed_data = None
        if raw_data:
            try:
                parsed_data = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                parsed_data = None
        return LogEntry(
            id=r["logId"], level=r["level"], message=r["message"],
            symbol=r.get("symbol"), strategy=r.get("strategy"),
            data=parsed_data, ts=r["ts"],
        )

    @staticmethod
    def _row_to_diagnosis(r: dict) -> TradeDiagnosis:
        raw_changes = r.get("parameterChanges", "{}")
        try:
            param_changes = json.loads(raw_changes) if raw_changes else {}
        except (json.JSONDecodeError, TypeError):
            param_changes = {}
        return TradeDiagnosis(
            position_id=r["positionId"], symbol=r["symbol"],
            strategy=r["strategy"], pnl_pct=r["pnlPct"],
            hold_ms=r["holdMs"], exit_reason=r["exitReason"],
            loss_reason=r["lossReason"], entry_qual_score=r["entryQualScore"],
            market_phase_at_entry=r["marketPhaseAtEntry"], action=r["action"],
            parameter_changes=param_changes,
            timestamp=r["timestamp"],
        )
