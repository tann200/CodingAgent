from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


TOOL_OUTPUT_MAX_BYTES = 50_000
TOOL_LARGE_TEXT_FIELDS = ("output", "content", "diff", "text", "stdout", "stderr")


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
