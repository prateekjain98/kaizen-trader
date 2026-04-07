"""Tests for the Convex storage client and DualWriteBackend."""

import json
import time
import threading
import unittest
from unittest.mock import MagicMock, patch, call

from src.types import Position, Trade, TradeDiagnosis, ScannerConfig
from src.storage.convex_client import ConvexStorage
from src.storage.backend import DualWriteBackend, StorageBackend


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
        """Items are added to the internal queue."""
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        pos = _make_position()
        storage.insert_position(pos)
        assert storage.pending_count == 1

    def test_drain_queue_calls_client_mutation(self):
        """Draining processes queued items via client.mutation()."""
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
        """Mutation errors are caught, not propagated."""
        mock_client = MagicMock()
        mock_client.mutation.side_effect = RuntimeError("network error")
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        storage.insert_position(_make_position())

        # Should not raise
        storage._drain_queue()
        assert storage.pending_count == 0

    def test_multiple_writes_queued(self):
        """Multiple different writes all land in the queue."""
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
        """The background thread drains items within a few seconds."""
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        storage._flush_interval = 0.1  # speed up for test

        storage.start()
        try:
            storage.insert_position(_make_position())
            storage.insert_trade(_make_trade())

            # Wait for flush thread to process
            deadline = time.time() + 3.0
            while storage.pending_count > 0 and time.time() < deadline:
                time.sleep(0.05)

            assert storage.pending_count == 0
            assert mock_client.mutation.call_count == 2
        finally:
            storage.stop()

    def test_graceful_shutdown_flushes_remaining(self):
        """stop() drains any remaining items before returning."""
        mock_client = MagicMock()
        storage = ConvexStorage(url="https://test.convex.cloud", client=mock_client)
        storage._flush_interval = 10.0  # long interval so thread won't flush

        storage.start()
        storage.insert_position(_make_position())
        storage.insert_trade(_make_trade())
        storage.insert_diagnosis(_make_diagnosis())

        # stop() should drain remaining items
        storage.stop()

        assert storage.pending_count == 0
        assert mock_client.mutation.call_count == 3


class TestConvexStorageDataMapping(unittest.TestCase):
    """Test correct data mapping from Python types to Convex args."""

    def test_position_close_sends_correct_args(self):
        """update_position_close maps fields correctly."""
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
        """snapshot_config correctly serializes a ScannerConfig dataclass."""
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


class TestDualWriteBackend(unittest.TestCase):
    """Test that DualWriteBackend delegates to both backends."""

    def _make_backends(self):
        primary = MagicMock(spec=["insert_position", "update_position_close",
                                   "insert_trade", "log", "insert_diagnosis",
                                   "snapshot_config"])
        fallback = MagicMock(spec=["insert_position", "update_position_close",
                                    "insert_trade", "log", "insert_diagnosis",
                                    "snapshot_config"])
        return primary, fallback

    def test_delegates_to_both_backends(self):
        """Both primary and fallback receive the write."""
        primary, fallback = self._make_backends()
        dual = DualWriteBackend(primary=primary, fallback=fallback)
        pos = _make_position()

        dual.insert_position(pos)

        primary.insert_position.assert_called_once_with(pos)
        fallback.insert_position.assert_called_once_with(pos)

    def test_primary_failure_still_writes_fallback(self):
        """If primary fails, fallback still gets the write."""
        primary, fallback = self._make_backends()
        primary.insert_trade.side_effect = RuntimeError("convex down")
        dual = DualWriteBackend(primary=primary, fallback=fallback)
        trade = _make_trade()

        # Should not raise
        dual.insert_trade(trade)

        fallback.insert_trade.assert_called_once_with(trade)
        primary.insert_trade.assert_called_once_with(trade)

    def test_all_methods_delegate(self):
        """Every StorageBackend method is forwarded by DualWriteBackend."""
        primary, fallback = self._make_backends()
        dual = DualWriteBackend(primary=primary, fallback=fallback)

        dual.insert_position(_make_position())
        dual.update_position_close("pos-1", 2100.0, 50.0, 0.05, "trailing_stop")
        dual.insert_trade(_make_trade())
        dual.log("info", "test")
        dual.insert_diagnosis(_make_diagnosis())
        dual.snapshot_config(ScannerConfig(), "test")

        assert primary.insert_position.call_count == 1
        assert primary.update_position_close.call_count == 1
        assert primary.insert_trade.call_count == 1
        assert primary.log.call_count == 1
        assert primary.insert_diagnosis.call_count == 1
        assert primary.snapshot_config.call_count == 1

        assert fallback.insert_position.call_count == 1
        assert fallback.update_position_close.call_count == 1
        assert fallback.insert_trade.call_count == 1
        assert fallback.log.call_count == 1
        assert fallback.insert_diagnosis.call_count == 1
        assert fallback.snapshot_config.call_count == 1


if __name__ == "__main__":
    unittest.main()
