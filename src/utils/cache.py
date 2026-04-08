"""Generic thread-safe TTL cache.

Replaces the repeated pattern of lock + dict[key, tuple[value, timestamp]]
that appears across many modules (hourly_stats, adaptive_stops, regime_gate,
leverage profile, cross_exchange_divergence, etc.).
"""

import threading
import time
from typing import Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Thread-safe cache with per-entry time-to-live.

    Usage:
        cache = TTLCache[str, float](ttl_s=3600)
        val = cache.get("key")
        if val is None:
            val = expensive_compute()
            cache.set("key", val)
    """

    def __init__(self, ttl_s: float):
        self._ttl = ttl_s
        self._data: dict[K, tuple[V, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: K) -> Optional[V]:
        """Return cached value if present and not expired, else None."""
        with self._lock:
            entry = self._data.get(key)
            if entry and (time.time() - entry[1]) < self._ttl:
                return entry[0]
        return None

    def set(self, key: K, value: V) -> None:
        """Store a value with the current timestamp."""
        with self._lock:
            self._data[key] = (value, time.time())

    def get_raw(self, key: K) -> Optional[tuple[V, float]]:
        """Return (value, stored_at) without TTL check. For reading stale-but-present data."""
        with self._lock:
            return self._data.get(key)

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
