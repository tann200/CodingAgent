"""Thread-safe three-state CircuitBreaker for LLM provider adapters.

Extracted from ``src/core/inference/llm_manager.py`` (TASK-43).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict


class CircuitBreaker:
    """Simple three-state circuit breaker (CLOSED -> OPEN -> HALF_OPEN -> CLOSED).

    States:
        CLOSED    -- requests pass through normally.
        OPEN      -- requests are rejected immediately (fast-fail) after
                     *failure_threshold* consecutive failures.
        HALF_OPEN -- after *recovery_timeout* seconds the breaker lets ONE probe
                     request through. If it succeeds -> CLOSED; if it fails -> OPEN.

    Thread-safe: all state mutations are protected by an RLock.
    A single ``_probe_in_flight`` flag prevents two concurrent callers from both
    seeing ``is_open() == False`` during HALF_OPEN and issuing simultaneous probes.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = self.CLOSED
        self._failure_count = 0
        self._opened_at: float = 0.0
        self._probe_in_flight: bool = False
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def allow_request(self) -> bool:
        """Atomically test and admit a request. In HALF_OPEN, allows exactly one probe."""
        with self._lock:
            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                if time.time() - self._opened_at >= self.recovery_timeout:
                    if not self._probe_in_flight:
                        self._state = self.HALF_OPEN
                        self._probe_in_flight = True
                        return True
                return False
            if self._state == self.HALF_OPEN:
                if not self._probe_in_flight:
                    self._probe_in_flight = True
                    return True
                return False
            return False

    def is_open(self) -> bool:
        """Return True when the breaker will reject the next call (read-only)."""
        with self._lock:
            return self._current_state() == self.OPEN or self._probe_in_flight

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._probe_in_flight = False
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._probe_in_flight = False
            if self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.time()

    def _current_state(self) -> str:
        """Return logical circuit state without mutating _state or _probe_in_flight."""
        if self._state == self.OPEN:
            if time.time() - self._opened_at >= self.recovery_timeout:
                return self.HALF_OPEN
        return self._state


# Per-provider circuit breaker registry (provider_key -> CircuitBreaker)
_CIRCUIT_BREAKERS: Dict[str, CircuitBreaker] = {}
_CB_LOCK = threading.RLock()

_CB_FAILURE_THRESHOLD = int(os.getenv("LLM_CB_FAILURE_THRESHOLD", "3"))
_CB_RECOVERY_TIMEOUT = float(os.getenv("LLM_CB_RECOVERY_TIMEOUT", "60"))


def get_circuit_breaker(provider_key: str) -> CircuitBreaker:
    """Return (creating if necessary) the CircuitBreaker for *provider_key*."""
    with _CB_LOCK:
        if provider_key not in _CIRCUIT_BREAKERS:
            _CIRCUIT_BREAKERS[provider_key] = CircuitBreaker(
                failure_threshold=_CB_FAILURE_THRESHOLD,
                recovery_timeout=_CB_RECOVERY_TIMEOUT,
            )
        return _CIRCUIT_BREAKERS[provider_key]
