"""Per-strategy consecutive loss cooldown.

Tracks losing streaks independently for each strategy. After 3 consecutive
losses, imposes a 30-minute cooldown on that specific strategy.

Differs from CooldownPeriod in src.risk.protections which is a global
portfolio-level protection that halts ALL trading after N losses.
"""

import threading
import time

from src.storage.database import log

_CONSECUTIVE_LOSS_THRESHOLD = 3  # trigger after 3 consecutive losses
_COOLDOWN_DURATION_S = 1800  # 30 minutes

_lock = threading.Lock()
_consecutive_losses: dict[str, int] = {}  # strategy -> consecutive loss count
_cooldown_until: dict[str, float] = {}  # strategy -> timestamp when cooldown expires


def record_trade_result(strategy: str, is_win: bool) -> None:
    """Record a trade result and manage cooldown state."""
    with _lock:
        if is_win:
            _consecutive_losses[strategy] = 0
        else:
            count = _consecutive_losses.get(strategy, 0) + 1
            _consecutive_losses[strategy] = count

            if count >= _CONSECUTIVE_LOSS_THRESHOLD:
                until = time.time() + _COOLDOWN_DURATION_S
                _cooldown_until[strategy] = until
                log("warn",
                    f"Strategy '{strategy}' in cooldown after {count} consecutive losses "
                    f"(cooldown for {_COOLDOWN_DURATION_S // 60} minutes)",
                    strategy=strategy)


def is_on_cooldown(strategy: str) -> bool:
    """Check if a strategy is currently in cooldown."""
    with _lock:
        until = _cooldown_until.get(strategy)
        if until is None:
            return False
        if time.time() >= until:
            # Cooldown expired — reset
            del _cooldown_until[strategy]
            _consecutive_losses[strategy] = 0
            log("info", f"Strategy '{strategy}' cooldown expired — re-enabled",
                strategy=strategy)
            return False
        return True


def get_consecutive_losses(strategy: str) -> int:
    """Get current consecutive loss count for a strategy."""
    with _lock:
        return _consecutive_losses.get(strategy, 0)


def get_cooldown_remaining_s(strategy: str) -> float:
    """Get remaining cooldown time in seconds. Returns 0 if not in cooldown."""
    with _lock:
        until = _cooldown_until.get(strategy)
        if until is None:
            return 0.0
        remaining = until - time.time()
        return max(0.0, remaining)
