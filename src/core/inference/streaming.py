from __future__ import annotations

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
        bus.publish("response.stream_chunk", {"chunk": chunk, "is_reasoning": is_reasoning})
        if not is_reasoning:
            bus.publish("model.token", {"text": chunk, "partial": True})
            bus.publish(
                "llm.token",
                {"text": chunk, "partial": True, "is_reasoning": False},
            )
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
            bus.publish("model.token", {"text": "", "partial": False, "full": full_text})
            bus.publish("llm.token", {"text": "", "partial": False, "full": full_text})
            bus.publish("response.stream_end", {"full_text": full_text})
        except Exception:
            pass
    return full_text
