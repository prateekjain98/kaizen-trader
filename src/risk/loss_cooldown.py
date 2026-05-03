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

# Per-symbol cooldown (parallel to per-strategy). Motivation: 7d prod data
# shows KNC traded 6× for -$2.08 net; brain keeps re-entering despite losses.
# Symbol-level shutoff after 2 losses for 4h, independent of strategy.
_SYMBOL_LOSS_THRESHOLD = 2
_SYMBOL_COOLDOWN_DURATION_S = 4 * 3600  # 4 hours
_consecutive_symbol_losses: dict[str, int] = {}  # symbol -> consecutive loss count
_symbol_cooldown_until: dict[str, float] = {}  # symbol -> ts when cooldown expires


def record_symbol_result(symbol: str, is_win: bool) -> None:
    """Record a per-symbol trade outcome and arm cooldown after N losses."""
    with _lock:
        if is_win:
            _consecutive_symbol_losses[symbol] = 0
            return
        count = _consecutive_symbol_losses.get(symbol, 0) + 1
        _consecutive_symbol_losses[symbol] = count
        if count >= _SYMBOL_LOSS_THRESHOLD:
            until = time.time() + _SYMBOL_COOLDOWN_DURATION_S
            _symbol_cooldown_until[symbol] = until
            log("warn",
                f"Symbol '{symbol}' in cooldown after {count} consecutive losses "
                f"(cooldown for {_SYMBOL_COOLDOWN_DURATION_S // 3600}h)",
                symbol=symbol)


def is_symbol_on_cooldown(symbol: str) -> bool:
    """Check if `symbol` is in per-symbol loss cooldown."""
    with _lock:
        until = _symbol_cooldown_until.get(symbol)
        if until is None:
            return False
        if time.time() >= until:
            del _symbol_cooldown_until[symbol]
            _consecutive_symbol_losses[symbol] = 0
            log("info", f"Symbol '{symbol}' cooldown expired — re-enabled",
                symbol=symbol)
            return False
        return True


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
