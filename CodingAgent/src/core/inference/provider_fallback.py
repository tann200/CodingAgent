"""provider_fallback.py — Cross-provider graceful fallback (P1-1).

Implements ``ProviderFallbackChain``: when the primary provider returns an
error result *and* its circuit breaker opens (or it is already open), this
chain automatically retries the call against each configured secondary
provider in priority order, stopping on the first success.

This complements the existing within-provider model fallback in
``call_postprocess.attempt_model_fallback`` (which tries different *models*
on the *same* provider).  Those two layers together give three levels of
resilience:

1. Same provider, same model — ``call_model`` / ``_call_model_internal``
2. Same provider, different model — ``attempt_model_fallback``
3. Different provider — ``ProviderFallbackChain.call``   ← this module

Configuration
-------------
Controlled entirely through environment variables so no config-file changes
are required:

``LLM_PROVIDER_FALLBACK_ENABLED``
    ``"1"`` (default) / ``"0"`` — kill-switch.

``LLM_PROVIDER_FALLBACK_ORDER``
    Comma-separated canonical provider keys defining the preferred fallback
    order.  When empty (default), the chain iterates all registered providers
    in ``ProviderManager.list_providers()`` order, skipping the primary.

``LLM_PROVIDER_FALLBACK_MAX``
    Maximum number of secondary providers to try before giving up.
    Default ``2``.

Timeout
-------
Each individual fallback attempt is bounded by
``InferenceTimeoutPolicy.fallback_attempt`` (default 90 s, env
``LLM_TIMEOUT_FALLBACK_ATTEMPT``).  A hung provider therefore cannot block
the entire fallback chain indefinitely.  The timeout fires as
``InferenceTimeoutError(phase="fallback_attempt")`` which is:
  * caught inside ``call()``
  * converted to an error-result dict
  * logged via ``log_timeout_event``
  * circuit-breaker failure recorded **once** (avoids double-counting)

Usage
-----
``ProviderFallbackChain`` is designed to be called from ``call_model()``
after the existing model-level fallback has already been exhausted::

    chain = get_fallback_chain()
    result, used_provider = await chain.call(
        primary_result=res,
        primary_provider=provider,
        messages=messages,
        model=model,
        stream=stream,
        format_json=format_json,
        tools=tools,
        kwargs=kwargs,
        call_model_internal=_call_model_internal,
        get_provider_manager=get_provider_manager,
        get_circuit_breaker=get_circuit_breaker,
        publish=_publish,            # optional event-bus publish fn
    )
    if used_provider and used_provider != primary_provider:
        # a different provider answered — downstream may want to know
        pass
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .call_postprocess import is_error_result
from .inference_timeout import (
    InferenceTimeoutError,
    InferenceTimeoutPolicy,
    get_default_policy,
    log_timeout_event,
    run_with_timeout,
)

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    val = os.getenv(name, "1" if default else "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


class ProviderFallbackChain:
    """Try alternative providers when the primary one fails.

    Each attempt is individually bounded by
    ``InferenceTimeoutPolicy.fallback_attempt`` so a hung provider cannot
    block the entire fallback chain.

    Designed to be a singleton (use ``get_fallback_chain()``), but safe to
    instantiate directly in tests.
    """

    def __init__(self) -> None:
        self._enabled: bool = _env_bool("LLM_PROVIDER_FALLBACK_ENABLED", default=True)
        self._max: int = _env_int("LLM_PROVIDER_FALLBACK_MAX", default=2)
        self._order_env: str = os.getenv("LLM_PROVIDER_FALLBACK_ORDER", "").strip()

    def _is_enabled(self) -> bool:
        # Re-read env each call so tests can flip the flag at runtime.
        return _env_bool("LLM_PROVIDER_FALLBACK_ENABLED", default=True)

    def _max_attempts(self) -> int:
        return _env_int("LLM_PROVIDER_FALLBACK_MAX", default=2)

    def _candidate_providers(
        self,
        primary_provider: Optional[str],
        list_providers: Callable[[], List[str]],
    ) -> List[str]:
        """Return ordered list of fallback provider keys (primary excluded)."""
        order_env = os.getenv("LLM_PROVIDER_FALLBACK_ORDER", "").strip()
        if order_env:
            candidates = [k.strip() for k in order_env.split(",") if k.strip()]
        else:
            try:
                candidates = list_providers()
            except Exception:
                candidates = []

        primary_key = (primary_provider or "").lower().replace(" ", "_")
        return [k for k in candidates if k.lower().replace(" ", "_") != primary_key]

    async def call(
        self,
        *,
        primary_result: Any,
        primary_provider: Optional[str],
        messages: List[Dict[str, Any]],
        model: Optional[str],
        stream: bool,
        format_json: bool,
        tools: Optional[List[Any]],
        kwargs: Dict[str, Any],
        call_model_internal: Callable[..., Awaitable[Any]],
        get_provider_manager: Callable[[], Any],
        get_circuit_breaker: Callable[[str], Any],
        publish: Optional[Callable[[str, Any], None]] = None,
        timeout_policy: Optional[InferenceTimeoutPolicy] = None,
    ) -> Tuple[Any, Optional[str]]:
        """Attempt cross-provider fallback.

        Each candidate attempt is bounded by
        ``timeout_policy.fallback_attempt`` (default 90 s).

        Returns ``(result, used_provider)`` where ``used_provider`` is the
        canonical key of the provider that ultimately succeeded (or
        ``primary_provider`` if no fallback was needed/available).

        The original ``primary_result`` is returned unchanged when:
        - fallback is disabled
        - the primary result is not an error
        - no candidate providers are available / open
        - all candidates also fail
        """
        policy = timeout_policy or get_default_policy()

        if not self._is_enabled():
            return primary_result, primary_provider

        if not is_error_result(primary_result):
            return primary_result, primary_provider

        try:
            mgr = get_provider_manager()
            candidates = self._candidate_providers(
                primary_provider, mgr.list_providers
            )
        except Exception as exc:
            logger.debug("ProviderFallbackChain: could not list providers: %s", exc)
            return primary_result, primary_provider

        max_attempts = self._max_attempts()
        attempts = 0

        for candidate_key in candidates:
            if attempts >= max_attempts:
                break

            # Skip candidates whose circuit breaker is already open
            try:
                cb = get_circuit_breaker(candidate_key)
                if cb.is_open():
                    logger.debug(
                        "ProviderFallbackChain: skipping %r — circuit breaker open",
                        candidate_key,
                    )
                    continue
            except Exception:
                pass

            attempts += 1
            logger.info(
                "ProviderFallbackChain: trying provider %r (attempt %d/%d)",
                candidate_key,
                attempts,
                max_attempts,
            )

            try:
                # Bound each individual fallback attempt so a hung provider
                # cannot block the rest of the chain.
                result = await run_with_timeout(
                    call_model_internal(
                        messages,
                        candidate_key,
                        model,
                        stream,
                        format_json,
                        tools,
                        **kwargs,
                    ),
                    timeout_secs=policy.fallback_attempt,
                    phase="fallback_attempt",
                    provider=candidate_key,
                )
            except InferenceTimeoutError as exc:
                # Timeout on a fallback attempt: log, record failure once,
                # then continue to the next candidate.
                log_timeout_event(exc, publish=publish)
                try:
                    get_circuit_breaker(candidate_key).record_failure()
                except Exception:
                    pass
                continue
            except Exception as exc:
                logger.warning(
                    "ProviderFallbackChain: provider %r raised %s: %s",
                    candidate_key,
                    type(exc).__name__,
                    exc,
                )
                try:
                    get_circuit_breaker(candidate_key).record_failure()
                except Exception:
                    pass
                continue

            if not is_error_result(result):
                logger.info(
                    "ProviderFallbackChain: fallback succeeded via %r", candidate_key
                )
                try:
                    get_circuit_breaker(candidate_key).record_success()
                except Exception:
                    pass
                if publish is not None:
                    try:
                        publish(
                            "provider.fallback.used",
                            {
                                "primary": primary_provider,
                                "fallback": candidate_key,
                                "model": model,
                            },
                        )
                    except Exception:
                        pass
                return result, candidate_key
            else:
                logger.warning(
                    "ProviderFallbackChain: provider %r returned error: %s",
                    candidate_key,
                    result.get("error", "?") if isinstance(result, dict) else result,
                )
                try:
                    get_circuit_breaker(candidate_key).record_failure()
                except Exception:
                    pass

        logger.warning(
            "ProviderFallbackChain: all %d fallback(s) exhausted for primary %r",
            attempts,
            primary_provider,
        )
        return primary_result, primary_provider


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_FALLBACK_CHAIN: Optional[ProviderFallbackChain] = None


def get_fallback_chain() -> ProviderFallbackChain:
    """Return the module-level singleton ``ProviderFallbackChain``."""
    global _FALLBACK_CHAIN
    if _FALLBACK_CHAIN is None:
        _FALLBACK_CHAIN = ProviderFallbackChain()
    return _FALLBACK_CHAIN
