"""Tests for the SQLite storage layer using in-memory database."""

import json
import os
import time
import uuid
import pytest

# Force in-memory DB before importing
os.environ["DB_PATH"] = ":memory:"

from src.storage.database import (
    db, insert_position, update_position_close, get_open_positions,
    get_closed_trades, insert_trade, log, get_recent_logs,
    insert_diagnosis, get_recent_diagnoses, snapshot_config,
    batch_writes, close,
)
from src.config import validate_config
from src.types import Position, Trade, TradeDiagnosis, ScannerConfig


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


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the DB connection for each test to get a fresh in-memory DB."""
    import src.storage.database as db_mod
    db_mod._conn = None
    db_mod._batch_active = False
    db_mod.DB_PATH = ":memory:"
    yield


class TestPositionCRUD:
    def test_insert_and_read_open(self):
        p = _make_position()
        insert_position(p)
        positions = get_open_positions()
        assert len(positions) == 1
        assert positions[0].id == p.id
        assert positions[0].symbol == "ETH"
        assert positions[0].status == "open"

    def test_close_position(self):
        p = _make_position()
        insert_position(p)
        update_position_close(p.id, exit_price=2100, pnl_usd=50,
                              pnl_pct=0.05, exit_reason="take_profit")
        open_pos = get_open_positions()
        assert len(open_pos) == 0

        closed = get_closed_trades(10)
        assert len(closed) == 1
        assert closed[0].exit_price == 2100
        assert closed[0].pnl_usd == 50
        assert closed[0].pnl_pct == 0.05
        assert closed[0].exit_reason == "take_profit"
        assert closed[0].closed_at is not None

    def test_multiple_positions(self):
        for sym in ["ETH", "BTC", "SOL"]:
            insert_position(_make_position(symbol=sym))
        positions = get_open_positions()
        assert len(positions) == 3
        symbols = {p.symbol for p in positions}
        assert symbols == {"ETH", "BTC", "SOL"}

    def test_closed_trades_ordered_by_closed_at_desc(self):
        ids = []
        for i in range(3):
            p = _make_position()
            insert_position(p)
            ids.append(p.id)
            # Close with slight time gap
            time.sleep(0.01)
            update_position_close(p.id, exit_price=2000, pnl_usd=10,
                                  pnl_pct=0.01, exit_reason="take_profit")

        closed = get_closed_trades(10)
        assert len(closed) == 3
        # Most recently closed first
        assert closed[0].id == ids[2]

    def test_closed_trades_limit(self):
        for _ in range(5):
            p = _make_position()
            insert_position(p)
            update_position_close(p.id, exit_price=2000, pnl_usd=0,
                                  pnl_pct=0, exit_reason="time_limit")
        assert len(get_closed_trades(3)) == 3
        assert len(get_closed_trades(10)) == 5

    def test_paper_trading_flag(self):
        p = _make_position()
        p.paper_trading = True
        insert_position(p)
        loaded = get_open_positions()[0]
        assert loaded.paper_trading is True


class TestTrades:
    def test_insert_trade(self):
        p = _make_position()
        insert_position(p)
        t = Trade(
            id=str(uuid.uuid4()), position_id=p.id,
            side="long", symbol="ETH", quantity=0.5,
            size_usd=1000, price=2000, status="filled",
            paper_trading=True, placed_at=int(time.time() * 1000),
        )
        insert_trade(t)
        # Verify by querying directly
        row = db().execute("SELECT * FROM trades WHERE id=?", (t.id,)).fetchone()
        assert row is not None
        assert row["symbol"] == "ETH"


class TestLogs:
    def test_log_and_retrieve(self, capsys):
        log("info", "Test message", symbol="ETH", strategy="momentum_swing")
        logs = get_recent_logs(10)
        assert len(logs) >= 1
        entry = logs[0]
        assert entry.level == "info"
        assert entry.message == "Test message"
        assert entry.symbol == "ETH"

    def test_log_with_data(self):
        log("trade", "Opened position", data={"size": 1000, "price": 2000})
        logs = get_recent_logs(10)
        entry = logs[0]
        assert entry.data == {"size": 1000, "price": 2000}

    def test_log_level_filter(self):
        log("info", "info msg")
        log("error", "error msg")
        log("warn", "warn msg")
        errors = get_recent_logs(10, level="error")
        assert len(errors) == 1
        assert errors[0].message == "error msg"

    def test_log_limit(self):
        for i in range(5):
            log("info", f"msg {i}")
        logs = get_recent_logs(3)
        assert len(logs) == 3

    def test_log_prints_to_stdout(self, capsys):
        log("info", "Hello world")
        captured = capsys.readouterr()
        assert "Hello world" in captured.out
        assert "[INFO]" in captured.out


class TestDiagnoses:
    def test_insert_and_retrieve(self):
        d = TradeDiagnosis(
            position_id="pos-1", symbol="ETH", strategy="momentum_swing",
            pnl_pct=-0.03, hold_ms=3_600_000, exit_reason="trailing_stop",
            loss_reason="entered_pump_top", entry_qual_score=65,
            market_phase_at_entry="bull", action="raise momentum_pct",
            parameter_changes={"momentum_pct_swing": 0.03},
            timestamp=int(time.time() * 1000),
        )
        insert_diagnosis(d)
        diagnoses = get_recent_diagnoses(10)
        assert len(diagnoses) == 1
        assert diagnoses[0].loss_reason == "entered_pump_top"
        assert diagnoses[0].parameter_changes == {"momentum_pct_swing": 0.03}

    def test_diagnoses_ordered_desc(self):
        for i in range(3):
            d = TradeDiagnosis(
                position_id=f"pos-{i}", symbol="ETH", strategy="momentum_swing",
                pnl_pct=-0.01 * (i + 1), hold_ms=3_600_000,
                exit_reason="trailing_stop", loss_reason="unknown",
                entry_qual_score=60, market_phase_at_entry="neutral",
                action="no change", parameter_changes={},
                timestamp=int(time.time() * 1000) + i * 1000,
            )
            insert_diagnosis(d)
        diagnoses = get_recent_diagnoses(10)
        assert diagnoses[0].position_id == "pos-2"  # most recent


class TestConfigSnapshots:
    def test_snapshot_config(self):
        config = ScannerConfig()
        snapshot_config(config, "test snapshot")
        row = db().execute(
            "SELECT * FROM scanner_config_history ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["reason"] == "test snapshot"
        stored = json.loads(row["config"])
        assert stored["momentum_pct_swing"] == 0.02


class TestBatchWrites:
    def test_batch_writes_commits_once(self):
        with batch_writes():
            for sym in ["ETH", "BTC", "SOL"]:
                insert_position(_make_position(symbol=sym))
        positions = get_open_positions()
        assert len(positions) == 3

    def test_batch_writes_rollback_on_error(self):
        try:
            with batch_writes():
                insert_position(_make_position(symbol="ETH"))
                raise ValueError("simulated failure")
        except ValueError:
            pass
        # The position should have been rolled back
        positions = get_open_positions()
        assert len(positions) == 0


class TestClose:
    def test_close_sets_conn_to_none(self):
        import src.storage.database as db_mod
        db()  # ensure connection exists
        assert db_mod._conn is not None
        close()
        assert db_mod._conn is None

    def test_close_allows_reopen(self):
        close()
        # After close, db() should create a new connection and work
        insert_position(_make_position(symbol="ETH"))
        positions = get_open_positions()
        assert len(positions) == 1

    def test_close_idempotent(self):
        close()
        close()  # should not raise


class TestCorruptJsonHandling:
    def test_corrupt_log_data_returns_none(self):
        """If JSON in the data column is corrupt, _row_to_log returns None for data."""
        entry_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        db().execute(
            "INSERT INTO logs VALUES (?,?,?,?,?,?,?)",
            (entry_id, "info", "test", None, None, "not-valid-json{{{", now),
        )
        db().commit()
        logs = get_recent_logs(10)
        match = [l for l in logs if l.id == entry_id]
        assert len(match) == 1
        assert match[0].data is None

    def test_corrupt_diagnosis_params_returns_empty_dict(self):
        """If parameter_changes JSON is corrupt, returns empty dict."""
        diag_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        db().execute(
            "INSERT INTO diagnoses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (diag_id, "pos-x", "ETH", "momentum_swing", -0.01, 3600000,
             "trailing_stop", "unknown", 60, "neutral", "no change",
             "corrupt-json!!!", now),
        )
        db().commit()
        diagnoses = get_recent_diagnoses(10)
        match = [d for d in diagnoses if d.position_id == "pos-x"]
        assert len(match) == 1
        assert match[0].parameter_changes == {}


class TestValidateConfig:
    def test_default_config_is_valid(self):
        violations = validate_config(ScannerConfig())
        assert violations == []

    def test_below_minimum_detected(self):
        config = ScannerConfig(momentum_pct_swing=0.001)  # min is 0.01
        violations = validate_config(config)
        assert len(violations) == 1
        assert "momentum_pct_swing" in violations[0]
        assert "below minimum" in violations[0]

    def test_above_maximum_detected(self):
        config = ScannerConfig(rsi_overbought=99)  # max is 80
        violations = validate_config(config)
        assert len(violations) == 1
        assert "rsi_overbought" in violations[0]
        assert "above maximum" in violations[0]

    def test_multiple_violations(self):
        config = ScannerConfig(
            momentum_pct_swing=0.001,  # below min
            rsi_overbought=99,         # above max
        )
        violations = validate_config(config)
        assert len(violations) == 2
