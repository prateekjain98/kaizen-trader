"""Tests for the Convex storage client."""

import json
import time
import threading
import unittest
from unittest.mock import MagicMock, patch, call

from src.types import Position, Trade, TradeDiagnosis, ScannerConfig
from src.storage.convex_client import ConvexStorage


def _now_ms() -> float:
    return time.time() * 1000


def _make_position(**overrides) -> Position:
    now = _now_ms()
    defaults = dict(
        id="pos-1", symbol="ETH", product_id="ETH-USD",
        strategy="momentum_swing", side="long", tier="swing",
        entry_price=2000.0, quantity=0.5, size_usd=1000.0,
        opened_at=now, high_watermark=2100.0, low_watermark=1900.0,
        current_price=2050.0, trail_pct=0.07, stop_price=1860.0,
        max_hold_ms=43_200_000.0, qual_score=70.0, signal_id="sig-1",
        status="open", paper_trading=True,
    )
    defaults.update(overrides)
    return Position(**defaults)


def _make_trade(**overrides) -> Trade:
    defaults = dict(
        id="trade-1", position_id="pos-1", side="long", symbol="ETH",
        quantity=0.5, size_usd=1000.0, price=2000.0, status="filled",
        paper_trading=True, placed_at=_now_ms(),
    )
    defaults.update(overrides)
    return Trade(**defaults)


def _make_diagnosis(**overrides) -> TradeDiagnosis:
    defaults = dict(
        position_id="pos-1", symbol="ETH", strategy="momentum_swing",
        pnl_pct=-0.03, hold_ms=7_200_000.0, exit_reason="trailing_stop",
        loss_reason="stop_too_tight", entry_qual_score=65.0,
        market_phase_at_entry="bull", action="widen_trail",
        parameter_changes={"base_trail_pct_swing": 0.08},
        timestamp=_now_ms(),
    )
    defaults.update(overrides)
    return TradeDiagnosis(**defaults)


class TestConvexStorageQueue(unittest.TestCase):
    """Test the queue-based flush mechanism."""

    def test_enqueue_adds_items(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        pos = _make_position()
        storage.insert_position(pos)
        assert storage.pending_count == 1

    def test_drain_queue_calls_client_mutation(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        pos = _make_position()
        storage.insert_position(pos)

        storage._drain_queue()

        mock_client.mutation.assert_called_once()
        call_args = mock_client.mutation.call_args
        assert call_args[0][0] == "mutations:insertPosition"
        assert call_args[0][1]["positionId"] == "pos-1"
        assert storage.pending_count == 0

    def test_drain_queue_handles_mutation_error(self):
        mock_client = MagicMock()
        mock_client.mutation.side_effect = RuntimeError("network error")
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        storage.insert_position(_make_position())

        storage._drain_queue()
        assert storage.pending_count == 0

    def test_multiple_writes_queued(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)

        storage.insert_position(_make_position())
        storage.insert_trade(_make_trade())
        storage.insert_diagnosis(_make_diagnosis())
        storage.log("info", "test message", symbol="ETH")

        assert storage.pending_count == 4

        storage._drain_queue()
        assert mock_client.mutation.call_count == 4
        assert storage.pending_count == 0


class TestConvexStorageFlushThread(unittest.TestCase):
    """Test that the flush thread processes items."""

    def test_flush_thread_processes_items(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        storage._flush_interval = 0.1

        storage.start()
        try:
            storage.insert_position(_make_position())
            storage.insert_trade(_make_trade())

            deadline = time.time() + 3.0
            while storage.pending_count > 0 and time.time() < deadline:
                time.sleep(0.05)

            assert storage.pending_count == 0
            assert mock_client.mutation.call_count == 2
        finally:
            storage.stop()

    def test_graceful_shutdown_flushes_remaining(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        storage._flush_interval = 0.2  # short interval so join is fast

        storage.start()
        storage.insert_position(_make_position())
        storage.insert_trade(_make_trade())
        storage.insert_diagnosis(_make_diagnosis())

        storage.stop()

        assert storage.pending_count == 0
        assert mock_client.mutation.call_count == 3


class TestConvexStorageDataMapping(unittest.TestCase):
    """Test correct data mapping from Python types to Convex args."""

    def test_position_close_sends_correct_args(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)

        storage.update_position_close("pos-1", 2100.0, 50.0, 0.05, "trailing_stop")
        storage._drain_queue()

        call_args = mock_client.mutation.call_args[0]
        assert call_args[0] == "mutations:updatePositionClose"
        payload = call_args[1]
        assert payload["positionId"] == "pos-1"
        assert payload["exitPrice"] == 2100.0
        assert payload["pnlUsd"] == 50.0
        assert payload["pnlPct"] == 0.05
        assert payload["exitReason"] == "trailing_stop"
        assert "closedAt" in payload

    def test_snapshot_config_serializes_dataclass(self):
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)

        config = ScannerConfig()
        storage.snapshot_config(config, "test snapshot")
        storage._drain_queue()

        call_args = mock_client.mutation.call_args[0]
        assert call_args[0] == "mutations:snapshotConfig"
        payload = call_args[1]
        parsed = json.loads(payload["config"])
        assert parsed["momentum_pct_swing"] == 0.02
        assert payload["reason"] == "test snapshot"


if __name__ == "__main__":
    unittest.main()
