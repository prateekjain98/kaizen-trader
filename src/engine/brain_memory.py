"""Cross-session memory for the trading brain.

Persists to data/brain_memory.json so the engine remembers:
- Recent trades (last 50) -- prevents re-entering losers
- Lessons from trade reviews (last 30) -- accumulated wisdom
- Avoid list -- symbols that lost money recently, with auto-expiry

This is the key difference between a human trader and a stateless bot.
A human remembers "RAVE burned us 3 times" -- now the bot does too.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class BrainMemory:
    """Persistent cross-session memory backed by a JSON file."""

    _MAX_TRADES = 50
    _MAX_LESSONS = 30
    # Losses > 2% get a 4h avoid window; losses > 5% get 12h
    _AVOID_HOURS_MODERATE = 4
    _AVOID_HOURS_SEVERE = 12
    _MODERATE_LOSS_PCT = -2.0
    _SEVERE_LOSS_PCT = -5.0

    def __init__(self, file_path: Path | None = None) -> None:
        self._file: Path = file_path or (
            Path(__file__).parent.parent.parent / "data" / "brain_memory.json"
        )
        self.recent_trades: list[dict[str, Any]] = []
        self.lessons: list[dict[str, Any]] = []
        self.avoid_list: dict[str, dict[str, Any]] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Read state from JSON. Creates the file with defaults if missing."""
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self.recent_trades = data.get("recent_trades", [])
                self.lessons = data.get("lessons", [])
                self.avoid_list = data.get("avoid_list", {})
            except (json.JSONDecodeError, KeyError):
                # Corrupted file -- start fresh
                self._reset()
        else:
            self._reset()

    def save(self) -> None:
        """Atomic write: write to .tmp then os.replace."""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".json.tmp")
        payload = json.dumps(
            {
                "recent_trades": self.recent_trades,
                "lessons": self.lessons,
                "avoid_list": self.avoid_list,
            },
            indent=2,
        )
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._file)

    def _reset(self) -> None:
        self.recent_trades = []
        self.lessons = []
        self.avoid_list = {}

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def record_trade(
        self,
        symbol: str,
        pnl_pct: float,
        pnl_usd: float,
        exit_reason: str,
        strategy: str,
        duration_h: float,
    ) -> None:
        """Append a closed trade. Auto-avoids symbols with meaningful losses."""
        now = time.time()
        self.recent_trades.append(
            {
                "symbol": symbol,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 4),
                "exit_reason": exit_reason,
                "strategy": strategy,
                "duration_h": round(duration_h, 2),
                "closed_at": now,
            }
        )
        # Trim to most recent N
        if len(self.recent_trades) > self._MAX_TRADES:
            self.recent_trades = self.recent_trades[-self._MAX_TRADES:]

        # Auto-avoid on significant losses
        if pnl_pct <= self._SEVERE_LOSS_PCT:
            hours = self._AVOID_HOURS_SEVERE
            self.avoid_list[symbol] = {
                "reason": f"Lost {pnl_pct:.1f}% ({exit_reason})",
                "added_at": now,
                "expires_at": now + hours * 3600,
            }
        elif pnl_pct <= self._MODERATE_LOSS_PCT:
            hours = self._AVOID_HOURS_MODERATE
            self.avoid_list[symbol] = {
                "reason": f"Lost {pnl_pct:.1f}% ({exit_reason})",
                "added_at": now,
                "expires_at": now + hours * 3600,
            }

        self.save()

    # ------------------------------------------------------------------
    # Lessons
    # ------------------------------------------------------------------

    def add_lesson(self, text: str, source: str) -> None:
        """Record a trading lesson. *source* identifies where it came from."""
        self.lessons.append(
            {
                "text": text,
                "source": source,
                "timestamp": time.time(),
            }
        )
        if len(self.lessons) > self._MAX_LESSONS:
            self.lessons = self.lessons[-self._MAX_LESSONS:]
        self.save()

    # ------------------------------------------------------------------
    # Avoid list
    # ------------------------------------------------------------------

    def should_avoid(self, symbol: str) -> tuple[bool, str]:
        """Check if *symbol* is on the avoid list.

        Returns (True, reason) or (False, "").
        Expired entries are cleaned up on access.
        """
        entry = self.avoid_list.get(symbol)
        if entry is None:
            return False, ""

        if time.time() >= entry["expires_at"]:
            del self.avoid_list[symbol]
            self.save()
            return False, ""

        return True, entry["reason"]

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_win_rate(self) -> float:
        """Win rate from recent trades. Returns 0.0 if no trades."""
        if not self.recent_trades:
            return 0.0
        wins = sum(1 for t in self.recent_trades if t["pnl_pct"] > 0)
        return wins / len(self.recent_trades)

    def get_avg_pnl(self) -> float:
        """Average P&L percentage from recent trades."""
        if not self.recent_trades:
            return 0.0
        return sum(t["pnl_pct"] for t in self.recent_trades) / len(self.recent_trades)

    # ------------------------------------------------------------------
    # Prompt context
    # ------------------------------------------------------------------

    def get_context_for_prompt(self) -> str:
        """Compact summary (~200 tokens) for the Claude brain prompt.

        Includes: recent P&L stats, top lessons, and current avoid list.
        """
        lines: list[str] = ["## Brain Memory"]

        # --- P&L summary ---
        if self.recent_trades:
            n = len(self.recent_trades)
            wr = self.get_win_rate()
            avg = self.get_avg_pnl()
            recent_5 = self.recent_trades[-5:]
            streak = ", ".join(
                f"{t['symbol']} {t['pnl_pct']:+.1f}%" for t in recent_5
            )
            lines.append(
                f"Last {n} trades: {wr:.0%} win rate, avg {avg:+.2f}%. "
                f"Recent: {streak}"
            )
        else:
            lines.append("No trade history yet.")

        # --- Lessons ---
        if self.lessons:
            lines.append("Lessons:")
            for lesson in self.lessons[-5:]:
                lines.append(f"  - {lesson['text']}")

        # --- Avoid list (only non-expired) ---
        now = time.time()
        active_avoids = {
            sym: info
            for sym, info in self.avoid_list.items()
            if info["expires_at"] > now
        }
        if active_avoids:
            avoid_strs = [
                f"{sym} ({info['reason']})" for sym, info in active_avoids.items()
            ]
            lines.append(f"AVOID: {', '.join(avoid_strs)}")

        return "\n".join(lines)
