"""Simple circuit breaker for external API calls."""

import threading
import time

from src.storage.database import log


class CircuitBreaker:
    """Prevents cascading failures by temporarily disabling calls to a failing service.

    States:
        CLOSED  — normal operation, calls pass through
        OPEN    — too many failures, calls are blocked
        HALF_OPEN — after reset_timeout, one trial call is allowed
    """

    def __init__(self, name: str, failure_threshold: int = 3, reset_timeout_s: float = 300):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s

        self._lock = threading.Lock()
        self._failure_count = 0
        self._last_failure_at: float = 0
        self._state = "closed"  # closed | open | half_open

    def can_call(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                elapsed = time.time() - self._last_failure_at
                if elapsed >= self.reset_timeout_s:
                    self._state = "half_open"
                    log("info", f"Circuit breaker '{self.name}' entering half-open state (testing)")
                    return True
                return False
            # half_open — allow the trial call
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state == "half_open":
                log("info", f"Circuit breaker '{self.name}' recovered — closing")
            self._failure_count = 0
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_at = time.time()
            if self._failure_count >= self.failure_threshold and self._state != "open":
                self._state = "open"
                log("warn", f"Circuit breaker '{self.name}' OPEN after {self._failure_count} failures "
                    f"— blocking calls for {self.reset_timeout_s}s")
