"""Tests for the Convex storage layer with mocked client."""

import json
import time
import uuid
import pytest
from unittest.mock import MagicMock, patch

from src.storage.convex_client import ConvexStorage
from src.config import validate_config
from src.types import Position, Trade, TradeDiagnosis, ScannerConfig, LogEntry


def _make_position(id=None, symbol="ETH", strategy="momentum_swing",
                   pnl_pct=None, pnl_usd=None, status="open") -> Position:
    now = int(time.time() * 1000)
    return Position(
        id=id or str(uuid.uuid4()),
        symbol=symbol, product_id=f"{symbol}-USD",
        strategy=strategy, side="long", tier="swing",
        entry_price=2000, quantity=0.5, size_usd=1000,
        opened_at=now, high_watermark=2100, low_watermark=1900,
        current_price=2000, trail_pct=0.07, stop_price=1860,
        max_hold_ms=43_200_000, qual_score=70,
        signal_id=str(uuid.uuid4()), status=status,
        pnl_usd=pnl_usd, pnl_pct=pnl_pct,
    )


def _make_convex_position_row(p: Position) -> dict:
    """Convert a Position to a Convex-style camelCase row dict."""
    return {
        "positionId": p.id, "symbol": p.symbol, "productId": p.product_id,
        "strategy": p.strategy, "side": p.side, "tier": p.tier,
        "entryPrice": p.entry_price, "quantity": p.quantity,
        "sizeUsd": p.size_usd, "openedAt": p.opened_at,
        "highWatermark": p.high_watermark, "lowWatermark": p.low_watermark,
        "currentPrice": p.current_price, "trailPct": p.trail_pct,
        "stopPrice": p.stop_price, "maxHoldMs": p.max_hold_ms,
        "qualScore": p.qual_score, "signalId": p.signal_id,
        "status": p.status, "exitPrice": p.exit_price,
        "closedAt": p.closed_at, "pnlUsd": p.pnl_usd,
        "pnlPct": p.pnl_pct, "exitReason": p.exit_reason,
        "paperTrading": p.paper_trading,
    }


@pytest.fixture
def mock_client():
    """Create a ConvexStorage with a mocked Convex client."""
    client = MagicMock()
    storage = ConvexStorage(url="https://test.convex.cloud", client=client)
    return storage, client


class TestWriteOperations:
    def test_insert_position_enqueues(self, mock_client):
        storage, client = mock_client
        p = _make_position()
        storage.insert_position(p)
        assert storage.pending_count == 1

    def test_insert_position_flushes_to_convex(self, mock_client):
        storage, client = mock_client
        p = _make_position()
        storage.insert_position(p)
        storage._drain_queue()
        client.mutation.assert_called_once()
        call_args = client.mutation.call_args
        assert call_args[0][0] == "mutations:insertPosition"
        assert call_args[0][1]["positionId"] == p.id
        assert call_args[0][1]["symbol"] == "ETH"

    def test_update_position_close(self, mock_client):
        storage, client = mock_client
        storage.update_position_close("pos-1", 2100, 50, 0.05, "take_profit")
        storage._drain_queue()
        client.mutation.assert_called_once()
        args = client.mutation.call_args[0]
        assert args[0] == "mutations:updatePositionClose"
        assert args[1]["positionId"] == "pos-1"
        assert args[1]["exitPrice"] == 2100

    def test_insert_trade(self, mock_client):
        storage, client = mock_client
        t = Trade(
            id="t-1", position_id="pos-1", side="long", symbol="ETH",
            quantity=0.5, size_usd=1000, price=2000, status="filled",
            paper_trading=True, placed_at=int(time.time() * 1000),
        )
        storage.insert_trade(t)
        storage._drain_queue()
        client.mutation.assert_called_once()
        assert client.mutation.call_args[0][0] == "mutations:insertTrade"

    def test_log_enqueues_and_prints(self, mock_client, capsys):
        storage, client = mock_client
        storage.log("info", "Test message", symbol="ETH")
        captured = capsys.readouterr()
        assert "[INFO]" in captured.out
        assert "Test message" in captured.out
        assert storage.pending_count == 1

    def test_insert_diagnosis(self, mock_client):
        storage, client = mock_client
        d = TradeDiagnosis(
            position_id="pos-1", symbol="ETH", strategy="momentum_swing",
            pnl_pct=-0.03, hold_ms=3_600_000, exit_reason="trailing_stop",
            loss_reason="entered_pump_top", entry_qual_score=65,
            market_phase_at_entry="bull", action="raise momentum_pct",
            parameter_changes={"momentum_pct_swing": 0.03},
            timestamp=int(time.time() * 1000),
        )
        storage.insert_diagnosis(d)
        storage._drain_queue()
        args = client.mutation.call_args[0][1]
        assert args["lossReason"] == "entered_pump_top"

    def test_snapshot_config(self, mock_client):
        storage, client = mock_client
        config = ScannerConfig()
        storage.snapshot_config(config, "test snapshot")
        storage._drain_queue()
        args = client.mutation.call_args[0][1]
        assert args["reason"] == "test snapshot"
        stored = json.loads(args["config"])
        assert stored["momentum_pct_swing"] == 0.02

    def test_flush_error_does_not_crash(self, mock_client):
        storage, client = mock_client
        client.mutation.side_effect = RuntimeError("Convex down")
        storage.log("info", "msg")
        storage._drain_queue()  # should not raise


class TestReadOperations:
    def test_get_open_positions(self, mock_client):
        storage, client = mock_client
        p = _make_position()
        client.query.return_value = [_make_convex_position_row(p)]
        positions = storage.get_open_positions()
        assert len(positions) == 1
        assert positions[0].id == p.id
        assert positions[0].symbol == "ETH"
        client.query.assert_called_with("queries:getOpenPositions", {"paperTrading": True})

    def test_get_closed_trades(self, mock_client):
        storage, client = mock_client
        p = _make_position(status="closed", pnl_pct=0.05, pnl_usd=50)
        row = _make_convex_position_row(p)
        row["exitPrice"] = 2100
        row["closedAt"] = time.time() * 1000
        client.query.return_value = [row]
        closed = storage.get_closed_trades(10)
        assert len(closed) == 1
        assert closed[0].pnl_pct == 0.05
        client.query.assert_called_with("queries:getClosedTrades", {"limit": 10, "paperTrading": True})

    def test_get_recent_logs(self, mock_client):
        storage, client = mock_client
        now = int(time.time() * 1000)
        client.query.return_value = [{
            "logId": "log-1", "level": "info", "message": "test",
            "symbol": "ETH", "strategy": None, "data": None, "ts": now,
        }]
        logs = storage.get_recent_logs(10)
        assert len(logs) == 1
        assert logs[0].message == "test"
        assert logs[0].level == "info"

    def test_get_recent_logs_with_level_filter(self, mock_client):
        storage, client = mock_client
        client.query.return_value = []
        storage.get_recent_logs(10, level="error")
        client.query.assert_called_with("queries:getRecentLogs", {"limit": 10, "level": "error"})

    def test_get_recent_diagnoses(self, mock_client):
        storage, client = mock_client
        now = int(time.time() * 1000)
        client.query.return_value = [{
            "positionId": "pos-1", "symbol": "ETH", "strategy": "momentum_swing",
            "pnlPct": -0.03, "holdMs": 3600000, "exitReason": "trailing_stop",
            "lossReason": "entered_pump_top", "entryQualScore": 65,
            "marketPhaseAtEntry": "bull", "action": "raise momentum_pct",
            "parameterChanges": '{"momentum_pct_swing": 0.03}',
            "timestamp": now,
        }]
        diagnoses = storage.get_recent_diagnoses(10)
        assert len(diagnoses) == 1
        assert diagnoses[0].loss_reason == "entered_pump_top"
        assert diagnoses[0].parameter_changes == {"momentum_pct_swing": 0.03}

    def test_empty_results(self, mock_client):
        storage, client = mock_client
        client.query.return_value = []
        assert storage.get_open_positions() == []
        assert storage.get_closed_trades() == []
        assert storage.get_recent_logs() == []

    def test_none_results(self, mock_client):
        storage, client = mock_client
        client.query.return_value = None
        assert storage.get_open_positions() == []
        assert storage.get_closed_trades() == []


class TestRowConverters:
    def test_position_round_trip(self):
        p = _make_position(pnl_pct=0.05, pnl_usd=50)
        row = _make_convex_position_row(p)
        result = ConvexStorage._row_to_position(row)
        assert result.id == p.id
        assert result.symbol == p.symbol
        assert result.entry_price == p.entry_price
        assert result.paper_trading == p.paper_trading

    def test_log_with_json_data(self):
        row = {
            "logId": "log-1", "level": "trade", "message": "opened",
            "symbol": "ETH", "strategy": "momentum_swing",
            "data": '{"size": 1000}', "ts": 1000,
        }
        result = ConvexStorage._row_to_log(row)
        assert result.data == {"size": 1000}

    def test_log_with_corrupt_json(self):
        row = {
            "logId": "log-1", "level": "info", "message": "test",
            "symbol": None, "strategy": None,
            "data": "not-valid-json{{{", "ts": 1000,
        }
        result = ConvexStorage._row_to_log(row)
        assert result.data is None

    def test_diagnosis_with_corrupt_params(self):
        row = {
            "positionId": "pos-1", "symbol": "ETH", "strategy": "momentum_swing",
            "pnlPct": -0.01, "holdMs": 3600000, "exitReason": "trailing_stop",
            "lossReason": "unknown", "entryQualScore": 60,
            "marketPhaseAtEntry": "neutral", "action": "no change",
            "parameterChanges": "corrupt!!!", "timestamp": 1000,
        }
        result = ConvexStorage._row_to_diagnosis(row)
        assert result.parameter_changes == {}


class TestDatabaseFacade:
    def test_init_and_close(self):
        import src.storage.database as db
        mock_client = MagicMock()
        with patch("src.storage.convex_client.ConvexStorage._get_client", return_value=mock_client):
            db.init("https://test.convex.cloud")
            assert db._storage is not None
            db.close()
            assert db._storage is None

    def test_raises_when_not_initialized(self):
        import src.storage.database as db
        db._storage = None
        with pytest.raises(RuntimeError, match="Database not initialized"):
            db.get_open_positions()

    def test_batch_writes_is_noop(self):
        import src.storage.database as db
        mock_storage = MagicMock(spec=ConvexStorage)
        db._storage = mock_storage
        try:
            with db.batch_writes():
                db.insert_position(_make_position())
            mock_storage.insert_position.assert_called_once()
        finally:
            db._storage = None


class TestValidateConfig:
    def test_default_config_is_valid(self):
        violations = validate_config(ScannerConfig())
        assert violations == []

    def test_below_minimum_detected(self):
        config = ScannerConfig(momentum_pct_swing=0.001)
        violations = validate_config(config)
        assert len(violations) == 1
        assert "momentum_pct_swing" in violations[0]

    def test_above_maximum_detected(self):
        config = ScannerConfig(rsi_overbought=99)
        violations = validate_config(config)
        assert len(violations) == 1
        assert "rsi_overbought" in violations[0]

    def test_multiple_violations(self):
        config = ScannerConfig(momentum_pct_swing=0.001, rsi_overbought=99)
        violations = validate_config(config)
        assert len(violations) == 2
