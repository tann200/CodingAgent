"""Cross-model conversation history normalization (TASK-36).

When switching providers or models mid-session (via ``/model``, ``/provider``,
CLI override over ``--resume``, or model escalation), native tool-call payloads,
provider-specific call IDs, and thinking/reasoning signatures often trigger
HTTP 400 validation errors on the destination provider.

``normalize_history_for_model_switch()`` converts native ``tool_calls`` /
``tool`` / Anthropic ``tool_use`` / ``tool_result`` turns into clean labeled
text blocks, strips provider-specific reasoning signatures and cache markers,
and coalesces adjacent same-role turns while keeping system messages intact.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence

_STRIPPED_TOP_LEVEL_KEYS = frozenset(
    {
        "tool_calls",
        "tool_call_id",
        "function_call",
        "reasoning_content",
        "reasoning_details",
        "reasoning",
        "thinking",
        "signature",
        "redacted_thinking",
        "cache_control",
        "id",
        "name",
    }
)

_STRIPPED_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n... [truncated {len(text) - limit} chars]"


def _format_tool_call_summary(name: str, raw_args: Any) -> str:
    clean_name = str(name or "unknown_tool").strip() or "unknown_tool"
    if isinstance(raw_args, str):
        args_str = raw_args.strip()
        try:
            parsed = json.loads(args_str)
            args_str = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except Exception:
            pass
    elif isinstance(raw_args, Mapping):
        args_str = json.dumps(dict(raw_args), ensure_ascii=False, sort_keys=True, default=str)
    else:
        args_str = str(raw_args or "{}")
    args_str = _truncate_text(args_str, 400)
    return f"[Prior tool call: {clean_name}({args_str})]"


def _extract_content_parts(
    content: Any,
    *,
    max_tool_result_chars: int,
) -> tuple[List[str], List[str]]:
    """Return ``(assistant_or_text_parts, tool_result_parts)`` from *content*."""
    text_parts: List[str] = []
    tool_result_parts: List[str] = []

    if isinstance(content, str):
        if content.strip():
            text_parts.append(content)
        return text_parts, tool_result_parts

    if not isinstance(content, list):
        rendered = str(content or "").strip()
        if rendered:
            text_parts.append(rendered)
        return text_parts, tool_result_parts

    for block in content:
        if isinstance(block, str):
            if block.strip():
                text_parts.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if btype in _STRIPPED_BLOCK_TYPES:
            continue
        if btype == "tool_use":
            text_parts.append(
                _format_tool_call_summary(
                    str(block.get("name") or "unknown_tool"),
                    block.get("input") or {},
                )
            )
            continue
        if btype == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner_str = "\n".join(
                    str(p.get("text") or p.get("content") or "")
                    if isinstance(p, Mapping)
                    else str(p)
                    for p in inner
                )
            else:
                inner_str = str(inner or "")
            label = str(block.get("name") or block.get("tool_name") or "tool")
            tool_result_parts.append(
                f"[Prior tool result ({label}): {_truncate_text(inner_str.strip(), max_tool_result_chars)}]"
            )
            continue
        txt = block.get("text") if "text" in block else block.get("content")
        if isinstance(txt, str) and txt.strip():
            text_parts.append(txt)

    return text_parts, tool_result_parts


def _format_openai_tool_calls(tool_calls: Any) -> List[str]:
    if not isinstance(tool_calls, list):
        return []
    lines: List[str] = []
    for tc in tool_calls:
        if not isinstance(tc, Mapping):
            continue
        fn = tc.get("function")
        if isinstance(fn, Mapping):
            name = str(fn.get("name") or tc.get("name") or "unknown_tool")
            args = fn.get("arguments", {})
        else:
            name = str(tc.get("name") or "unknown_tool")
            args = tc.get("arguments", tc.get("input", {}))
        lines.append(_format_tool_call_summary(name, args))
    return lines


def normalize_history_for_model_switch(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_tool_result_chars: int = 2000,
) -> List[Dict[str, Any]]:
    """Normalize conversation *messages* for safe handoff to a different model/provider.

    - Preserves ``system`` messages (stripping ``cache_control`` / provider IDs).
    - Converts assistant ``tool_calls`` / Anthropic ``tool_use`` blocks into
      labeled plain-text summaries.
    - Converts ``tool`` role messages and Anthropic ``tool_result`` blocks into
      labeled ``user`` text blocks.
    - Strips provider-specific reasoning/thinking blocks, signatures, and call IDs.
    - Coalesces consecutive ``user`` or ``assistant`` messages so strict
      alternating-role APIs accept the history cleanly.
    """
    normalized: List[Dict[str, Any]] = []

    def _push(role: str, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if normalized and normalized[-1]["role"] == role and role in ("user", "assistant"):
            normalized[-1]["content"] = f"{normalized[-1]['content']}\n\n{cleaned}"
        else:
            normalized.append({"role": role, "content": cleaned})

    for raw in messages:
        if not isinstance(raw, Mapping):
            continue
        role = str(raw.get("role") or "user").strip().lower()

        if role == "system":
            text_parts, _ = _extract_content_parts(
                raw.get("content", ""), max_tool_result_chars=max_tool_result_chars
            )
            if text_parts:
                normalized.append({"role": "system", "content": "\n\n".join(text_parts)})
            continue

        if role in ("tool", "function"):
            tname = str(raw.get("name") or raw.get("tool_name") or "tool")
            text_parts, tr_parts = _extract_content_parts(
                raw.get("content", ""), max_tool_result_chars=max_tool_result_chars
            )
            body = "\n".join(text_parts + tr_parts).strip()
            body = _truncate_text(body, max_tool_result_chars)
            _push("user", f"[Prior tool result ({tname}): {body}]")
            continue

        if role == "assistant":
            text_parts, tr_parts = _extract_content_parts(
                raw.get("content", ""), max_tool_result_chars=max_tool_result_chars
            )
            call_lines = _format_openai_tool_calls(raw.get("tool_calls"))
            fn_call = raw.get("function_call")
            if isinstance(fn_call, Mapping):
                call_lines.append(
                    _format_tool_call_summary(
                        str(fn_call.get("name") or "unknown_tool"),
                        fn_call.get("arguments", {}),
                    )
                )
            combined = "\n".join(text_parts + call_lines).strip()
            if combined:
                _push("assistant", combined)
            if tr_parts:
                _push("user", "\n".join(tr_parts))
            continue

        # Default: user / human / other -> user
        text_parts, tr_parts = _extract_content_parts(
            raw.get("content", ""), max_tool_result_chars=max_tool_result_chars
        )
        combined_user = "\n".join(text_parts + tr_parts).strip()
        if combined_user:
            _push("user", combined_user)

    return normalized


__all__ = ["normalize_history_for_model_switch"]
