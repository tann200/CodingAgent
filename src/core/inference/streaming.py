from __future__ import annotations


from src.core.messaging.event_types import LLMToken, ModelToken, ResponseStreamChunk, ResponseStreamEnd
import json
from typing import Any, Callable, List, Optional, Tuple

def decode_sse_line(
    raw_line: Any,
    *,
    carry: Optional[bytearray] = None,
) -> Optional[str]:
    """Decode one SSE line, handling multi-byte UTF-8 sequences split across chunks.

    Args:
        raw_line: Either a ``str`` or ``bytes``/``bytearray`` chunk.
        carry: Mutable ``bytearray`` used to buffer an incomplete UTF-8 sequence
               from the previous call.  Pass the same object on every call for a
               given SSE stream.  Ignored when *raw_line* is already a ``str``.

    Returns:
        The ``data:`` payload (str), ``"[DONE]"``, or ``None``.
    """
    if not raw_line:
        return None
    if isinstance(raw_line, str):
        line = raw_line
    else:
        # C-01: prepend any leftover bytes from previous chunk, then try to
        # decode.  If the sequence is still incomplete, stash the tail in
        # *carry* and return None so the caller waits for the next chunk.
        buf = (carry + raw_line) if carry is not None else bytearray(raw_line)
        try:
            line = buf.decode("utf-8")
            if carry is not None:
                carry.clear()
        except UnicodeDecodeError as exc:
            # Keep the incomplete tail for the next chunk.
            tail = buf[exc.start :]
            if carry is not None:
                carry.clear()
                carry.extend(tail)
            try:
                line = buf[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not line:
                return None
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return "[DONE]"
    return data


def parse_sse_chunk(data: str) -> Optional[dict]:
    try:
        chunk = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(chunk, dict):
        return None
    return chunk


def extract_stream_deltas(chunk: dict) -> Optional[Tuple[dict, str, str]]:
    try:
        # SPEC-14: Anthropic SSE event shapes
        chunk_type = chunk.get("type")
        if chunk_type:
            delta = chunk.get("delta") or {}
            delta_type = delta.get("type")
            if chunk_type == "content_block_delta":
                if delta_type == "text_delta":
                    return delta, "", delta.get("text") or ""
                elif delta_type == "thinking_delta":
                    return delta, delta.get("thinking") or "", ""
                elif delta_type == "input_json_delta":
                    return delta, "", delta.get("partial_json") or ""
                return delta, "", delta.get("text") or ""
            elif chunk_type == "content_block_start":
                cb = chunk.get("content_block") or {}
                if cb.get("type") == "text":
                    return cb, "", cb.get("text") or ""
                return cb, "", ""
            elif chunk_type in (
                "message_start",
                "message_delta",
                "message_stop",
                "content_block_stop",
                "ping",
            ):
                return delta, "", ""

        choices = chunk.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        reasoning_delta = delta.get("reasoning_content") or delta.get("thinking") or ""
        content = delta.get("content") or ""
        if not reasoning_delta and delta.get("is_reasoning"):
            return delta, reasoning_delta, content
        return delta, reasoning_delta, content
    except (KeyError, IndexError, AttributeError):
        return None


def publish_stream_chunk(*, bus: Any, chunk: str, is_reasoning: bool) -> None:
    if not bus or not chunk:
        return
    try:
        bus.publish_typed(ResponseStreamChunk(chunk=chunk, is_reasoning=is_reasoning))
        if not is_reasoning:
            bus.publish_typed(ModelToken(text=chunk, partial=True))
            bus.publish_typed(LLMToken(text=chunk, partial=True, is_reasoning=False))
    except Exception:
        pass


def split_thinking_content(
    *,
    content_delta: str,
    inside_think: bool,
    tag_split_enabled: bool,
    publish_chunk: Callable[[str, bool], None],
) -> Tuple[str, str, bool, List[str], bool]:
    reasoning_delta = ""
    text_parts: List[str] = []
    consumed_original = False

    if not tag_split_enabled or not content_delta:
        return reasoning_delta, content_delta, inside_think, text_parts, consumed_original

    if "<think>" in content_delta and not inside_think:
        before, _, rest = content_delta.partition("<think>")
        inside_think = True
        consumed_original = True
        if before:
            publish_chunk(before, False)
            text_parts.append(before)
        if "</think>" in rest:
            think_part, _, after = rest.partition("</think>")
            inside_think = False
            if think_part:
                publish_chunk(think_part, True)
            content_delta = after
        else:
            if rest:
                publish_chunk(rest, True)
            content_delta = ""
    elif "</think>" in content_delta and inside_think:
        think_part, _, after = content_delta.partition("</think>")
        inside_think = False
        consumed_original = True
        if think_part:
            publish_chunk(think_part, True)
        content_delta = after
    elif inside_think:
        reasoning_delta = content_delta
        content_delta = ""
        consumed_original = True

    return reasoning_delta, content_delta, inside_think, text_parts, consumed_original


def finalize_stream(*, bus: Any, accumulated: List[str]) -> str:
    full_text = "".join(accumulated)
    if bus and full_text:
        try:
            bus.publish_typed(ModelToken(text="", partial=False, full=full_text))
            bus.publish_typed(LLMToken(text="", partial=False, full=full_text))
            bus.publish_typed(ResponseStreamEnd(full_text=full_text))
        except Exception:
            pass
    return full_text


def consume_generator_stream(gen: Any, model: Optional[str] = None) -> dict[str, Any]:
    """Iterate a generator of chunks (e.g. from OllamaAdapter), publish events, and return result."""
    accumulated_content: List[str] = []
    p_tokens = 0
    c_tokens = 0
    role = "assistant"
    tool_calls: List[dict[str, Any]] = []
    finish_reason = "stop"

    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
    except Exception:
        bus = None

    for chunk in gen:
        if isinstance(chunk, dict):
            msg = chunk.get("message") or {}
            delta_text = msg.get("content") or ""
            if not delta_text and "response" in chunk:
                delta_text = chunk.get("response") or ""
            if delta_text:
                accumulated_content.append(delta_text)
                publish_stream_chunk(bus=bus, chunk=delta_text, is_reasoning=False)
            if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                tool_calls.extend(msg["tool_calls"])
            if chunk.get("prompt_eval_count"):
                p_tokens = int(chunk["prompt_eval_count"])
            if chunk.get("eval_count"):
                c_tokens = int(chunk["eval_count"])
            if chunk.get("done_reason"):
                finish_reason = str(chunk["done_reason"])
        elif isinstance(chunk, str):
            if chunk:
                accumulated_content.append(chunk)
                publish_stream_chunk(bus=bus, chunk=chunk, is_reasoning=False)

    full_text = finalize_stream(bus=bus, accumulated=accumulated_content)

    choice: dict[str, Any] = {
        "message": {
            "role": role,
            "content": full_text,
        },
        "finish_reason": finish_reason,
    }
    if tool_calls:
        choice["tool_calls"] = tool_calls
        choice["message"]["tool_calls"] = tool_calls

    return {
        "ok": True,
        "provider": "ollama",
        "model": model or "unknown",
        "latency": 0.0,
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "total_tokens": p_tokens + c_tokens,
        "choices": [choice],
        "text": full_text,
        "streamed": True,
    }
