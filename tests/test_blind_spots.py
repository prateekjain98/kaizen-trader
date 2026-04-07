"""Tests for blind spot detection — recurring unclassified loss patterns."""

import time
import pytest

from src.self_healing.blind_spots import (
    BlindSpotDetector, BlindSpotConfig, UnknownFingerprint,
    _hold_bucket, _fingerprint_key,
)
from src.types import TradeDiagnosis


def _make_diagnosis(
    strategy="momentum_swing", market_phase="neutral",
    exit_reason="trailing_stop", hold_ms=5 * 3_600_000,
    pnl_pct=-0.02, position_id="pos-1",
) -> TradeDiagnosis:
    return TradeDiagnosis(
        position_id=position_id, symbol="ETH", strategy=strategy,
        pnl_pct=pnl_pct, hold_ms=hold_ms, exit_reason=exit_reason,
        loss_reason="unknown", entry_qual_score=65,
        market_phase_at_entry=market_phase,
        action="no change", parameter_changes={},
        timestamp=time.time() * 1000,
    )


# ── Hold bucket classification ────────────────────────────────────────────

class TestHoldBucket:
    def test_under_1h(self):
        assert _hold_bucket(30 * 60_000) == "<1h"

    def test_1_to_4h(self):
        assert _hold_bucket(2 * 3_600_000) == "1-4h"

    def test_4_to_12h(self):
        assert _hold_bucket(6 * 3_600_000) == "4-12h"

    def test_12_to_24h(self):
        assert _hold_bucket(18 * 3_600_000) == "12-24h"

    def test_over_24h(self):
        assert _hold_bucket(30 * 3_600_000) == ">24h"

    def test_exact_boundary_1h(self):
        assert _hold_bucket(1 * 3_600_000) == "1-4h"

    def test_exact_boundary_4h(self):
        assert _hold_bucket(4 * 3_600_000) == "4-12h"


# ── Fingerprint key ───────────────────────────────────────────────────────

class TestFingerprintKey:
    def test_consistent(self):
        key1 = _fingerprint_key("momentum_swing", "swing", "neutral", "trailing_stop", "4-12h")
        key2 = _fingerprint_key("momentum_swing", "swing", "neutral", "trailing_stop", "4-12h")
        assert key1 == key2

    def test_different_strategy(self):
        key1 = _fingerprint_key("momentum_swing", "swing", "neutral", "trailing_stop", "4-12h")
        key2 = _fingerprint_key("mean_reversion", "swing", "neutral", "trailing_stop", "4-12h")
        assert key1 != key2


# ── BlindSpotDetector ─────────────────────────────────────────────────────

class TestBlindSpotDetector:
    def test_first_occurrence_returns_none(self):
        det = BlindSpotDetector()
        result = det.record_unknown(_make_diagnosis(position_id="p1"))
        assert result is None

    def test_second_occurrence_returns_none(self):
        det = BlindSpotDetector()
        det.record_unknown(_make_diagnosis(position_id="p1"))
        result = det.record_unknown(_make_diagnosis(position_id="p2"))
        assert result is None

    def test_third_occurrence_returns_fingerprint(self):
        det = BlindSpotDetector()
        det.record_unknown(_make_diagnosis(position_id="p1"))
        det.record_unknown(_make_diagnosis(position_id="p2"))
        result = det.record_unknown(_make_diagnosis(position_id="p3"))
        assert result is not None
        assert result.occurrences == 3
        assert len(result.position_ids) == 3

    def test_fourth_occurrence_returns_none_again(self):
        det = BlindSpotDetector()
        for i in range(3):
            det.record_unknown(_make_diagnosis(position_id=f"p{i}"))
        # 4th time: already flagged, should return None
        result = det.record_unknown(_make_diagnosis(position_id="p4"))
        assert result is None

    def test_custom_threshold(self):
        det = BlindSpotDetector(BlindSpotConfig(min_occurrences_to_flag=5))
        for i in range(4):
            result = det.record_unknown(_make_diagnosis(position_id=f"p{i}"))
            assert result is None
        result = det.record_unknown(_make_diagnosis(position_id="p5"))
        assert result is not None
        assert result.occurrences == 5

    def test_different_strategies_tracked_separately(self):
        det = BlindSpotDetector()
        for i in range(3):
            det.record_unknown(_make_diagnosis(strategy="momentum_swing", position_id=f"m{i}"))
        for i in range(2):
            det.record_unknown(_make_diagnosis(strategy="mean_reversion", position_id=f"r{i}"))
        flagged = det.get_flagged_blind_spots()
        assert len(flagged) == 1
        assert flagged[0].strategy == "momentum_swing"

    def test_different_hold_buckets_tracked_separately(self):
        det = BlindSpotDetector()
        for i in range(3):
            det.record_unknown(_make_diagnosis(hold_ms=2 * 3_600_000, position_id=f"s{i}"))
        for i in range(2):
            det.record_unknown(_make_diagnosis(hold_ms=8 * 3_600_000, position_id=f"l{i}"))
        flagged = det.get_flagged_blind_spots()
        assert len(flagged) == 1

    def test_avg_pnl_tracks_correctly(self):
        det = BlindSpotDetector()
        det.record_unknown(_make_diagnosis(pnl_pct=-0.01, position_id="p1"))
        det.record_unknown(_make_diagnosis(pnl_pct=-0.03, position_id="p2"))
        det.record_unknown(_make_diagnosis(pnl_pct=-0.05, position_id="p3"))
        flagged = det.get_flagged_blind_spots()
        assert len(flagged) == 1
        assert abs(flagged[0].avg_pnl_pct - (-0.03)) < 1e-10

    def test_promote_removes_from_flagged(self):
        det = BlindSpotDetector()
        for i in range(3):
            det.record_unknown(_make_diagnosis(position_id=f"p{i}"))
        flagged = det.get_flagged_blind_spots()
        assert len(flagged) == 1
        det.promote_to_loss_reason(flagged[0].key, "custom:new_pattern")
        assert len(det.get_flagged_blind_spots()) == 0

    def test_lookup_promoted(self):
        det = BlindSpotDetector()
        for i in range(3):
            det.record_unknown(_make_diagnosis(position_id=f"p{i}"))
        flagged = det.get_flagged_blind_spots()
        det.promote_to_loss_reason(flagged[0].key, "custom:swing_drift")
        result = det.lookup_promoted(
            "momentum_swing", "neutral", "trailing_stop", 5 * 3_600_000,
        )
        assert result == "custom:swing_drift"

    def test_lookup_promoted_no_match(self):
        det = BlindSpotDetector()
        result = det.lookup_promoted("unknown_strat", "neutral", "trailing_stop", 3_600_000)
        assert result is None

    def test_reset_clears_all(self):
        det = BlindSpotDetector()
        for i in range(3):
            det.record_unknown(_make_diagnosis(position_id=f"p{i}"))
        det.reset()
        assert len(det.get_flagged_blind_spots()) == 0
