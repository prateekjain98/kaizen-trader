"""Tests for the enhanced logging and AI evaluation pipeline."""

import json
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

# Force in-memory DB before importing
os.environ["DB_PATH"] = ":memory:"
os.environ["PAPER_TRADING"] = "true"

from src.storage.database import (
    db, log, insert_position, insert_trade, insert_diagnosis,
    snapshot_config, init_dual_write, get_recent_logs, _backend,
)
import src.storage.database as db_mod
from src.self_healing.log_analyzer import (
    _build_prompt, _build_delta_section, _build_strategy_health_section,
    _build_blind_spots_section, Analysis, DataSourceSuggestion,
)
from src.self_healing.delta_evaluator import DeltaEvaluator, ParameterDelta, TradeSnapshot, get_evaluator
from src.self_healing.blind_spots import BlindSpotDetector, BlindSpotConfig, UnknownFingerprint
from src.evaluation.strategy_selector import StrategySelector, SelectionConfig, StrategyHealth
from src.types import ScannerConfig, Position, TradeDiagnosis


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the DB connection and backend for each test."""
    db_mod._conn = None
    db_mod._backend = None
    db_mod.DB_PATH = ":memory:"
    yield
    db_mod._backend = None


def _make_position(symbol="ETH", strategy="momentum_swing", pnl_pct=None,
                   pnl_usd=None, status="open") -> Position:
    now = int(time.time() * 1000)
    return Position(
        id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
        strategy=strategy, side="long", tier="swing",
        entry_price=2000, quantity=0.5, size_usd=1000,
        opened_at=now, high_watermark=2100, low_watermark=1900,
        current_price=2000, trail_pct=0.07, stop_price=1860,
        max_hold_ms=43_200_000, qual_score=70,
        signal_id=str(uuid.uuid4()), status=status,
        pnl_usd=pnl_usd, pnl_pct=pnl_pct,
    )


# ─── Test 1: init_dual_write sets up the backend ─────────────────────────────

class TestInitDualWrite:
    def test_init_dual_write_sets_backend(self):
        """init_dual_write should set the module-level _backend."""
        assert db_mod._backend is None

        mock_convex = MagicMock()
        with patch("src.storage.convex_client.ConvexStorage", return_value=mock_convex):
            with patch("src.storage.backend.DualWriteBackend") as MockDualWrite:
                mock_dual = MagicMock()
                MockDualWrite.return_value = mock_dual
                init_dual_write("https://fake-convex.example.com")

        assert db_mod._backend is mock_dual
        mock_convex.start.assert_called_once()

    def test_init_dual_write_not_set_by_default(self):
        """Without calling init_dual_write, _backend should remain None."""
        assert db_mod._backend is None


# ─── Test 2: log() forwards to dual-write backend ────────────────────────────

class TestDualWriteForwarding:
    def test_log_forwards_to_backend_when_set(self):
        """When _backend is set, log() should delegate to _backend.log()."""
        mock_backend = MagicMock()
        db_mod._backend = mock_backend

        log("info", "test dual write", symbol="ETH", data={"key": "val"})

        mock_backend.log.assert_called_once_with(
            "info", "test dual write", symbol="ETH", strategy=None, data={"key": "val"}
        )

    def test_log_writes_sqlite_when_no_backend(self):
        """Without dual-write, log() should write directly to SQLite."""
        assert db_mod._backend is None
        log("info", "sqlite only")
        logs = get_recent_logs(10)
        assert any(l.message == "sqlite only" for l in logs)

    def test_insert_position_forwards_to_backend(self):
        """insert_position should delegate to _backend when set."""
        mock_backend = MagicMock()
        db_mod._backend = mock_backend
        p = _make_position()
        insert_position(p)
        mock_backend.insert_position.assert_called_once_with(p)

    def test_snapshot_config_forwards_to_backend(self):
        """snapshot_config should delegate to _backend when set."""
        mock_backend = MagicMock()
        db_mod._backend = mock_backend
        config = ScannerConfig()
        snapshot_config(config, "test reason")
        mock_backend.snapshot_config.assert_called_once_with(config, "test reason")

    def test_insert_diagnosis_forwards_to_backend(self):
        """insert_diagnosis should delegate to _backend when set."""
        mock_backend = MagicMock()
        db_mod._backend = mock_backend
        d = TradeDiagnosis(
            position_id="pos-1", symbol="ETH", strategy="momentum_swing",
            pnl_pct=-0.03, hold_ms=3_600_000, exit_reason="trailing_stop",
            loss_reason="entered_pump_top", entry_qual_score=65,
            market_phase_at_entry="bull", action="raise momentum_pct",
            parameter_changes={}, timestamp=int(time.time() * 1000),
        )
        insert_diagnosis(d)
        mock_backend.insert_diagnosis.assert_called_once_with(d)


# ─── Test 3: Claude prompt includes delta section ────────────────────────────

class TestPromptSections:
    def test_delta_section_empty_when_no_deltas(self):
        """_build_delta_section returns placeholder when no deltas exist."""
        evaluator = DeltaEvaluator()
        with patch("src.self_healing.log_analyzer.get_evaluator", return_value=evaluator):
            section = _build_delta_section()
        assert "no parameter deltas" in section

    def test_delta_section_shows_recorded_deltas(self):
        """_build_delta_section should include delta details."""
        evaluator = DeltaEvaluator()
        delta = ParameterDelta(
            id="d1", parameter="momentum_pct_swing",
            old_value=0.02, new_value=0.03,
            reason="test", source="claude_analysis",
            trades_before=TradeSnapshot(win_rate=0.5, avg_pnl_pct=0.01, count=10),
            verdict="improved", evaluation_status="evaluated",
        )
        evaluator._deltas.append(delta)
        with patch("src.self_healing.log_analyzer.get_evaluator", return_value=evaluator):
            section = _build_delta_section()
        assert "momentum_pct_swing" in section
        assert "0.02" in section
        assert "0.03" in section
        assert "improved" in section

    def test_strategy_health_section_shows_status(self):
        """_build_strategy_health_section should show enabled/disabled strategies."""
        selector = StrategySelector(SelectionConfig())
        # Manually add health entries
        selector._health["momentum_swing"] = StrategyHealth(
            strategy_id="momentum_swing", enabled=True,
            rolling_win_rate=0.55, rolling_sharpe=1.2,
        )
        selector._health["mean_reversion"] = StrategyHealth(
            strategy_id="mean_reversion", enabled=False,
            rolling_win_rate=0.20, rolling_sharpe=-0.8,
            disable_reason="Win rate 20% < 25%",
        )
        section = _build_strategy_health_section(selector)
        assert "momentum_swing: enabled" in section
        assert "mean_reversion: DISABLED" in section
        assert "Win rate 20%" in section

    def test_strategy_health_section_without_selector(self):
        """_build_strategy_health_section should handle None selector."""
        section = _build_strategy_health_section(None)
        assert "not available" in section

    def test_blind_spots_section_when_empty(self):
        """_build_blind_spots_section should show 'none' when no blind spots."""
        detector = BlindSpotDetector()
        with patch("src.self_healing.log_analyzer.get_detector", return_value=detector):
            section = _build_blind_spots_section()
        assert "none" in section.lower()

    def test_blind_spots_section_shows_flagged(self):
        """_build_blind_spots_section should include flagged blind spot details."""
        detector = BlindSpotDetector(BlindSpotConfig(min_occurrences_to_flag=1))
        fp = UnknownFingerprint(
            strategy="momentum_swing", tier="swing",
            market_phase="bull", exit_reason="trailing_stop",
            hold_bucket="1-4h", avg_pnl_pct=-0.025,
            occurrences=5, first_seen=1000, last_seen=2000,
        )
        detector._fingerprints[fp.key] = fp
        with patch("src.self_healing.log_analyzer.get_detector", return_value=detector):
            section = _build_blind_spots_section()
        assert "momentum_swing" in section
        assert "5 occurrences" in section
        assert "1-4h" in section

    @patch("src.self_healing.log_analyzer.get_closed_trades", return_value=[])
    @patch("src.self_healing.log_analyzer.get_recent_diagnoses", return_value=[])
    @patch("src.self_healing.log_analyzer.get_recent_logs", return_value=[])
    @patch("src.self_healing.log_analyzer.compute_metrics")
    @patch("src.self_healing.log_analyzer.format_metrics", return_value="no data")
    def test_full_prompt_includes_all_sections(self, mock_fmt, mock_metrics, *_):
        """The full prompt should include delta, health, and blind spot sections."""
        mock_metrics.return_value = MagicMock(total_trades=0)
        config = ScannerConfig()
        selector = StrategySelector(SelectionConfig())

        prompt = _build_prompt(config, strategy_selector=selector)
        assert "## Parameter Delta Tracking" in prompt
        assert "## Strategy Health (Darwinian Selection)" in prompt
        assert "## Detected Blind Spots" in prompt
        assert "dataSourceSuggestions" in prompt


# ─── Test 4: Analysis model accepts dataSourceSuggestions ─────────────────────

class TestAnalysisModel:
    def test_analysis_with_data_source_suggestions(self):
        """Analysis should parse dataSourceSuggestions field."""
        analysis = Analysis(
            chainOfThought="test",
            summary="test summary",
            topIssues=["issue1"],
            strategyInsights=[],
            parameterChanges=[],
            newStrategySuggestions=[],
            dataSourceSuggestions=[
                DataSourceSuggestion(
                    source="on-chain whale tracking",
                    rationale="Would help predict large sell-offs",
                    priority="high",
                ),
            ],
            overallHealthScore=75,
        )
        assert len(analysis.dataSourceSuggestions) == 1
        assert analysis.dataSourceSuggestions[0].source == "on-chain whale tracking"

    def test_analysis_defaults_empty_data_source_suggestions(self):
        """dataSourceSuggestions should default to empty list."""
        analysis = Analysis(
            chainOfThought="test",
            summary="test summary",
            topIssues=[],
            strategyInsights=[],
            parameterChanges=[],
            newStrategySuggestions=[],
            overallHealthScore=50,
        )
        assert analysis.dataSourceSuggestions == []
