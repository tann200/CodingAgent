from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .inference_timeout import (
    InferenceTimeoutError,
    InferenceTimeoutPolicy,
    get_default_policy,
    log_timeout_event,
    run_with_timeout,
)

_logger = logging.getLogger(__name__)


def select_runtime_provider_config(*, raw: Any, provider: Optional[str]) -> Optional[dict]:
    providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    selected = None
    if provider:
        provider_name = str(provider).lower()
        for item in providers:
            if not isinstance(item, dict):
                continue
            if (item.get("name") or "").lower() == provider_name or (item.get("type") or "").lower() == provider_name:
                selected = item
                break
    if selected is None and providers:
        first = providers[0]
        if isinstance(first, dict):
            selected = first
    return selected


def instantiate_runtime_adapter(
    *,
    provider_config: Optional[dict],
    providers_config_path: Optional[str],
    resolve_adapter_class: Callable[..., Tuple[Optional[type], Optional[str]]],
    instantiate_adapter: Callable[..., Tuple[Optional[Any], Optional[str]]],
    normalize_models_for_provider: Callable[[dict], List[str]],
    camelize: Callable[[str], str],
) -> Optional[Any]:
    if not provider_config:
        return None

    provider_type = str(provider_config.get("type") or "").strip().lower().replace("-", "_") or "ollama"
    adapter_cls, _error = resolve_adapter_class(
        provider_type=provider_type,
        camelize=camelize,
    )
    if adapter_cls is None:
        return None

    adapter, _error = instantiate_adapter(
        adapter_cls=adapter_cls,
        provider=provider_config,
        providers_config_path=providers_config_path,
        normalize_models_for_provider=normalize_models_for_provider,
    )
    if adapter is not None:
        return adapter

    # Preserve the broader historical runtime fallback path.
    try:
        return adapter_cls(
            name=provider_config.get("name"),
            base_url=provider_config.get("base_url") or provider_config.get("url"),
            api_key=provider_config.get("api_key"),
        )
    except TypeError:
        try:
            return adapter_cls(provider_config.get("base_url") or provider_config.get("url"))
        except Exception:
            try:
                return adapter_cls(provider_config)
            except Exception:
                try:
                    return adapter_cls()
                except Exception:
                    return None


def prepare_call_extra_args(
    *,
    kwargs: Dict[str, Any],
    tools: Optional[List[Any]],
    is_proxy_adapter: Callable[[Any], bool],
    adapter: Any,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    call_extra_args = dict(kwargs or {})
    try:
        if tools is not None:
            call_extra_args["tools"] = tools
            return call_extra_args

        inject_noop = False
        try:
            inject_noop = is_proxy_adapter(adapter)
        except Exception:
            inject_noop = False

        # P0: only inject noop when history has tool calls (conversation
        # is in "tool mode") — unconditional injection causes 400 errors
        # during compaction when no tools are expected.
        if inject_noop and _history_has_tool_calls(messages):
            call_extra_args["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "_noop",
                        "description": "No-op placeholder injected by LLM manager to satisfy proxy requirement",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
    except Exception:
        pass
    return call_extra_args


def _history_has_tool_calls(messages: Optional[List[Dict[str, Any]]]) -> bool:
    """Return True when *messages* contains at least one assistant tool-call record."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("tool_calls"):
            return True
        content = msg.get("content", "")
        if isinstance(content, str) and '"tool_calls"' in content:
            return True
    return False


async def _run_sync_with_deadline(
    loop: asyncio.AbstractEventLoop,
    executor: Any,
    fn: Callable,
    *,
    timeout_secs: float,
    phase: str,
    provider: Optional[str] = None,
) -> Any:
    """Run a sync callable in *executor* with a hard async deadline.

    Wraps ``loop.run_in_executor`` so that a hung synchronous adapter call
    cannot block the event loop indefinitely.

    Raises ``InferenceTimeoutError`` when the deadline expires.
    """
    coro = loop.run_in_executor(executor, fn)
    return await run_with_timeout(
        coro,
        timeout_secs=timeout_secs,
        phase=phase,
        provider=provider,
    )


async def _consume_stream_response(
    raw_response: Any,
    *,
    model: Optional[str],
    policy: InferenceTimeoutPolicy,
    provider: Optional[str],
    publish_event: Optional[Callable[[str, Any], None]],
    # Supplied from llm_manager so the full event-bus publishing, reasoning
    # detection and finalize_stream logic is reused without duplication.
    consume_sse_stream_async: Optional[Callable[..., Awaitable[str]]] = None,
    # Fallback: the original sync consumer, run under connect_total deadline
    consume_sse_stream_sync: Optional[Callable[[Any, Optional[str]], str]] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Any:
    """Consume a streaming response under first_token + stream_idle deadlines.

    Preference order:
    1. If *consume_sse_stream_async* is provided it is awaited directly — it
       must accept ``(raw_response, model, policy, provider, publish_event)``
       and return a result dict.
    2. Otherwise the streaming lines are consumed via
       ``streaming.consume_stream_with_timeouts`` which applies per-token
       deadlines and offloads each sync ``next()`` to a thread executor.

    Returns a standard ``{"ok": True/False, ...}`` result dict.
    """
    from .streaming import (
        consume_stream_with_timeouts,
        finalize_stream,
        publish_stream_chunk,
    )

    _loop = loop or asyncio.get_event_loop()

    if consume_sse_stream_async is not None:
        return await consume_sse_stream_async(
            raw_response, model, policy, provider, publish_event
        )

    # --- Determine event bus and tag-split once ---
    bus = None
    try:
        from src.core.orchestration.event_bus import get_event_bus
        bus = get_event_bus()
    except Exception:
        pass

    tag_split_enabled = False
    try:
        from src.core.inference.thinking_utils import is_reasoning_model as _is_rm
        tag_split_enabled = _is_rm(model or "")
    except Exception:
        pass

    def _publish_chunk(chunk: str, is_reasoning: bool) -> None:
        publish_stream_chunk(bus=bus, chunk=chunk, is_reasoning=is_reasoning)

    # Determine what to iterate: prefer raw_response.iter_lines(), else raw_response itself
    if hasattr(raw_response, "iter_lines"):
        lines_iter = raw_response
    else:
        lines_iter = raw_response

    accumulated, timeout_err = await consume_stream_with_timeouts(
        lines_iter,
        policy=policy,
        provider=provider,
        publish_chunk_fn=_publish_chunk,
        tag_split_enabled=tag_split_enabled,
        publish=publish_event,
        loop=_loop,
    )

    if timeout_err is not None:
        # Partial content was collected; still finalize bus events but return error
        finalize_stream(bus=bus, accumulated=accumulated)
        return timeout_err.to_error_result()

    text = finalize_stream(bus=bus, accumulated=accumulated)
    return {"ok": True, "text": text, "streamed": True}


async def call_adapter_with_fallbacks(
    *,
    adapter: Any,
    messages: List[Dict[str, Any]],
    model: Optional[str],
    stream: bool,
    format_json: bool,
    call_extra_args: Dict[str, Any],
    run_with_correlation: Callable[..., Awaitable[Any]],
    consume_sse_stream: Callable[[Any, Optional[str]], str],
    timeout_policy: Optional[InferenceTimeoutPolicy] = None,
    provider: Optional[str] = None,
    publish: Optional[Callable[[str, Any], None]] = None,
) -> Any:
    """Call adapter methods (.chat or .generate) with bounded async deadlines.

    connect_total deadline
    ----------------------
    The initial sync adapter call (.chat / .generate) is run in a thread
    executor with a ``connect_total`` deadline.  If the call blocks beyond
    that limit an ``InferenceTimeoutError(phase="connect_total")`` is raised,
    caught here, and returned as a structured error dict.

    Streaming first_token + stream_idle deadlines
    ---------------------------------------------
    When the adapter returns a streaming response (``hasattr(result, "iter_lines")``),
    the SSE consumption is handed off to ``_consume_stream_response`` which
    invokes ``consume_stream_with_timeouts``.  That function:
    - Offloads each blocking ``next()`` of a sync ``iter_lines()`` to a thread
      executor so ``asyncio.wait_for`` can actually fire.
    - Applies ``first_token`` deadline until the first real token chunk.
    - Applies ``stream_idle`` deadline between subsequent real token chunks.
    - Heartbeat / empty / malformed lines do NOT reset the deadline.
    - On timeout: closes the response, publishes ``inference.timeout``, returns
      a structured error dict — NOT a silent partial success.

    Args:
        timeout_policy: Override the process-wide default policy.  Useful in tests.
        provider:       Provider key for structured timeout metadata.
        publish:        Optional event-bus publish callable for timeout events.
    """
    policy = timeout_policy or get_default_policy()
    last_err = None
    loop = asyncio.get_running_loop()

    if hasattr(adapter, "chat"):
        try:
            fn = functools.partial(
                adapter.chat,
                messages,
                model=model,
                stream=stream,
                format_json=format_json,
                **call_extra_args,
            )
            result = await _run_sync_with_deadline(
                loop,
                None,
                fn,
                timeout_secs=policy.connect_total,
                phase="connect_total",
                provider=provider,
            )
            if stream and hasattr(result, "iter_lines"):
                return await _consume_stream_response(
                    result,
                    model=model,
                    policy=policy,
                    provider=provider,
                    publish_event=publish,
                    loop=loop,
                )
            return result
        except InferenceTimeoutError as exc:
            log_timeout_event(exc, publish=publish)
            return exc.to_error_result()
        except Exception as exc:
            last_err = exc

    if hasattr(adapter, "generate"):
        try:
            fn = functools.partial(
                adapter.generate,
                messages,
                model=model,
                stream=stream,
                format_json=format_json,
                **call_extra_args,
            )
            result = await _run_sync_with_deadline(
                loop,
                None,
                fn,
                timeout_secs=policy.connect_total,
                phase="connect_total",
                provider=provider,
            )
            if stream and hasattr(result, "iter_lines"):
                return await _consume_stream_response(
                    result,
                    model=model,
                    policy=policy,
                    provider=provider,
                    publish_event=publish,
                    loop=loop,
                )
            return result
        except InferenceTimeoutError as exc:
            log_timeout_event(exc, publish=publish)
            return exc.to_error_result()
        except TypeError:
            try:
                fn = functools.partial(adapter.generate, messages)
                result = await _run_sync_with_deadline(
                    loop,
                    None,
                    fn,
                    timeout_secs=policy.connect_total,
                    phase="connect_total",
                    provider=provider,
                )
                if stream and hasattr(result, "iter_lines"):
                    return await _consume_stream_response(
                        result,
                        model=model,
                        policy=policy,
                        provider=provider,
                        publish_event=publish,
                        loop=loop,
                    )
                return result
            except InferenceTimeoutError as exc:
                log_timeout_event(exc, publish=publish)
                return exc.to_error_result()
            except Exception as exc:
                last_err = exc
        except Exception as exc:
            last_err = exc

    if last_err is not None:
        return {"ok": False, "error": str(last_err)}
    return {"ok": False, "error": "adapter_missing_generate_or_chat"}
