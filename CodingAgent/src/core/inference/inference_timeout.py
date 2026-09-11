"""inference_timeout.py — Bounded inference timeout policy.

Provides a single env-configurable ``InferenceTimeoutPolicy`` that covers:

  * ``connect_total``   — max seconds for a sync-adapter call (connect + call, run
                          in executor).  Default 120 s — preserves previous behaviour.
  * ``first_token``     — max seconds to wait for the *first* token on a streaming
                          response before raising.  Default 60 s.
  * ``stream_idle``     — max seconds allowed between successive tokens on a stream.
                          Default 30 s.
  * ``fallback_attempt``— max seconds allowed for a single cross-provider fallback
                          attempt.  Default 90 s.

All defaults are finite so no path can hang forever.  The policy is a small
immutable value object, read once from environment variables.  Tests can
construct custom policies directly without touching env.

Environment variables
---------------------
``LLM_TIMEOUT_CONNECT_TOTAL``   float, default 120
``LLM_TIMEOUT_FIRST_TOKEN``     float, default 60
``LLM_TIMEOUT_STREAM_IDLE``     float, default 30
``LLM_TIMEOUT_FALLBACK_ATTEMPT``float, default 90

Warm-up (pre-loading) is *not* supported — see docs/roadmap for details.

Failure metadata
----------------
All timeout raises use ``InferenceTimeoutError``, which carries structured
metadata:
  * ``phase``     — "connect_total" | "first_token" | "stream_idle" | "fallback_attempt"
  * ``kind``      — "timeout"
  * ``retryable`` — True (timeouts are generally transient)
  * ``provider``  — optional str, set by call-site

The error converts to the standard ``{"ok": False, "error": ...}`` dict
format via ``to_error_result()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured error
# ---------------------------------------------------------------------------


class InferenceTimeoutError(asyncio.TimeoutError):
    """Timeout with structured metadata for observability.

    Attributes:
        phase:     Which timeout phase fired (connect_total, first_token, etc.)
        kind:      Always "timeout".
        retryable: True — timeouts are transient by default.
        provider:  Optional provider key that timed out.
    """

    def __init__(
        self,
        *,
        phase: str,
        timeout_secs: float,
        provider: Optional[str] = None,
    ) -> None:
        self.phase = phase
        self.kind = "timeout"
        self.retryable = True
        self.provider = provider
        self.timeout_secs = timeout_secs
        msg = (
            f"inference timeout: phase={phase!r} limit={timeout_secs:.1f}s"
            + (f" provider={provider!r}" if provider else "")
        )
        super().__init__(msg)

    def to_error_result(self) -> dict:
        """Convert to the standard ``{"ok": False, ...}`` result dict."""
        return {
            "ok": False,
            "error": f"timeout:{self.phase}",
            "meta": {
                "phase": self.phase,
                "kind": self.kind,
                "retryable": self.retryable,
                "provider": self.provider,
                "timeout_secs": self.timeout_secs,
            },
        }


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class InferenceTimeoutPolicy:
    """Immutable timeout policy.  Construct from env or supply values directly.

    All values are in seconds and must be strictly positive.
    """

    connect_total: float = field(default=120.0)
    first_token: float = field(default=60.0)
    stream_idle: float = field(default=30.0)
    fallback_attempt: float = field(default=90.0)

    def __post_init__(self) -> None:
        for attr in ("connect_total", "first_token", "stream_idle", "fallback_attempt"):
            v = getattr(self, attr)
            if not (isinstance(v, (int, float)) and v > 0):
                raise ValueError(
                    f"InferenceTimeoutPolicy.{attr} must be a positive number, got {v!r}"
                )

    @classmethod
    def from_env(cls) -> "InferenceTimeoutPolicy":
        """Read policy from environment variables, using finite defaults."""
        return cls(
            connect_total=_env_float("LLM_TIMEOUT_CONNECT_TOTAL", 120.0),
            first_token=_env_float("LLM_TIMEOUT_FIRST_TOKEN", 60.0),
            stream_idle=_env_float("LLM_TIMEOUT_STREAM_IDLE", 30.0),
            fallback_attempt=_env_float("LLM_TIMEOUT_FALLBACK_ATTEMPT", 90.0),
        )


# ---------------------------------------------------------------------------
# Module-level default policy (lazy, re-reads env on first access)
# ---------------------------------------------------------------------------

_DEFAULT_POLICY: Optional[InferenceTimeoutPolicy] = None


def get_default_policy() -> InferenceTimeoutPolicy:
    """Return the process-wide default policy, initialised lazily from env."""
    global _DEFAULT_POLICY
    if _DEFAULT_POLICY is None:
        _DEFAULT_POLICY = InferenceTimeoutPolicy.from_env()
    return _DEFAULT_POLICY


def reset_default_policy() -> None:
    """Force re-read of env vars on next call.  Useful in tests."""
    global _DEFAULT_POLICY
    _DEFAULT_POLICY = None


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def run_with_timeout(
    coro,
    *,
    timeout_secs: float,
    phase: str,
    provider: Optional[str] = None,
) -> Any:
    """Await *coro* with a deadline; raise ``InferenceTimeoutError`` on expiry.

    Args:
        coro:         Any awaitable.
        timeout_secs: Hard deadline in seconds.
        phase:        Timeout phase label used in error metadata.
        provider:     Optional provider key for structured metadata.

    Raises:
        InferenceTimeoutError: When the deadline expires.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_secs)
    except asyncio.TimeoutError:
        err = InferenceTimeoutError(
            phase=phase, timeout_secs=timeout_secs, provider=provider
        )
        _logger.warning(
            "inference timeout: phase=%r secs=%.1f provider=%r",
            phase,
            timeout_secs,
            provider,
        )
        raise err from None


def log_timeout_event(
    err: InferenceTimeoutError,
    *,
    publish: Any = None,
) -> None:
    """Log and optionally publish a timeout event to the event bus.

    Avoids double-counting: does NOT call circuit-breaker methods.
    Circuit-breaker recording is the caller's responsibility.

    Args:
        err:     The ``InferenceTimeoutError`` that was caught.
        publish: Optional callable(event_name, payload) for event-bus publishing.
    """
    _logger.warning(
        "inference.timeout phase=%r provider=%r secs=%.1f",
        err.phase,
        err.provider,
        err.timeout_secs,
    )
    if publish is not None:
        try:
            publish(
                "inference.timeout",
                {
                    "phase": err.phase,
                    "kind": err.kind,
                    "retryable": err.retryable,
                    "provider": err.provider,
                    "timeout_secs": err.timeout_secs,
                },
            )
        except Exception:
            pass
