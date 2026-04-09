"""Darwinian strategy selection — auto-disable underperformers, cull chronic losers."""

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from src.automation.github_issues import create_chronic_underperformer_issue
from src.evaluation.metrics import _mean, _std_dev, _max_consecutive_losses
from src.types import Position


@dataclass
class StrategyHealth:
    strategy_id: str
    enabled: bool = True
    rolling_sharpe: Optional[float] = None
    rolling_win_rate: float = 0.0
    consecutive_losses: int = 0
    disabled_at: Optional[float] = None
    disable_reason: Optional[str] = None
    trades_since_enable: int = 0
    last_evaluated_at: float = 0


@dataclass
class SelectionConfig:
    rolling_window_trades: int = 30
    min_sharpe_threshold: float = -0.5
    min_win_rate: float = 0.25
    max_consecutive_losses: int = 8
    probation_days: int = 7
    cull_after_days: int = 14
    min_trades_before_eval: int = 10


class StrategySelector:
    def __init__(self, config: SelectionConfig = SelectionConfig()):
        self.config = config
        self._lock = threading.Lock()
        self._health: dict[str, StrategyHealth] = {}

    def _get_or_create(self, strategy_id: str) -> StrategyHealth:
        if strategy_id not in self._health:
            self._health[strategy_id] = StrategyHealth(
                strategy_id=strategy_id,
                last_evaluated_at=time.time() * 1000,
            )
        return self._health[strategy_id]

    def is_strategy_enabled(self, strategy_id: str) -> bool:
        with self._lock:
            h = self._health.get(strategy_id)
            if h is None:
                return True  # unknown strategies are allowed by default
            return h.enabled

    def on_trade_closed(self, position: Position) -> None:
        """Update consecutive loss counter in real-time."""
        with self._lock:
            h = self._get_or_create(position.strategy)
            h.trades_since_enable += 1
            pnl = position.pnl_pct or 0
            if pnl < 0:
                h.consecutive_losses += 1
                if h.consecutive_losses >= self.config.max_consecutive_losses and h.enabled:
                    h.enabled = False
                    h.disabled_at = time.time() * 1000
                    h.disable_reason = f"{h.consecutive_losses} consecutive losses"
            else:
                h.consecutive_losses = 0

    def evaluate_strategies(self, closed_positions: list[Position]) -> list[StrategyHealth]:
        """Periodic evaluation using recent closed positions.

        Groups positions by strategy, computes rolling metrics, and
        disables/re-enables strategies based on thresholds.
        """
        now = time.time() * 1000

        # Group by strategy
        by_strategy: dict[str, list[Position]] = {}
        for p in closed_positions:
            by_strategy.setdefault(p.strategy, []).append(p)

        with self._lock:
            for strategy_id, positions in by_strategy.items():
                h = self._get_or_create(strategy_id)
                h.last_evaluated_at = now

                # Take only the rolling window
                recent = positions[:self.config.rolling_window_trades]

                if len(recent) < self.config.min_trades_before_eval:
                    continue  # not enough data to judge

                pnl_pcts = [p.pnl_pct or 0 for p in recent]
                wins = [p for p in pnl_pcts if p > 0]

                h.rolling_win_rate = len(wins) / len(recent)
                h.consecutive_losses = _max_consecutive_losses(pnl_pcts)

                # Compute rolling Sharpe
                mean_pnl = _mean(pnl_pcts)
                std_pnl = _std_dev(pnl_pcts, mean_pnl)
                h.rolling_sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else None

                # Disable checks
                if h.enabled:
                    reasons = []
                    if h.rolling_sharpe is not None and h.rolling_sharpe < self.config.min_sharpe_threshold:
                        reasons.append(f"Sharpe {h.rolling_sharpe:.2f} < {self.config.min_sharpe_threshold}")
                    if h.rolling_win_rate < self.config.min_win_rate:
                        reasons.append(f"Win rate {h.rolling_win_rate*100:.0f}% < {self.config.min_win_rate*100:.0f}%")

                    if reasons:
                        h.enabled = False
                        h.disabled_at = now
                        h.disable_reason = "; ".join(reasons)

                # Re-enable check: if disabled and now above thresholds
                elif h.disabled_at:
                    days_disabled = (now - h.disabled_at) / (86_400_000)
                    sharpe_ok = h.rolling_sharpe is None or h.rolling_sharpe >= 0.3
                    winrate_ok = h.rolling_win_rate >= self.config.min_win_rate * 1.5

                    if sharpe_ok and winrate_ok and days_disabled >= self.config.probation_days:
                        h.enabled = True
                        h.disabled_at = None
                        h.disable_reason = None
                        h.trades_since_enable = 0

            # Collect chronic underperformers (disabled > cull_after_days)
            chronic_candidates = []
            for h in self._health.values():
                if not h.enabled and h.disabled_at:
                    days_off = (now - h.disabled_at) / 86_400_000
                    if days_off >= self.config.cull_after_days:
                        chronic_candidates.append((
                            h.strategy_id,
                            int(days_off),
                            h.rolling_win_rate * 100,
                            h.rolling_sharpe or 0.0,
                            h.consecutive_losses,
                        ))

            result = list(self._health.values())

        # Create issues outside the lock to avoid deadlocks with DB logging
        for sid, days_off, wr, sharpe, closs in chronic_candidates:
            create_chronic_underperformer_issue(
                strategy_id=sid,
                days_disabled=days_off,
                win_rate=wr,
                sharpe=sharpe,
                consecutive_losses=closs,
            )

        return result

    def force_enable(self, strategy_id: str) -> None:
        with self._lock:
            h = self._get_or_create(strategy_id)
            h.enabled = True
            h.disabled_at = None
            h.disable_reason = None
            h.trades_since_enable = 0

    def get_health_report(self) -> list[StrategyHealth]:
        with self._lock:
            return list(self._health.values())
