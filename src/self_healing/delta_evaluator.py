"""Track parameter changes and evaluate whether they improved or worsened performance."""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.config import CONFIG_BOUNDS
from src.self_healing.analysis_memory import get_analysis_memory
from src.storage.database import log, get_closed_trades, snapshot_config
from src.types import Position


@dataclass
class TradeSnapshot:
    win_rate: float
    avg_pnl_pct: float
    count: int


@dataclass
class ParameterDelta:
    id: str
    parameter: str
    old_value: float
    new_value: float
    reason: str
    source: str  # "immediate_healer" | "claude_analysis"
    trades_before: TradeSnapshot
    trades_after: Optional[TradeSnapshot] = None
    evaluation_status: str = "pending"  # "pending" | "evaluated" | "reverted"
    verdict: Optional[str] = None  # "improved" | "worsened" | "neutral"
    timestamp: float = field(default_factory=lambda: time.time() * 1000)
    evaluation_timestamp: Optional[float] = None


class DeltaEvaluator:
    """Tracks parameter changes and evaluates their impact on trading performance."""

    MIN_TRADES_FOR_EVAL = 25  # Need at least 25 trades for statistical significance
    WORSENED_WIN_RATE_DROP = 0.08  # 8% win rate drop = worsened (was 5%, too sensitive to noise)
    WORSENED_PNL_DROP = 0.10  # 10% avg PnL drop = worsened
    MAX_REVERTS_PER_CYCLE = 1  # Only revert 1 parameter per evaluation

    def __init__(self):
        self._deltas: list[ParameterDelta] = []
        self._lock = threading.Lock()

    def record_delta(self, parameter: str, old_value: float, new_value: float,
                     reason: str, source: str, config: object) -> ParameterDelta:
        """Record a parameter change with a snapshot of recent performance."""
        recent = get_closed_trades(20)
        before = self._compute_snapshot(recent)

        delta = ParameterDelta(
            id=str(uuid.uuid4()),
            parameter=parameter,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            source=source,
            trades_before=before,
        )
        with self._lock:
            self._deltas.append(delta)

        log("info", f"Delta recorded: {parameter} {old_value} -> {new_value} ({reason})",
            data={"source": source, "before_win_rate": before.win_rate})
        return delta

    def evaluate_pending_deltas(self, config: object) -> list[ParameterDelta]:
        """Evaluate all pending deltas. Auto-revert if worsened. Returns evaluated deltas."""
        evaluated: list[ParameterDelta] = []
        reverts_this_cycle = 0

        with self._lock:
            pending = [d for d in self._deltas if d.evaluation_status == "pending"]

        for delta in pending:
            # Get trades that closed after this delta was recorded
            all_recent = get_closed_trades(100)
            trades_after = [
                t for t in all_recent
                if t.closed_at is not None and t.closed_at > delta.timestamp
            ]

            if len(trades_after) < self.MIN_TRADES_FOR_EVAL:
                continue

            after = self._compute_snapshot(trades_after)
            delta.trades_after = after
            delta.evaluation_timestamp = time.time() * 1000

            # Determine verdict
            before = delta.trades_before
            win_rate_change = after.win_rate - before.win_rate
            pnl_change = after.avg_pnl_pct - before.avg_pnl_pct

            if (win_rate_change < -self.WORSENED_WIN_RATE_DROP
                    or pnl_change < -self.WORSENED_PNL_DROP):
                delta.verdict = "worsened"
                if reverts_this_cycle < self.MAX_REVERTS_PER_CYCLE:
                    self._revert_parameter(delta, config)
                    delta.evaluation_status = "reverted"
                    reverts_this_cycle += 1
                    log("warn",
                        f"Delta REVERTED: {delta.parameter} {delta.new_value} -> {delta.old_value} "
                        f"(win_rate {before.win_rate:.1%} -> {after.win_rate:.1%}, "
                        f"avg_pnl {before.avg_pnl_pct:.2%} -> {after.avg_pnl_pct:.2%})",
                        data={"delta_id": delta.id, "parameter": delta.parameter})
                else:
                    delta.evaluation_status = "evaluated"
                    log("warn",
                        f"Delta WORSENED but revert cap reached: {delta.parameter} "
                        f"(win_rate {before.win_rate:.1%} -> {after.win_rate:.1%})",
                        data={"delta_id": delta.id, "parameter": delta.parameter})
            elif (win_rate_change > 0.02 and pnl_change > 0):
                # "improved" requires BOTH win rate increase AND positive PnL change
                delta.verdict = "improved"
                delta.evaluation_status = "evaluated"
                log("info",
                    f"Delta IMPROVED: {delta.parameter} "
                    f"(win_rate {before.win_rate:.1%} -> {after.win_rate:.1%}, "
                    f"avg_pnl {before.avg_pnl_pct:.2%} -> {after.avg_pnl_pct:.2%})",
                    data={"delta_id": delta.id, "parameter": delta.parameter})
            else:
                delta.verdict = "neutral"
                delta.evaluation_status = "evaluated"
                log("info",
                    f"Delta NEUTRAL: {delta.parameter} "
                    f"(win_rate {before.win_rate:.1%} -> {after.win_rate:.1%})",
                    data={"delta_id": delta.id, "parameter": delta.parameter})

            evaluated.append(delta)

            # Reinforce analysis memory based on delta verdict
            try:
                memory = get_analysis_memory()
                profitable = delta.verdict == "improved"
                memory.reinforce(delta.parameter, profitable)
                if delta.reason:
                    # Also reinforce insights matching the change reason
                    for word in delta.reason.split()[:5]:
                        if len(word) > 4:
                            memory.reinforce(word, profitable)
            except Exception as err:
                log("warn", f"Memory reinforcement failed for {delta.parameter}: {err}")

        return evaluated

    def get_pending_deltas(self) -> list[ParameterDelta]:
        with self._lock:
            return [d for d in self._deltas if d.evaluation_status == "pending"]

    def get_all_deltas(self) -> list[ParameterDelta]:
        with self._lock:
            return list(self._deltas)

    def _compute_snapshot(self, trades: list[Position]) -> TradeSnapshot:
        if not trades:
            return TradeSnapshot(win_rate=0.0, avg_pnl_pct=0.0, count=0)

        wins = sum(1 for t in trades if (t.pnl_pct or 0) > 0)
        avg_pnl = sum(t.pnl_pct or 0 for t in trades) / len(trades)

        return TradeSnapshot(
            win_rate=wins / len(trades),
            avg_pnl_pct=avg_pnl,
            count=len(trades),
        )

    def _revert_parameter(self, delta: ParameterDelta, config: object) -> None:
        """Revert a parameter change. Respect CONFIG_BOUNDS."""
        bounds = CONFIG_BOUNDS.get(delta.parameter)
        if not bounds:
            log("warn", f"Cannot revert {delta.parameter} — not in CONFIG_BOUNDS")
            return

        lo, hi = bounds
        reverted_value = min(hi, max(lo, delta.old_value))
        setattr(config, delta.parameter, reverted_value)
        snapshot_config(config, f"delta-revert: {delta.parameter} {delta.new_value} -> {reverted_value}")


# Module singleton (double-checked lock pattern)
_evaluator: Optional[DeltaEvaluator] = None
_evaluator_lock = threading.Lock()


def get_evaluator() -> DeltaEvaluator:
    global _evaluator
    if _evaluator is None:
        with _evaluator_lock:
            if _evaluator is None:
                _evaluator = DeltaEvaluator()
    return _evaluator
