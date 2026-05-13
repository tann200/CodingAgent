from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence


TOOL_OUTPUT_MAX_BYTES = 50_000
TOOL_LARGE_TEXT_FIELDS = ("output", "content", "diff", "text", "stdout", "stderr")

# Cheap tool-output pruning (P1): replaces old tool-result content with a
# short placeholder once the running token budget is exceeded, preventing
# unbounded context growth without an expensive LLM compaction call.
_PRUNED_TOOL_PLACEHOLDER = "[Old tool result content cleared to save context]"
_PRUNE_PROTECT_TOKENS = 40_000
_PRUNE_PROTECT_RECENT = 6


def prune_tool_outputs(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Zero out old tool-result content beyond the token-protect boundary.

    Walks history newest-to-oldest.  Once the running token count exceeds
    ``_PRUNE_PROTECT_TOKENS``, old tool/user messages containing a
    ``tool_execution_result`` have their content replaced with a short
    placeholder.  The most recent ``_PRUNE_PROTECT_RECENT`` messages are
    always preserved.
    """
    if not history:
        return history

    def _est(msg: Dict[str, Any]) -> int:
        c = msg.get("content") or ""
        s = c if isinstance(c, str) else str(c)
        return max(1, len(s) // 4)

    def _is_tool_result(msg: Dict[str, Any]) -> bool:
        if msg.get("role") == "tool":
            return True
        if msg.get("role") == "user":
            c = msg.get("content") or ""
            return "tool_execution_result" in (c if isinstance(c, str) else str(c))
        return False

    result = list(history)
    total = len(result)
    running_tokens = 0

    for i in range(total - 1, -1, -1):
        if (total - 1 - i) < _PRUNE_PROTECT_RECENT:
            running_tokens += _est(result[i])
            continue
        running_tokens += _est(result[i])
        if running_tokens > _PRUNE_PROTECT_TOKENS and _is_tool_result(result[i]):
            msg = result[i]
            if msg.get("content") != _PRUNED_TOOL_PLACEHOLDER:
                result[i] = {**msg, "content": _PRUNED_TOOL_PLACEHOLDER}

    return result


def truncate_tool_output(
    result: Mapping[str, Any],
    *,
    marker_label: str,
    logger: Any = None,
    max_bytes: int = TOOL_OUTPUT_MAX_BYTES,
    large_text_fields: Sequence[str] = TOOL_LARGE_TEXT_FIELDS,
) -> dict:
    """Cap large tool-result text fields before they enter model history."""
    try:
        serialized = json.dumps(result, default=str)
    except Exception:
        return result if isinstance(result, dict) else dict(result)
    if len(serialized.encode("utf-8", errors="replace")) <= max_bytes:
        return dict(result) if not isinstance(result, dict) else result

    truncated = dict(result)
    for field in large_text_fields:
        value = truncated.get(field)
        if not isinstance(value, str) or len(value) < 500:
            continue
        try:
            current_size = len(
                json.dumps(truncated, default=str).encode("utf-8", errors="replace")
            )
        except Exception:
            break
        if current_size <= max_bytes:
            break
        excess = current_size - max_bytes
        new_len = max(200, len(value) - excess - 80)
        omitted = len(value) - new_len
        truncated[field] = (
            value[:new_len]
            + f"\n…[{marker_label}: {omitted} chars truncated — output exceeded 50 KB limit]"
        )
        truncated["_output_truncated"] = True
        if logger is not None:
            try:
                logger.debug(
                    "truncate_tool_output: truncated field=%r by %d chars (was %d B over limit)",
                    field,
                    omitted,
                    excess,
                )
            except Exception:
                pass
    return truncated
