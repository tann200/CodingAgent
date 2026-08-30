"""Centralized provider resilience policy and async retry.

PERF-02: single source of truth for how the agent treats transient provider
failures.  Previously each adapter owned a parallel copy of the retryable-status
set / backoff formula; this module centralizes:

- shared phase timeouts (connect, model-load, first-token, stream-idle),
- the retryable HTTP status classifier, and
- capped, jittered exponential backoff (via :func:`jittered_backoff`).

Adapters that need retry use :func:`async_retry` (async) or call the
classifiers directly.  This replaces the hand-rolled loops in
``openai_compat_adapter`` and ``ollama_adapter`` with one policy.

Usage::

    from src.core.utils.retry import async_retry, is_retryable_status_code

    @async_retry(max_attempts=3, backoff=(1.0, 2.0, 4.0), retryable_codes=(429, 503))
    async def call_api(url: str) -> dict:
        ...

The decorator retries on any exception in *retryable* (default: all
exceptions) whose HTTP status code (if any) is in *retryable_codes*.
If the exception has no HTTP status, it is always retried up to
*max_attempts*.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

_logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


@dataclass(frozen=True)
class ResiliencePolicy:
    """Shared phase timeouts for provider resilience.

    Centralizes the scattered literal timeouts used by adapters.  Phase timeouts
    are intentionally generous for local models (VRAM load) while keeping the
    connect probe fast.
    """

    connect_timeout: float = 10.0       # establishing the TCP/TLS connection
    model_load_timeout: float = 120.0   # local model warm-up into VRAM
    first_token_timeout: float = 120.0  # time to first generated token
    stream_idle_timeout: float = 60.0   # idle gap between stream tokens
    request_timeout: float = 120.0      # generic single-request deadline


# Default resilience policy shared across adapters.
DEFAULT_RESILIENCE_POLICY = ResiliencePolicy()

# Central retryable HTTP status set (was duplicated as `_RETRYABLE_STATUS` in
# openai_compat_adapter.py and `status_code >= 500` in ollama_adapter.py).
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def is_retryable_status_code(status_code: int | None) -> bool:
    """Return True when an HTTP status warrants a retry under the shared policy."""
    if status_code is None:
        return False
    return status_code in RETRYABLE_STATUS_CODES


def is_retryable_exception(
    exc: BaseException,
    retryable_codes: Iterable[int] = RETRYABLE_STATUS_CODES,
    *,
    retry_connection_errors: bool = True,
) -> bool:
    """Classify a raised exception as transient (retryable) or not.

    - Connection/timeout errors are treated as transient unless disabled.
    - HTTP-status-carrying exceptions (``requests.HTTPError``, ``httpx`` and
      generic objects exposing ``.response.status_code``) are retryable only
      when the code is in ``retryable_codes``.
    """
    codes = frozenset(retryable_codes)

    if isinstance(exc, ConnectionError):
        return retry_connection_errors

    if isinstance(exc, TimeoutError):
        return True

    response = getattr(exc, "response", None)
    if response is None and hasattr(exc, "status_code"):
        # e.g. openai errors carrying status_code directly
        code = getattr(exc, "status_code", None)
        return code in codes

    status_code: Optional[int] = getattr(response, "status_code", None)
    if status_code is not None:
        return status_code in codes

    return retry_connection_errors

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths.
_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    This replaces fixed exponential backoff with jittered delays to prevent
    thundering-herd retry spikes when multiple sessions hit the same
    rate-limited provider concurrently.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range. 0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    Example:
        >>> jittered_backoff(1, base_delay=2.0, max_delay=30.0)
        2.1  # ~2s + small jitter
        >>> jittered_backoff(3, base_delay=2.0, max_delay=30.0)
        8.2  # ~8s + jitter
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2**exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def async_retry(
    max_attempts: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    retryable: Iterable[type[Exception]] = (Exception,),
    retryable_codes: Iterable[int] = RETRYABLE_STATUS_CODES,
    *,
    max_backoff: float | None = None,
    retry_connection_errors: bool = True,
) -> Callable[[F], F]:
    """Decorator: retry an async function with capped, jittered backoff.

    Args:
        max_attempts:   Maximum number of total attempts (including the first).
        backoff:        Base sleep durations (seconds) between attempts.
                        Index 0 → wait after 1st failure, etc.
                        If fewer entries than ``max_attempts - 1``, the last
                        value is repeated.
        retryable:      Exception types that trigger a retry.  Default is all.
        retryable_codes: HTTP status codes that trigger a retry.  Only
                        consulted when the exception has a ``.response``
                        attribute with a ``.status_code`` property (e.g.
                        ``requests.HTTPError``, ``httpx.HTTPStatusError``).
                        When the code is *not* in this set the exception is
                        re-raised immediately.
        max_backoff:    Optional cap on the base delay before jitter is added.
                        When None, the largest ``backoff`` entry is the cap.
        retry_connection_errors:
                        Retry connection/timeout errors (default True).

    Returns:
        A decorated async function that automatically retries on transient
        failures with capped, jittered exponential backoff.
    """
    _retryable_tuple = tuple(retryable)
    _retryable_codes = frozenset(retryable_codes)
    _backoff = list(backoff)
    _max_backoff = max_backoff if max_backoff is not None else (_backoff[-1] if _backoff else 1.0)

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> object:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except _retryable_tuple as exc:
                    last_exc = exc
                    if not is_retryable_exception(
                        exc,
                        retryable_codes=_retryable_codes,
                        retry_connection_errors=retry_connection_errors,
                    ):
                        raise

                    if attempt == max_attempts:
                        raise

                    # Capped, jittered exponential backoff.
                    _base = _backoff[min(attempt - 1, len(_backoff) - 1)] if _backoff else 1.0
                    wait = jittered_backoff(
                        1,
                        base_delay=_base,
                        max_delay=_max_backoff,
                        jitter_ratio=0.5,
                    )
                    _logger.debug(
                        "async_retry: %s attempt %d/%d failed (%s), retrying in %.1fs",
                        fn.__qualname__,
                        attempt,
                        max_attempts,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)

            # Should never reach here, but satisfy type checker
            if last_exc is not None:
                raise last_exc
            return None  # pragma: no cover

        return wrapper  # type: ignore[return-value]

    return decorator
