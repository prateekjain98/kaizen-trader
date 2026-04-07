"""Layered LLM memory for the analysis loop.

Working memory: last 3 analysis summaries (short-term context).
Long-term memory: key insights with daily decay. Prune when weight < 0.1.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from src.storage.database import log

_MEMORY_FILE = "data/analysis_memory.json"
_MAX_WORKING_MEMORY = 3
_DAILY_DECAY = 0.10  # 10% weight decay per day
_MIN_WEIGHT = 0.10


@dataclass
class Insight:
    text: str
    timestamp: float
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)


@dataclass
class MemoryState:
    working_memory: list[str] = field(default_factory=list)  # last N summaries
    insights: list[Insight] = field(default_factory=list)
    last_decay_at: float = 0


class AnalysisMemory:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = MemoryState()
        self._load()

    def _load(self) -> None:
        """Load memory from disk."""
        if not os.path.exists(_MEMORY_FILE):
            return
        try:
            with open(_MEMORY_FILE) as f:
                data = json.load(f)
            self._state.working_memory = data.get("working_memory", [])
            self._state.insights = [
                Insight(**i) for i in data.get("insights", [])
            ]
            self._state.last_decay_at = data.get("last_decay_at", 0)
        except Exception as err:
            log("warn", f"Analysis memory load failed: {err}")

    def _save(self) -> None:
        """Persist memory to disk."""
        try:
            os.makedirs(os.path.dirname(_MEMORY_FILE), exist_ok=True)
            data = {
                "working_memory": self._state.working_memory,
                "insights": [asdict(i) for i in self._state.insights],
                "last_decay_at": self._state.last_decay_at,
            }
            with open(_MEMORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as err:
            log("warn", f"Analysis memory save failed: {err}")

    def record_analysis(self, summary: str, insights: list[str]) -> None:
        """Record a new analysis result."""
        with self._lock:
            # Working memory: FIFO, max 3
            self._state.working_memory.append(summary)
            if len(self._state.working_memory) > _MAX_WORKING_MEMORY:
                self._state.working_memory.pop(0)

            # Long-term insights
            now = time.time() * 1000
            for text in insights:
                # Extract simple tags from text
                tags = _extract_tags(text)
                self._state.insights.append(Insight(
                    text=text, timestamp=now, weight=1.0, tags=tags,
                ))

            self._save()

    def get_working_context(self) -> str:
        """Get formatted working memory for injection into the analysis prompt."""
        with self._lock:
            if not self._state.working_memory:
                return "(no prior analyses yet)"
            lines = []
            for i, summary in enumerate(self._state.working_memory, 1):
                lines.append(f"Analysis {i}: {summary}")
            return "\n".join(lines)

    def get_relevant_insights(self, topic: str, limit: int = 5) -> list[str]:
        """Get top insights matching a topic (simple keyword match)."""
        with self._lock:
            topic_lower = topic.lower()
            scored = []
            for insight in self._state.insights:
                if insight.weight < _MIN_WEIGHT:
                    continue
                # Simple relevance: keyword overlap
                relevance = 0
                for word in topic_lower.split():
                    if word in insight.text.lower():
                        relevance += 1
                    if word in [t.lower() for t in insight.tags]:
                        relevance += 2
                if relevance > 0:
                    scored.append((insight.weight * relevance, insight.text))

            scored.sort(reverse=True)
            return [text for _, text in scored[:limit]]

    def decay_and_prune(self) -> int:
        """Apply daily decay to insights and prune stale ones. Returns count pruned."""
        with self._lock:
            now = time.time() * 1000
            days_since_decay = (now - self._state.last_decay_at) / 86_400_000

            if days_since_decay < 0.5:
                return 0  # don't decay more than twice a day

            pruned = 0
            surviving = []
            for insight in self._state.insights:
                days_old = (now - insight.timestamp) / 86_400_000
                insight.weight *= (1 - _DAILY_DECAY) ** max(1, int(days_old - days_since_decay + 1))
                if insight.weight >= _MIN_WEIGHT:
                    surviving.append(insight)
                else:
                    pruned += 1

            self._state.insights = surviving
            self._state.last_decay_at = now
            self._save()

            if pruned:
                log("info", f"Analysis memory: pruned {pruned} stale insights, {len(surviving)} remaining")
            return pruned

    def get_stats(self) -> dict:
        """Get memory stats for logging."""
        with self._lock:
            return {
                "working_memory_size": len(self._state.working_memory),
                "total_insights": len(self._state.insights),
                "avg_weight": (
                    sum(i.weight for i in self._state.insights) / len(self._state.insights)
                    if self._state.insights else 0
                ),
            }


def _extract_tags(text: str) -> list[str]:
    """Extract simple tags from insight text."""
    strategy_names = [
        "momentum", "mean_reversion", "funding", "liquidation", "orderbook",
        "whale", "correlation", "narrative", "protocol", "fear_greed",
    ]
    tags = []
    lower = text.lower()
    for s in strategy_names:
        if s in lower:
            tags.append(s)
    if "stop" in lower:
        tags.append("stop_loss")
    if "size" in lower or "position" in lower:
        tags.append("sizing")
    if "drawdown" in lower:
        tags.append("risk")
    return tags


# Singleton
_instance: Optional[AnalysisMemory] = None
_instance_lock = threading.Lock()


def get_analysis_memory() -> AnalysisMemory:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AnalysisMemory()
    return _instance
