"""Declarative, stackable protection rules — inspired by Freqtrade."""

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from src.types import Position


@dataclass
class ProtectionVerdict:
    allowed: bool
    rule_name: str = ""
    reason: str = ""


@dataclass
class ProtectionContext:
    realized_pnl_today: float
    open_position_count: int
    timestamp_ms: float


class ProtectionRule(ABC):
    """Base class for stackable protection rules."""

    name: str = "base"

    @abstractmethod
    def check(self, ctx: ProtectionContext) -> ProtectionVerdict:
        ...

    def on_trade_closed(self, position: Position, pnl_usd: float) -> None:
        """Hook called when a trade closes. Override to update internal state."""
        pass

    def on_day_reset(self) -> None:
        """Hook called at UTC midnight. Override to clear daily state."""
        pass


# ── Concrete rules ────────────────────────────────────────────────────────


class MaxDailyLossGuard(ProtectionRule):
    """Block trading when daily realized loss exceeds a threshold."""

    name = "max_daily_loss"

    def __init__(self, max_daily_loss_usd: float = 300):
        self.max_daily_loss_usd = max_daily_loss_usd

    def check(self, ctx: ProtectionContext) -> ProtectionVerdict:
        if ctx.realized_pnl_today < -self.max_daily_loss_usd:
            return ProtectionVerdict(
                allowed=False, rule_name=self.name,
                reason=f"Daily loss ${-ctx.realized_pnl_today:.2f} exceeds ${self.max_daily_loss_usd}",
            )
        return ProtectionVerdict(allowed=True)


class MaxOpenPositionsGuard(ProtectionRule):
    """Block when the number of open positions reaches a cap."""

    name = "max_open_positions"

    def __init__(self, max_open_positions: int = 5):
        self.max_open_positions = max_open_positions

    def check(self, ctx: ProtectionContext) -> ProtectionVerdict:
        if ctx.open_position_count >= self.max_open_positions:
            return ProtectionVerdict(
                allowed=False, rule_name=self.name,
                reason=f"Position cap reached ({ctx.open_position_count}/{self.max_open_positions})",
            )
        return ProtectionVerdict(allowed=True)


class StoplossGuard(ProtectionRule):
    """Block after N consecutive stop-loss exits within a lookback window."""

    name = "stoploss_guard"

    def __init__(self, max_consecutive_stops: int = 3, lookback_ms: float = 3_600_000):
        self.max_consecutive_stops = max_consecutive_stops
        self.lookback_ms = lookback_ms
        self._recent_stops: deque[float] = deque()  # timestamps of stop-loss exits
        self._consecutive = 0

    def on_trade_closed(self, position: Position, pnl_usd: float) -> None:
        if position.exit_reason == "trailing_stop" and pnl_usd < 0:
            self._consecutive += 1
            self._recent_stops.append(time.time() * 1000)
        else:
            self._consecutive = 0

    def check(self, ctx: ProtectionContext) -> ProtectionVerdict:
        # Prune old entries
        cutoff = ctx.timestamp_ms - self.lookback_ms
        while self._recent_stops and self._recent_stops[0] < cutoff:
            self._recent_stops.popleft()

        if self._consecutive >= self.max_consecutive_stops:
            return ProtectionVerdict(
                allowed=False, rule_name=self.name,
                reason=f"{self._consecutive} consecutive stop-losses in lookback window",
            )
        return ProtectionVerdict(allowed=True)

    def on_day_reset(self) -> None:
        self._consecutive = 0
        self._recent_stops.clear()


class MaxDrawdownGuard(ProtectionRule):
    """Block when daily drawdown exceeds a threshold percentage."""

    name = "max_drawdown"

    def __init__(self, max_drawdown_pct: float = 0.15):
        self.max_drawdown_pct = max_drawdown_pct
        self._peak_equity: float = 0
        self._current_equity: float = 0

    def on_trade_closed(self, position: Position, pnl_usd: float) -> None:
        self._current_equity += pnl_usd
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity

    def check(self, ctx: ProtectionContext) -> ProtectionVerdict:
        if self._peak_equity > 0:
            dd = (self._peak_equity - self._current_equity) / self._peak_equity
            if dd > self.max_drawdown_pct:
                return ProtectionVerdict(
                    allowed=False, rule_name=self.name,
                    reason=f"Drawdown {dd*100:.1f}% exceeds {self.max_drawdown_pct*100:.0f}%",
                )
        return ProtectionVerdict(allowed=True)

    def on_day_reset(self) -> None:
        self._peak_equity = 0
        self._current_equity = 0


class CooldownPeriod(ProtectionRule):
    """Enforce a cooldown period after N consecutive losses."""

    name = "cooldown"

    def __init__(self, cooldown_ms: float = 1_800_000, trigger_after_n_losses: int = 4):
        self.cooldown_ms = cooldown_ms
        self.trigger_after_n_losses = trigger_after_n_losses
        self._consecutive_losses = 0
        self._cooldown_until: float = 0

    def on_trade_closed(self, position: Position, pnl_usd: float) -> None:
        if pnl_usd < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.trigger_after_n_losses:
                self._cooldown_until = time.time() * 1000 + self.cooldown_ms
        else:
            self._consecutive_losses = 0

    def check(self, ctx: ProtectionContext) -> ProtectionVerdict:
        if ctx.timestamp_ms < self._cooldown_until:
            remaining_s = (self._cooldown_until - ctx.timestamp_ms) / 1000
            return ProtectionVerdict(
                allowed=False, rule_name=self.name,
                reason=f"Cooldown active — {remaining_s:.0f}s remaining after {self.trigger_after_n_losses} consecutive losses",
            )
        return ProtectionVerdict(allowed=True)

    def on_day_reset(self) -> None:
        self._consecutive_losses = 0


# ── Protection Chain ──────────────────────────────────────────────────────


_RULE_TYPES: dict[str, type] = {
    "max_daily_loss": MaxDailyLossGuard,
    "max_open_positions": MaxOpenPositionsGuard,
    "stoploss_guard": StoplossGuard,
    "max_drawdown": MaxDrawdownGuard,
    "cooldown": CooldownPeriod,
}


class ProtectionChain:
    """Evaluates a list of ProtectionRule instances. First block short-circuits."""

    def __init__(self, rules: list[ProtectionRule]):
        self.rules = rules

    def can_open(self, ctx: ProtectionContext) -> ProtectionVerdict:
        for rule in self.rules:
            verdict = rule.check(ctx)
            if not verdict.allowed:
                return verdict
        return ProtectionVerdict(allowed=True)

    def notify_close(self, position: Position, pnl_usd: float) -> None:
        for rule in self.rules:
            rule.on_trade_closed(position, pnl_usd)

    def reset_day(self) -> None:
        for rule in self.rules:
            rule.on_day_reset()

    @classmethod
    def from_config(cls, config_list: list[dict]) -> "ProtectionChain":
        """Build a chain from a list of dicts like:
        [{"rule_type": "max_daily_loss", "enabled": True, "params": {"max_daily_loss_usd": 300}}]
        """
        rules: list[ProtectionRule] = []
        for entry in config_list:
            if not entry.get("enabled", True):
                continue
            rule_type = entry["rule_type"]
            if rule_type not in _RULE_TYPES:
                raise ValueError(f"Unknown protection rule type: {rule_type}")
            params = entry.get("params", {})
            rules.append(_RULE_TYPES[rule_type](**params))
        return cls(rules)


DEFAULT_PROTECTIONS: list[dict] = [
    {"rule_type": "max_daily_loss", "params": {"max_daily_loss_usd": 300}},
    {"rule_type": "max_open_positions", "params": {"max_open_positions": 5}},
    {"rule_type": "stoploss_guard", "params": {"max_consecutive_stops": 3, "lookback_ms": 3_600_000}},
    {"rule_type": "max_drawdown", "params": {"max_drawdown_pct": 0.15}},
    {"rule_type": "cooldown", "params": {"cooldown_ms": 1_800_000, "trigger_after_n_losses": 4}},
]
