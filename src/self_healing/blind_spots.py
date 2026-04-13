"""Blind spot detection — identifies recurring unclassified loss patterns."""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from src.types import TradeDiagnosis, SCALP_STRATEGY_IDS
from src.automation.github_issues import create_blind_spot_issue


def _infer_tier(strategy: str) -> str:
    return "scalp" if strategy in SCALP_STRATEGY_IDS else "swing"


def _hold_bucket(hold_ms: float) -> str:
    # Heuristic: if value is suspiciously small, it's likely in seconds not milliseconds
    if 0 < hold_ms < 1000:
        hold_ms *= 1000
    hours = hold_ms / 3_600_000
    if hours < 1:
        return "<1h"
    if hours < 4:
        return "1-4h"
    if hours < 12:
        return "4-12h"
    if hours < 24:
        return "12-24h"
    return ">24h"


def _fingerprint_key(strategy: str, tier: str, market_phase: str,
                     exit_reason: str, hold_bucket: str) -> str:
    return f"{strategy}|{tier}|{market_phase}|{exit_reason}|{hold_bucket}"


@dataclass
class UnknownFingerprint:
    strategy: str
    tier: str
    market_phase: str
    exit_reason: str
    hold_bucket: str
    avg_pnl_pct: float
    occurrences: int
    first_seen: float
    last_seen: float
    position_ids: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return _fingerprint_key(
            self.strategy, self.tier, self.market_phase,
            self.exit_reason, self.hold_bucket,
        )


@dataclass
class BlindSpotConfig:
    min_occurrences_to_flag: int = 3


class BlindSpotDetector:
    def __init__(self, config: BlindSpotConfig = BlindSpotConfig()):
        self.config = config
        self._lock = threading.Lock()
        self._fingerprints: dict[str, UnknownFingerprint] = {}
        self._promoted: dict[str, str] = {}  # fingerprint_key -> custom loss reason

    def record_unknown(self, diagnosis: TradeDiagnosis) -> Optional[UnknownFingerprint]:
        """Record an 'unknown' diagnosis. Returns the fingerprint if it just crossed the threshold."""
        tier = _infer_tier(diagnosis.strategy)
        bucket = _hold_bucket(diagnosis.hold_ms)
        key = _fingerprint_key(
            diagnosis.strategy,
            tier,
            diagnosis.market_phase_at_entry,
            diagnosis.exit_reason,
            bucket,
        )

        now = time.time() * 1000
        with self._lock:
            fp = self._fingerprints.get(key)

            if fp is None:
                fp = UnknownFingerprint(
                    strategy=diagnosis.strategy,
                    tier=tier,
                    market_phase=diagnosis.market_phase_at_entry,
                    exit_reason=diagnosis.exit_reason,
                    hold_bucket=bucket,
                    avg_pnl_pct=diagnosis.pnl_pct,
                    occurrences=1,
                    first_seen=now,
                    last_seen=now,
                    position_ids=[diagnosis.position_id],
                )
                self._fingerprints[key] = fp
                return None

            # Update existing fingerprint
            fp.occurrences += 1
            fp.last_seen = now
            fp.position_ids.append(diagnosis.position_id)
            # Running average of pnl_pct
            fp.avg_pnl_pct = (
                (fp.avg_pnl_pct * (fp.occurrences - 1) + diagnosis.pnl_pct)
                / fp.occurrences
            )

            just_crossed = fp.occurrences == self.config.min_occurrences_to_flag
            crossed_fp = fp if just_crossed else None

        # Create GitHub issue OUTSIDE the lock to avoid blocking for up to 30s
        if crossed_fp is not None:
            create_blind_spot_issue(
                fingerprint_key=crossed_fp.key,
                occurrences=crossed_fp.occurrences,
                avg_loss_pct=crossed_fp.avg_pnl_pct * 100,
                affected_strategies=[crossed_fp.strategy],
            )
            return crossed_fp
        return None

    def get_flagged_blind_spots(self) -> list[UnknownFingerprint]:
        """Return all fingerprints that have crossed the threshold."""
        with self._lock:
            return [
                fp for fp in self._fingerprints.values()
                if fp.occurrences >= self.config.min_occurrences_to_flag
                and fp.key not in self._promoted
            ]

    def promote_to_loss_reason(self, fingerprint_key: str, reason_name: str) -> None:
        """Register a custom loss reason from a blind spot."""
        with self._lock:
            self._promoted[fingerprint_key] = reason_name

    def lookup_promoted(self, strategy: str, market_phase: str,
                        exit_reason: str, hold_ms: float) -> Optional[str]:
        """Check if a promoted blind spot matches. Called before returning 'unknown'."""
        bucket = _hold_bucket(hold_ms)
        tier = _infer_tier(strategy)
        key = _fingerprint_key(strategy, tier, market_phase, exit_reason, bucket)
        with self._lock:
            return self._promoted.get(key)

    def reset(self) -> None:
        with self._lock:
            self._fingerprints.clear()
            self._promoted.clear()


# Module-level singleton
_detector = BlindSpotDetector()


def get_detector() -> BlindSpotDetector:
    return _detector
