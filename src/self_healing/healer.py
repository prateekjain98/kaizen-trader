"""Self-Healing Engine — diagnoses losses and patches parameters."""

import dataclasses
import threading
import time

from src.config import CONFIG_BOUNDS
from src.self_healing.blind_spots import get_detector
from src.self_healing.delta_evaluator import get_evaluator
from src.storage.database import insert_diagnosis, snapshot_config, log
from src.types import Position, ScannerConfig, TradeDiagnosis

_MAX_ADAPTATIONS_PER_SESSION = 20
_lock = threading.Lock()
_adaptation_count = 0


def _clamp(value: float, key: str) -> float:
    lo, hi = CONFIG_BOUNDS[key]
    return min(hi, max(lo, value))


def _adjust(config: ScannerConfig, key: str, delta: float) -> None:
    current = getattr(config, key)
    setattr(config, key, _clamp(current + delta, key))


def _classify_loss_reason(p: Position) -> str:
    # Skip diagnosis if position was never properly closed
    if p.closed_at is None:
        return "unknown"

    hold_hours = (p.closed_at - p.opened_at) / 3_600_000
    pnl_pct = p.pnl_pct or 0

    # Use stored momentum at entry time (frozen at open), not low_watermark
    # which tracks worst price during hold and gives false "pump top" diagnoses
    momentum_at_entry = getattr(p, "momentum_at_entry", 0.0) or 0.0

    if momentum_at_entry > 0.08 and hold_hours < 4:
        return "entered_pump_top"
    if hold_hours < 2 and p.exit_reason == "trailing_stop":
        return "stop_too_tight"
    if hold_hours > 20 and pnl_pct < -0.05:
        return "stop_too_wide"
    if p.qual_score < 55:
        return "low_qual_score"
    if p.strategy == "funding_extreme":
        return "funding_squeeze"

    # Check promoted blind spots before returning unknown
    hold_ms = ((p.closed_at or time.time() * 1000) - p.opened_at)
    promoted = get_detector().lookup_promoted(
        p.strategy, "", p.exit_reason or "", hold_ms,
    )
    if promoted:
        return promoted

    return "unknown"


def _apply_loss_adaptation(p: Position, reason: str, config: ScannerConfig) -> dict:
    changes: dict = {}
    action = "no change"

    old_values: dict = {}

    if reason == "entered_pump_top":
        key = "momentum_pct_swing" if p.tier == "swing" else "momentum_pct_scalp"
        old_val = getattr(config, key)
        _adjust(config, key, 0.01)
        new_val = getattr(config, key)
        old_values[key] = old_val
        changes[key] = new_val
        action = f"raise {key} {old_val*100:.1f}% -> {new_val*100:.1f}%"

    elif reason == "stop_too_tight":
        key = "base_trail_pct_swing" if p.tier == "swing" else "base_trail_pct_scalp"
        old_val = getattr(config, key)
        _adjust(config, key, 0.01)
        new_val = getattr(config, key)
        old_values[key] = old_val
        changes[key] = new_val
        action = f"widen {key} {old_val*100:.0f}% -> {new_val*100:.0f}%"

    elif reason == "stop_too_wide":
        key = "base_trail_pct_swing" if p.tier == "swing" else "base_trail_pct_scalp"
        old_val = getattr(config, key)
        _adjust(config, key, -0.01)
        new_val = getattr(config, key)
        old_values[key] = old_val
        changes[key] = new_val
        action = f"tighten {key} {old_val*100:.0f}% -> {new_val*100:.0f}%"

    elif reason == "low_qual_score":
        key = "min_qual_score_swing" if p.tier == "swing" else "min_qual_score_scalp"
        old_val = getattr(config, key)
        _adjust(config, key, 2)
        new_val = getattr(config, key)
        old_values[key] = old_val
        changes[key] = new_val
        action = f"raise {key} {old_val} -> {new_val}"

    elif reason == "funding_squeeze":
        old_val = config.funding_rate_extreme_threshold
        _adjust(config, "funding_rate_extreme_threshold", -0.0001)
        new_val = config.funding_rate_extreme_threshold
        old_values["funding_rate_extreme_threshold"] = old_val
        changes["funding_rate_extreme_threshold"] = new_val
        action = f"lower funding threshold {old_val*100:.3f}% -> {new_val*100:.3f}%"

    else:
        action = "no change — unknown loss reason"

    return {"action": action, "changes": changes, "old_values": old_values}


def on_position_closed(p: Position, config: ScannerConfig, market_phase: str) -> None:
    global _adaptation_count
    pnl_pct = p.pnl_pct or 0
    is_loss = pnl_pct < -0.005

    if not is_loss:
        log("heal", f"{p.symbol} WIN +{pnl_pct*100:.1f}% — no parameter changes",
            symbol=p.symbol, strategy=p.strategy)
        return

    with _lock:
        if _adaptation_count >= _MAX_ADAPTATIONS_PER_SESSION:
            log("warn", f"Self-healer hit session cap ({_MAX_ADAPTATIONS_PER_SESSION}) — skipping",
                symbol=p.symbol)
            return
        _adaptation_count += 1

    loss_reason = _classify_loss_reason(p)
    hold_ms = (p.closed_at or time.time() * 1000) - p.opened_at
    result = _apply_loss_adaptation(p, loss_reason, config)

    diagnosis = TradeDiagnosis(
        position_id=p.id, symbol=p.symbol, strategy=p.strategy,
        pnl_pct=pnl_pct, hold_ms=hold_ms,
        exit_reason=p.exit_reason or "error",
        loss_reason=loss_reason, entry_qual_score=p.qual_score,
        market_phase_at_entry=market_phase,
        action=result["action"], parameter_changes=result["changes"],
        timestamp=time.time() * 1000,
    )

    insert_diagnosis(diagnosis)
    snapshot_config(config, f"self-healer: {result['action']}")

    # Record deltas for tracking and auto-revert evaluation
    for key, new_val in result["changes"].items():
        old_val = result["old_values"].get(key)
        if old_val is not None and old_val != new_val:
            get_evaluator().record_delta(
                parameter=key, old_value=old_val, new_value=new_val,
                reason=loss_reason, source="immediate_healer", config=config,
            )

    log("heal",
        f"{p.symbol} LOSS {pnl_pct*100:.1f}% reason={loss_reason} -> {result['action']}",
        symbol=p.symbol, strategy=p.strategy,
        data={"loss_reason": loss_reason, "action": result["action"], "changes": result["changes"]})

    # Blind spot detection: track recurring unknown losses
    if loss_reason == "unknown":
        flagged = get_detector().record_unknown(diagnosis)
        if flagged:
            log("warn",
                f"BLIND SPOT DETECTED: {flagged.key} seen {flagged.occurrences} times — "
                f"avg loss {flagged.avg_pnl_pct*100:.1f}%",
                symbol=p.symbol, strategy=p.strategy,
                data={"blind_spot": flagged.key, "occurrences": flagged.occurrences,
                      "position_ids": flagged.position_ids})


def reset_session_count() -> None:
    global _adaptation_count
    with _lock:
        _adaptation_count = 0
