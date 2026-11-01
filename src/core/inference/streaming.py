from __future__ import annotations


from src.core.messaging.event_types import LLMToken, ModelToken, ResponseStreamChunk, ResponseStreamEnd
import json
from typing import Any, Callable, Dict, List, Optional, Tuple


class StreamedTurnText(str):
    """``str`` subclass returned by SSE streaming with structured turn metadata.

    Preserves ``isinstance(val, str)`` and equality with plain strings while
    exposing accumulated ``tool_calls``, ``thinking_blocks``,
    ``reasoning_content``, ``finish_reason``, and ``usage`` so streamed turns
    never drop native function calls or signed thinking blocks.
    """

    tool_calls: List[Dict[str, Any]]
    thinking_blocks: List[Dict[str, Any]]
    reasoning_content: str
    finish_reason: str
    usage: Dict[str, int]

    def __new__(
        cls,
        text: str,
        *,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        thinking_blocks: Optional[List[Dict[str, Any]]] = None,
        reasoning_content: str = "",
        finish_reason: str = "stop",
        usage: Optional[Dict[str, int]] = None,
    ) -> "StreamedTurnText":
        obj = super().__new__(cls, text)
        obj.tool_calls = list(tool_calls or [])
        obj.thinking_blocks = list(thinking_blocks or [])
        obj.reasoning_content = reasoning_content
        obj.finish_reason = finish_reason or "stop"
        obj.usage = dict(usage or {})
        return obj


class StreamedTurnAccumulator:
    """Accumulates OpenAI and Anthropic SSE deltas for tool calls, thinking, and usage."""

    def __init__(self) -> None:
        self._openai_tool_calls: Dict[int, Dict[str, Any]] = {}
        self._anthropic_blocks: Dict[int, Dict[str, Any]] = {}
        self._reasoning_parts: List[str] = []
        self.finish_reason: str = "stop"
        self.usage: Dict[str, int] = {}

    def record_reasoning(self, text: str) -> None:
        if text:
            self._reasoning_parts.append(text)

    def feed_chunk(self, chunk: Dict[str, Any]) -> None:
        """Process one parsed SSE JSON object (OpenAI or Anthropic shape)."""
        if not isinstance(chunk, dict):
            return

        # 1. Anthropic SSE event shapes
        chunk_type = chunk.get("type")
        if chunk_type:
            if chunk_type == "message_start":
                msg = chunk.get("message") or {}
                u = msg.get("usage") if isinstance(msg, dict) else None
                if isinstance(u, dict):
                    if "input_tokens" in u:
                        self.usage["prompt_tokens"] = int(u.get("input_tokens") or 0)
                    if "cache_creation_input_tokens" in u:
                        self.usage["cache_creation_input_tokens"] = int(
                            u.get("cache_creation_input_tokens") or 0
                        )
                    if "cache_read_input_tokens" in u:
                        self.usage["cache_read_input_tokens"] = int(
                            u.get("cache_read_input_tokens") or 0
                        )
            elif chunk_type == "content_block_start":
                idx = int(chunk.get("index", 0) or 0)
                cb = chunk.get("content_block") or {}
                if isinstance(cb, dict):
                    cb_type = cb.get("type")
                    if cb_type == "tool_use":
                        init_input = cb.get("input")
                        init_parts: List[str] = []
                        if isinstance(init_input, dict) and init_input:
                            init_parts.append(json.dumps(init_input))
                        self._anthropic_blocks[idx] = {
                            "type": "tool_use",
                            "id": str(cb.get("id") or f"toolu_{idx}"),
                            "name": str(cb.get("name") or ""),
                            "json_parts": init_parts,
                        }
                    elif cb_type == "thinking":
                        init_think = str(cb.get("thinking") or "")
                        self._anthropic_blocks[idx] = {
                            "type": "thinking",
                            "thinking_parts": [init_think] if init_think else [],
                            "signature_parts": (
                                [str(cb["signature"])] if cb.get("signature") else []
                            ),
                        }
                    elif cb_type == "redacted_thinking":
                        self._anthropic_blocks[idx] = {
                            "type": "redacted_thinking",
                            "data": str(cb.get("data") or ""),
                        }
            elif chunk_type == "content_block_delta":
                idx = int(chunk.get("index", 0) or 0)
                delta = chunk.get("delta") or {}
                blk = self._anthropic_blocks.get(idx)
                if isinstance(delta, dict) and isinstance(blk, dict):
                    dtype = delta.get("type")
                    if dtype == "input_json_delta" and blk.get("type") == "tool_use":
                        part = delta.get("partial_json")
                        if part:
                            blk["json_parts"].append(str(part))
                    elif dtype == "thinking_delta" and blk.get("type") == "thinking":
                        tpart = delta.get("thinking")
                        if tpart:
                            blk["thinking_parts"].append(str(tpart))
                    elif dtype == "signature_delta" and blk.get("type") == "thinking":
                        spart = delta.get("signature")
                        if spart:
                            blk["signature_parts"].append(str(spart))
            elif chunk_type == "message_delta":
                delta = chunk.get("delta") or {}
                if isinstance(delta, dict):
                    sr = delta.get("stop_reason")
                    if sr == "tool_use":
                        self.finish_reason = "tool_calls"
                    elif sr == "max_tokens":
                        self.finish_reason = "length"
                    elif sr:
                        self.finish_reason = "stop"
                u = chunk.get("usage")
                if isinstance(u, dict) and "output_tokens" in u:
                    self.usage["completion_tokens"] = int(u.get("output_tokens") or 0)
            return

        # 2. OpenAI-compatible SSE chunk shapes
        u = chunk.get("usage")
        if isinstance(u, dict):
            if "prompt_tokens" in u:
                self.usage["prompt_tokens"] = int(u.get("prompt_tokens") or 0)
            if "completion_tokens" in u:
                self.usage["completion_tokens"] = int(u.get("completion_tokens") or 0)
            if "cache_creation_input_tokens" in u:
                self.usage["cache_creation_input_tokens"] = int(
                    u.get("cache_creation_input_tokens") or 0
                )
            if "cache_read_input_tokens" in u:
                self.usage["cache_read_input_tokens"] = int(
                    u.get("cache_read_input_tokens") or 0
                )

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        fr = c0.get("finish_reason")
        if fr:
            self.finish_reason = str(fr)
        delta = c0.get("delta")
        if not isinstance(delta, dict):
            return
        delta_tcs = delta.get("tool_calls")
        if isinstance(delta_tcs, list):
            for pos, tc in enumerate(delta_tcs):
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index", pos) or 0)
                slot = self._openai_tool_calls.setdefault(
                    idx,
                    {
                        "id": "",
                        "type": "function",
                        "name_parts": [],
                        "arg_parts": [],
                    },
                )
                if tc.get("id"):
                    slot["id"] = str(tc["id"])
                fn = tc.get("function")
                if isinstance(fn, dict):
                    if fn.get("name"):
                        slot["name_parts"].append(str(fn["name"]))
                    if fn.get("arguments") is not None:
                        slot["arg_parts"].append(str(fn["arguments"]))

    def build_tool_calls(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for idx in sorted(self._openai_tool_calls.keys()):
            slot = self._openai_tool_calls[idx]
            fn_name = "".join(slot["name_parts"])
            fn_args = "".join(slot["arg_parts"]) or "{}"
            out.append(
                {
                    "id": slot["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": fn_args,
                    },
                }
            )
        for idx in sorted(self._anthropic_blocks.keys()):
            blk = self._anthropic_blocks[idx]
            if blk.get("type") == "tool_use":
                raw_json = "".join(blk.get("json_parts") or []).strip() or "{}"
                out.append(
                    {
                        "id": blk.get("id") or f"toolu_{idx}",
                        "type": "function",
                        "function": {
                            "name": blk.get("name") or "",
                            "arguments": raw_json,
                        },
                    }
                )
        return out

    def build_thinking_blocks(self) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        for idx in sorted(self._anthropic_blocks.keys()):
            blk = self._anthropic_blocks[idx]
            btype = blk.get("type")
            if btype == "thinking":
                entry: Dict[str, Any] = {
                    "type": "thinking",
                    "thinking": "".join(blk.get("thinking_parts") or []),
                }
                sig = "".join(blk.get("signature_parts") or [])
                if sig:
                    entry["signature"] = sig
                blocks.append(entry)
            elif btype == "redacted_thinking":
                blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": blk.get("data") or "",
                    }
                )
        return blocks

    @property
    def reasoning_content(self) -> str:
        return "".join(self._reasoning_parts)

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
                elif delta_type in ("input_json_delta", "signature_delta"):
                    # Accumulated as structured tool_calls / signature in StreamedTurnAccumulator
                    return delta, "", ""
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

    normalized_tool_calls: List[dict[str, Any]] = []
    for idx, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            continue
        item = dict(tc)
        if not item.get("id"):
            item["id"] = f"call_{idx}"
        if not item.get("type"):
            item["type"] = "function"
        if "function" not in item or not isinstance(item.get("function"), dict):
            raw_args = item.get("arguments", {})
            arg_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
            item["function"] = {
                "name": str(item.get("name") or ""),
                "arguments": arg_str,
            }
        else:
            fn_copy = dict(item["function"])
            raw_args = fn_copy.get("arguments", {})
            if not isinstance(raw_args, str):
                fn_copy["arguments"] = json.dumps(raw_args)
            item["function"] = fn_copy
        normalized_tool_calls.append(item)

    choice: dict[str, Any] = {
        "message": {
            "role": role,
            "content": full_text,
        },
        "finish_reason": "tool_calls" if normalized_tool_calls and finish_reason == "stop" else finish_reason,
    }
    if normalized_tool_calls:
        choice["tool_calls"] = normalized_tool_calls
        choice["message"]["tool_calls"] = normalized_tool_calls

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
