from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

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


def _attach_streamed_turn_metadata(
    target: Dict[str, Any],
    stream_text: Any,
    *,
    always_populate_choices: bool,
) -> Dict[str, Any]:
    """Attach structured tool_calls, thinking_blocks, reasoning, and usage from SSE."""
    plain_text = str(stream_text)
    target["text"] = plain_text
    target["streamed"] = True

    tcs = list(getattr(stream_text, "tool_calls", None) or [])
    tbs = list(getattr(stream_text, "thinking_blocks", None) or [])
    rc = str(getattr(stream_text, "reasoning_content", "") or "")
    finish_reason = str(getattr(stream_text, "finish_reason", "") or "stop")
    usage = getattr(stream_text, "usage", None) or {}

    if isinstance(usage, dict) and usage:
        p_tok = int(usage.get("prompt_tokens", target.get("prompt_tokens", 0)) or 0)
        c_tok = int(
            usage.get("completion_tokens", target.get("completion_tokens", 0)) or 0
        )
        target["prompt_tokens"] = p_tok
        target["completion_tokens"] = c_tok
        target["total_tokens"] = p_tok + c_tok
        if "cache_creation_input_tokens" in usage:
            target["cache_creation_input_tokens"] = int(
                usage["cache_creation_input_tokens"] or 0
            )
        if "cache_read_input_tokens" in usage:
            target["cache_read_input_tokens"] = int(
                usage["cache_read_input_tokens"] or 0
            )

    has_structured = bool(tcs or tbs or rc)
    if always_populate_choices or has_structured:
        msg_obj: Dict[str, Any] = {"role": "assistant", "content": plain_text}
        if tcs:
            msg_obj["tool_calls"] = tcs
            if finish_reason == "stop":
                finish_reason = "tool_calls"
        if tbs:
            msg_obj["thinking_blocks"] = tbs
        if rc:
            msg_obj["reasoning_content"] = rc
        choice_obj: Dict[str, Any] = {
            "message": msg_obj,
            "finish_reason": finish_reason,
        }
        if tcs:
            choice_obj["tool_calls"] = tcs
        if not target.get("choices") or has_structured:
            target["choices"] = [choice_obj]

    return target


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
) -> Any:
    last_err = None

    if hasattr(adapter, "chat"):
        loop = asyncio.get_running_loop()
        try:
            fn = functools.partial(
                adapter.chat,
                messages,
                model=model,
                stream=stream,
                format_json=format_json,
                **call_extra_args,
            )
            result = await run_with_correlation(loop, None, fn)
            if stream:
                if hasattr(result, "iter_lines"):
                    text = await run_with_correlation(
                        loop,
                        None,
                        functools.partial(consume_sse_stream, result, model),
                    )
                    return _attach_streamed_turn_metadata(
                        {"ok": True},
                        text,
                        always_populate_choices=False,
                    )
                elif isinstance(result, dict) and result.get("raw") is not None:
                    raw = result["raw"]
                    if hasattr(raw, "iter_lines"):
                        text = await run_with_correlation(
                            loop,
                            None,
                            functools.partial(
                                consume_sse_stream, raw, model or result.get("model")
                            ),
                        )
                        return _attach_streamed_turn_metadata(
                            result,
                            text,
                            always_populate_choices=True,
                        )
                    elif hasattr(raw, "__iter__"):
                        from src.core.inference.streaming import consume_generator_stream

                        return await run_with_correlation(
                            loop,
                            None,
                            functools.partial(
                                consume_generator_stream,
                                raw,
                                model or result.get("model"),
                            ),
                        )
                elif hasattr(result, "__iter__") and not isinstance(
                    result, (dict, list, str, bytes)
                ):
                    from src.core.inference.streaming import consume_generator_stream

                    return await run_with_correlation(
                        loop,
                        None,
                        functools.partial(consume_generator_stream, result, model),
                    )
            return result
        except Exception as exc:
            last_err = exc

    if hasattr(adapter, "generate"):
        loop = asyncio.get_running_loop()
        try:
            fn = functools.partial(
                adapter.generate,
                messages,
                model=model,
                stream=stream,
                format_json=format_json,
                **call_extra_args,
            )
            result = await run_with_correlation(loop, None, fn)
            if stream:
                if hasattr(result, "iter_lines"):
                    text = await run_with_correlation(
                        loop,
                        None,
                        functools.partial(consume_sse_stream, result, model),
                    )
                    return _attach_streamed_turn_metadata(
                        {"ok": True},
                        text,
                        always_populate_choices=False,
                    )
                elif isinstance(result, dict) and result.get("raw") is not None:
                    raw = result["raw"]
                    if hasattr(raw, "iter_lines"):
                        text = await run_with_correlation(
                            loop,
                            None,
                            functools.partial(
                                consume_sse_stream, raw, model or result.get("model")
                            ),
                        )
                        return _attach_streamed_turn_metadata(
                            result,
                            text,
                            always_populate_choices=True,
                        )
                    elif hasattr(raw, "__iter__"):
                        from src.core.inference.streaming import consume_generator_stream

                        return await run_with_correlation(
                            loop,
                            None,
                            functools.partial(
                                consume_generator_stream,
                                raw,
                                model or result.get("model"),
                            ),
                        )
                elif hasattr(result, "__iter__") and not isinstance(
                    result, (dict, list, str, bytes)
                ):
                    from src.core.inference.streaming import consume_generator_stream

                    return await run_with_correlation(
                        loop,
                        None,
                        functools.partial(consume_generator_stream, result, model),
                    )
            return result
        except TypeError:
            try:
                fn = functools.partial(adapter.generate, messages)
                return await run_with_correlation(loop, None, fn)
            except Exception as exc:
                last_err = exc
        except Exception as exc:
            last_err = exc

    if last_err is not None:
        return {"ok": False, "error": str(last_err)}
    return {"ok": False, "error": "adapter_missing_generate_or_chat"}
