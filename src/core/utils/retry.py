"""Async retry decorator with exponential backoff.

Provides a single ``@async_retry`` decorator to replace hand-rolled retry
loops scattered across the codebase.

Usage::

    from src.core.utils.retry import async_retry

    @async_retry(max_attempts=3, backoff=(1.0, 2.0, 4.0), retryable_codes=(429, 503))
    async def call_api(url: str) -> dict:
        ...

The decorator retries on any exception in *retryable* (default: all
exceptions) whose HTTP status code (if any) is in *retryable_codes*.
If the exception has no HTTP status, it is always retried up to
*max_attempts*.

Inspired by the hand-rolled loop in ``openai_compat_adapter.py:296–333``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Iterable
from typing import Callable, TypeVar

_logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


def async_retry(
    max_attempts: int = 3,
    backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    retryable: Iterable[type[Exception]] = (Exception,),
    retryable_codes: Iterable[int] = (429, 500, 502, 503, 504),
) -> Callable[[F], F]:
    """Decorator: retry an async function with exponential backoff.

    Args:
        max_attempts:   Maximum number of total attempts (including the first).
        backoff:        Sleep durations (seconds) between attempts.
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

    Returns:
        A decorated async function that automatically retries on transient
        failures.
    """
    _retryable_tuple = tuple(retryable)
    _retryable_codes = frozenset(retryable_codes)
    _backoff = list(backoff)

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> object:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except _retryable_tuple as exc:
                    last_exc = exc
                    # Check HTTP status code if available
                    response = getattr(exc, "response", None)
                    status_code: int | None = getattr(response, "status_code", None)
                    if status_code is not None and status_code not in _retryable_codes:
                        # Non-retryable HTTP status — re-raise immediately
                        raise

                    if attempt == max_attempts:
                        raise

                    # Calculate wait time — clamp to last backoff value
                    wait_idx = min(attempt - 1, len(_backoff) - 1)
                    wait = _backoff[wait_idx] if _backoff else 1.0
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
