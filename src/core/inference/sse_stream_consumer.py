"""SSE stream consumer for OpenAI-compatible and reasoning LLM responses.

Extracted from ``src/core/inference/llm_manager.py`` (TASK-43).
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from src.core.inference.streaming import (
    StreamedTurnAccumulator,
    StreamedTurnText,
    decode_sse_line,
    extract_stream_deltas,
    finalize_stream,
    parse_sse_chunk,
    publish_stream_chunk,
    split_thinking_content,
)

_fallback_logger = logging.getLogger("coding_agent")


def consume_sse_stream(
    raw_response: Any,
    model: Optional[str] = None,
    *,
    logger: Any = None,
) -> str:
    """Iterate an OpenAI-compatible SSE stream, publish token/reasoning events, and return accumulated text.

    Three reasoning-detection sources are tried in order:
      1. ``delta.reasoning_content`` / ``delta.thinking`` -- structured field.
      2. ``<think>`` / ``</think>`` tag split inside ``delta.content``.
      3. ``delta.is_reasoning`` boolean flag (some providers).
    """
    log = logger if logger is not None else _fallback_logger
    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
    except Exception:
        bus = None

    model_id = model or ""
    try:
        from src.core.inference.thinking_utils import is_reasoning_model as _is_rm

        tag_split_enabled = _is_rm(model_id)
    except Exception:
        tag_split_enabled = False

    inside_think = False
    accumulated: List[str] = []
    turn_acc = StreamedTurnAccumulator()
    utf8_carry = bytearray()
    tag_buffer: List[str] = []

    def _publish_chunk(chunk: str, is_reasoning: bool) -> None:
        if is_reasoning and chunk:
            turn_acc.record_reasoning(chunk)
        publish_stream_chunk(bus=bus, chunk=chunk, is_reasoning=is_reasoning)

    try:
        for raw_line in raw_response.iter_lines():
            data = decode_sse_line(raw_line, carry=utf8_carry)
            if data is None:
                continue
            if data == "[DONE]":
                break
            chunk = parse_sse_chunk(data)
            if chunk is None:
                continue
            turn_acc.feed_chunk(chunk)
            extracted = extract_stream_deltas(chunk)
            if extracted is None:
                continue

            delta, reasoning_delta, content_delta = extracted
            used_original_content = False

            if not reasoning_delta:
                (
                    split_reasoning,
                    content_delta,
                    inside_think,
                    prepublished_text,
                    used_original_content,
                ) = split_thinking_content(
                    content_delta=content_delta,
                    inside_think=inside_think,
                    tag_split_enabled=tag_split_enabled,
                    publish_chunk=_publish_chunk,
                    tag_buffer=tag_buffer,
                )
                if prepublished_text:
                    accumulated.extend(prepublished_text)
                if split_reasoning:
                    reasoning_delta = split_reasoning

            if reasoning_delta:
                _publish_chunk(reasoning_delta, True)

            token_text = (
                content_delta
                if content_delta
                else (
                    delta.get("content") or ""
                    if not reasoning_delta and not used_original_content
                    else ""
                )
            )
            if token_text:
                accumulated.append(token_text)
                _publish_chunk(token_text, False)
        if tag_buffer:
            trailing = "".join(tag_buffer)
            tag_buffer.clear()
            if trailing:
                if inside_think:
                    _publish_chunk(trailing, True)
                else:
                    accumulated.append(trailing)
                    _publish_chunk(trailing, False)
    except Exception as e:
        log.warning(f"_consume_sse_stream: stream iteration error: {e}")

    full_text = finalize_stream(bus=bus, accumulated=accumulated)
    return StreamedTurnText(
        full_text,
        tool_calls=turn_acc.build_tool_calls(),
        thinking_blocks=turn_acc.build_thinking_blocks(),
        reasoning_content=turn_acc.reasoning_content,
        finish_reason=turn_acc.finish_reason,
        usage=turn_acc.usage,
    )
