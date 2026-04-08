"""Tests for the enhanced logging and AI evaluation pipeline."""

import json
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.storage.database import (
    log, insert_position, insert_trade, insert_diagnosis,
    snapshot_config, get_recent_logs,
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


# ─── Test 1: database facade delegates to ConvexStorage ──────────────────────

class TestDatabaseDelegation:
    def test_log_delegates_to_storage(self, _mock_convex_storage):
        """log() should delegate to the ConvexStorage instance."""
        log("info", "test message", symbol="ETH", data={"key": "val"})
        _mock_convex_storage.log.assert_called_once_with(
            "info", "test message", symbol="ETH", strategy=None, data={"key": "val"}
        )

    def test_insert_position_delegates_to_storage(self, _mock_convex_storage):
        """insert_position should delegate to ConvexStorage."""
        p = _make_position()
        insert_position(p)
        _mock_convex_storage.insert_position.assert_called_once_with(p)

    def test_snapshot_config_delegates_to_storage(self, _mock_convex_storage):
        """snapshot_config should delegate to ConvexStorage."""
        config = ScannerConfig()
        snapshot_config(config, "test reason")
        _mock_convex_storage.snapshot_config.assert_called_once_with(config, "test reason")

    def test_insert_diagnosis_delegates_to_storage(self, _mock_convex_storage):
        """insert_diagnosis should delegate to ConvexStorage."""
        d = TradeDiagnosis(
            position_id="pos-1", symbol="ETH", strategy="momentum_swing",
            pnl_pct=-0.03, hold_ms=3_600_000, exit_reason="trailing_stop",
            loss_reason="entered_pump_top", entry_qual_score=65,
            market_phase_at_entry="bull", action="raise momentum_pct",
            parameter_changes={}, timestamp=int(time.time() * 1000),
        )
        insert_diagnosis(d)
        _mock_convex_storage.insert_diagnosis.assert_called_once_with(d)


# ─── Test 2: Claude prompt includes delta section ────────────────────────────

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


# ─── Test 3: Analysis model accepts dataSourceSuggestions ─────────────────────

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
