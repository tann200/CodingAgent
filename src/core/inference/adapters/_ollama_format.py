"""Pure message/option/tool-call formatting helpers for OllamaAdapter (OPT-06).

Extracted from ``src/core/inference/adapters/ollama_adapter.py`` to keep the
adapter under 1,000 LOC while preserving all ``OllamaAdapter`` method names and
exact wire-format behavior.
"""

from __future__ import annotations

import json
from typing import Any, Collection, Dict, List, Optional


def build_base_variants(base_url: Optional[str]) -> List[str]:
    """Return deduplicated candidate Ollama base URLs."""
    if not base_url:
        return ["http://localhost:11434"]
    b = base_url.rstrip("/")
    variants = [b]
    if b.endswith("/api"):
        variants.append(b[:-4])
        variants.append(b + "/v1")
    if b.endswith("/v1"):
        variants.append(b[:-3])
        variants.append(b + "/api")
    variants.append(b + "/api")
    variants.append(b + "/v1")
    out: List[str] = []
    seen: set[str] = set()
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def build_endpoints(base_url: Optional[str], path: str) -> List[str]:
    """Build candidate full URLs for *path* across all base URL variants."""
    stripped_path = path.lstrip("/")
    return [base.rstrip("/") + "/" + stripped_path for base in build_base_variants(base_url)]


def parse_json_response_field(response_text: Any) -> Any:
    """Try to parse a response string as JSON and return the decoded Python object."""
    if response_text is None:
        return None
    if isinstance(response_text, (dict, list)):
        return response_text
    try:
        return json.loads(str(response_text).strip())
    except Exception:
        return response_text


def resolve_ollama_num_ctx(
    options: Dict[str, Any],
    provider: Any,
) -> Optional[int]:
    """Resolve effective ``num_ctx`` from options, provider config, or active context length."""
    if "num_ctx" in options and options["num_ctx"] is not None:
        try:
            return max(512, int(options["num_ctx"]))
        except (TypeError, ValueError):
            pass
    prov = provider if isinstance(provider, dict) else {}
    for key in ("num_ctx", "context_length"):
        raw = prov.get(key)
        if raw is not None:
            try:
                val = int(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    try:
        from src.core.inference.provider_context import _load_active_context_length

        ctx = int(_load_active_context_length("ollama") or 0)
        if ctx > 0:
            return ctx
    except Exception:
        pass
    return None


def extract_ollama_options(
    kwargs: Dict[str, Any],
    *,
    ollama_option_keys: Collection[str],
    provider: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Separate Ollama-specific ``options`` from top-level request payload kwargs."""
    options: Dict[str, Any] = {}
    payload_kwargs: Dict[str, Any] = {}
    for k, v in kwargs.items():
        if k == "kv_cache_quant":
            if isinstance(v, str) and v.strip():
                options.setdefault("kv_cache_type", v.strip())
            elif v:
                options.setdefault("kv_cache_type", "q8_0")
            continue
        if k in ollama_option_keys:
            options[k] = v
        else:
            payload_kwargs[k] = v
    resolved_ctx = resolve_ollama_num_ctx(options, provider)
    if resolved_ctx is not None:
        options.setdefault("num_ctx", resolved_ctx)
    if "kv_cache_type" not in options and isinstance(provider, dict):
        prov_kv = provider.get("kv_cache_quant")
        if isinstance(prov_kv, str) and prov_kv.strip():
            options["kv_cache_type"] = prov_kv.strip()
        elif prov_kv:
            options["kv_cache_type"] = "q8_0"
    return options, payload_kwargs


def normalize_inbound_tool_calls(raw_tool_calls: List[Any]) -> List[Dict[str, Any]]:
    """Ensure Ollama tool_calls carry deterministic ``id`` and OpenAI ``function`` shape."""
    normalized: List[Dict[str, Any]] = []
    for idx, tc in enumerate(raw_tool_calls or []):
        if not isinstance(tc, dict):
            continue
        item = dict(tc)
        if not item.get("id"):
            item["id"] = f"call_{idx}"
        if not item.get("type"):
            item["type"] = "function"
        fn = item.get("function")
        if isinstance(fn, dict):
            fn_copy = dict(fn)
            raw_args = fn_copy.get("arguments", {})
            if not isinstance(raw_args, str):
                fn_copy["arguments"] = json.dumps(raw_args)
            item["function"] = fn_copy
        else:
            raw_args = item.get("arguments", {})
            arg_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
            item["function"] = {
                "name": str(item.get("name") or ""),
                "arguments": arg_str,
            }
        normalized.append(item)
    return normalized


def format_outbound_messages(messages: Any) -> Any:
    """Project canonical OpenAI-style history into Ollama ``/api/chat`` shape."""
    if not isinstance(messages, (list, tuple)):
        return messages
    try:
        from src.core.context.context_builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
    except Exception:
        SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

    formatted: List[Dict[str, Any]] = []
    for raw_msg in messages:
        if not isinstance(raw_msg, dict):
            continue
        msg = {
            k: v
            for k, v in raw_msg.items()
            if k not in ("thinking_blocks", "reasoning_content")
        }
        role = msg.get("role")
        if role == "system" and isinstance(msg.get("content"), str):
            content = msg["content"]
            if SYSTEM_PROMPT_DYNAMIC_BOUNDARY in content:
                msg["content"] = content.replace(
                    f"\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\n", "\n\n"
                ).replace(SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "")
        elif role == "assistant" and isinstance(msg.get("tool_calls"), list):
            ollama_tcs: List[Dict[str, Any]] = []
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tc_copy = dict(tc)
                fn = tc_copy.get("function")
                if isinstance(fn, dict):
                    fn_copy = dict(fn)
                    raw_args = fn_copy.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            parsed = json.loads(raw_args)
                            fn_copy["arguments"] = (
                                parsed if isinstance(parsed, dict) else {"value": parsed}
                            )
                        except Exception:
                            fn_copy["arguments"] = {"raw_input": raw_args}
                    tc_copy["function"] = fn_copy
                ollama_tcs.append(tc_copy)
            msg["tool_calls"] = ollama_tcs
        elif role == "tool":
            if not msg.get("tool_name") and msg.get("name"):
                msg["tool_name"] = msg["name"]
        formatted.append(msg)
    return formatted


def extract_tool_calls_from_chat(chat_response: Any) -> List[Any]:
    """Extract structured or JSON-embedded tool calls from an Ollama chat response."""
    message = chat_response.get("message") if isinstance(chat_response, dict) else None
    if not message:
        return []
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        return tool_calls
    parsed = parse_json_response_field(message.get("content"))
    if isinstance(parsed, dict) and parsed.get("tool_call"):
        return [parsed.get("tool_call")]
    return []
