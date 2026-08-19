from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional


def is_error_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or "error" in result:
        return True
    meta = result.get("meta")
    return bool(isinstance(meta, dict) and meta.get("error"))


async def attempt_model_fallback(
    *,
    enabled: bool,
    current_result: Any,
    current_model: Optional[str],
    provider: Optional[str],
    messages: List[Dict[str, Any]],
    stream: bool,
    format_json: bool,
    tools: Optional[List[Any]],
    kwargs: Dict[str, Any],
    max_fallbacks: int,
    get_available_models: Callable[[str, str, str], Awaitable[List[str]]],
    call_model_internal: Callable[..., Awaitable[Any]],
    on_success: Optional[Callable[[], None]] = None,
) -> "tuple[Any, bool]":
    """Return ``(result, cb_handled)`` where ``cb_handled`` is True when a
    fallback succeeded and ``on_success`` already recorded success in the
    circuit breaker — the caller must skip the second ``record_success`` call
    to prevent double-counting (G-01/G-02).
    """
    if not enabled or not is_error_result(current_result):
        return current_result, False

    try:
        models = await get_available_models("", "", provider or "")
        if not models:
            return current_result, False

        attempts = 0
        for fallback_model in models:
            if fallback_model == current_model:
                continue
            if attempts >= max_fallbacks:
                break
            attempts += 1
            fallback_result = await call_model_internal(
                messages,
                provider,
                fallback_model,
                stream,
                format_json,
                tools,
                **kwargs,
            )
            if not is_error_result(fallback_result):
                if on_success is not None:
                    try:
                        on_success()
                    except Exception:
                        pass
                # G-01: circuit breaker success already recorded via on_success;
                # signal caller to skip its own record_success call.
                return fallback_result, True
    except Exception as _fb_exc:
        # G-03: log the exception so programming bugs (AttributeError, TypeError)
        # are not silently swallowed alongside expected network/provider errors.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "attempt_model_fallback: unexpected error during fallback: %s", _fb_exc
        )
        return current_result, False

    return current_result, False


def update_circuit_breaker_for_result(
    *,
    provider_key: str,
    result: Any,
    get_circuit_breaker: Callable[[str], Any],
) -> None:
    if not provider_key:
        return
    breaker = get_circuit_breaker(provider_key)
    if is_error_result(result):
        breaker.record_failure()
    else:
        breaker.record_success()


def record_token_usage(
    *,
    session_id: Optional[str],
    result: Any,
    get_token_budget_monitor: Optional[Callable[[], Any]],
) -> None:
    if not session_id or not isinstance(result, dict):
        return

    prompt_tokens = int(result.get("prompt_tokens") or 0)
    completion_tokens = int(result.get("completion_tokens") or 0)
    total_tokens = int(result.get("total_tokens") or (prompt_tokens + completion_tokens))
    if total_tokens <= 0 or get_token_budget_monitor is None:
        return

    try:
        get_token_budget_monitor().record_usage(
            session_id,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )
    except Exception:
        pass


def publish_llm_response_hook(
    *,
    enabled: bool,
    hook_registry: Any,
    hook_name: str,
    result: Any,
    model: Optional[str],
    provider: Optional[str],
) -> None:
    if not enabled or hook_registry is None:
        return
    try:
        text = result.get("text", "") if isinstance(result, dict) else ""
        hook_registry.call(
            hook_name,
            {
                "content": text,
                "model": model or "",
                "provider": provider or "",
                "ok": result.get("ok", True) if isinstance(result, dict) else True,
            },
        )
    except Exception:
        pass
