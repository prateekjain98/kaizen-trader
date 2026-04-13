"""Cross-strategy signal aggregation.

When multiple strategies signal the same asset within a time window:
- Same side: pick highest score + apply agreement bonus
- Opposing sides: cancel both (conflicting signals = no edge)
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, replace
from typing import Optional

from src.types import TradeSignal


_AGREEMENT_BONUS = 8
_MAX_SCORE = 95


@dataclass
class _PendingSignal:
    signal: TradeSignal
    agreeing_strategies: list[str]
    created_ms: float


class SignalAggregator:
    """Aggregates signals for the same symbol within a time window."""

    def __init__(self, window_ms: float = 3000):
        self._window_ms = window_ms
        self._lock = threading.Lock()
        self._pending: dict[str, list[_PendingSignal]] = {}

    def submit(self, signal: TradeSignal) -> list[TradeSignal]:
        """Submit a signal for aggregation.

        Returns a list of signals to process (may be empty if
        conflicting, or contain a boosted signal if agreeing).
        """
        now_ms = time.time() * 1000

        with self._lock:
            self._expire_old(now_ms)

            symbol = signal.symbol
            pending = self._pending.get(symbol, [])

            if not pending:
                self._pending[symbol] = [_PendingSignal(
                    signal=signal,
                    agreeing_strategies=[signal.strategy],
                    created_ms=signal.created_at if signal.created_at else now_ms,
                )]
                return [signal]

            same_side = [p for p in pending if p.signal.side == signal.side]
            opp_side = [p for p in pending if p.signal.side != signal.side]

            if opp_side:
                self._pending.pop(symbol, None)
                return []

            if same_side:
                best = max(same_side, key=lambda p: p.signal.score)
                best.agreeing_strategies.append(signal.strategy)

                boosted_score = max(best.signal.score, signal.score)
                boosted_score = min(boosted_score + _AGREEMENT_BONUS, _MAX_SCORE)

                base = signal if signal.score >= best.signal.score else best.signal
                boosted = replace(base, score=boosted_score)
                best.signal = boosted

                return [boosted]

            self._pending.setdefault(symbol, []).append(_PendingSignal(
                signal=signal,
                agreeing_strategies=[signal.strategy],
                created_ms=signal.created_at if signal.created_at else now_ms,
            ))
            return [signal]

    def flush(self) -> list[TradeSignal]:
        """Return all pending signals and clear the buffer."""
        with self._lock:
            results = []
            for pending_list in self._pending.values():
                for p in pending_list:
                    results.append(p.signal)
            self._pending.clear()
            return results

    def _expire_old(self, now_ms: float) -> None:
        """Remove signals older than the aggregation window."""
        expired_symbols = []
        for symbol, pending_list in self._pending.items():
            self._pending[symbol] = [
                p for p in pending_list
                if (now_ms - p.created_ms) < self._window_ms
            ]
            if not self._pending[symbol]:
                expired_symbols.append(symbol)
        for s in expired_symbols:
            del self._pending[s]
