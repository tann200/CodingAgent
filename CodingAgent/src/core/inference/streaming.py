from __future__ import annotations


from src.core.messaging.event_types import LLMToken, ModelToken, ResponseStreamChunk, ResponseStreamEnd
import asyncio
import concurrent.futures
import json
import logging
from typing import Any, AsyncIterator, Callable, List, Optional, Tuple

from .inference_timeout import (
    InferenceTimeoutError,
    InferenceTimeoutPolicy,
    get_default_policy,
    log_timeout_event,
)

_logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Sentinel used to signal StopIteration across thread boundaries
# ---------------------------------------------------------------------------

class _StopSentinel:
    """Returned by the thread-executor next() call when the iterator is done."""
    __slots__ = ()


_STOP = _StopSentinel()


def _sync_next(iterator: Any) -> Any:
    """Call next() on *iterator* in a thread.  Returns _STOP on StopIteration."""
    try:
        return next(iterator)
    except StopIteration:
        return _STOP


async def _iter_sync_in_executor(
    iterator: Any,
    loop: asyncio.AbstractEventLoop,
    executor: Any = None,
) -> AsyncIterator[Any]:
    """Wrap a blocking synchronous iterator as an async iterator.

    Each ``next()`` call is offloaded to *executor* (default thread pool) so
    that a blocking ``iter_lines()`` does not hold the event loop and
    ``asyncio.wait_for`` can fire.
    """
    while True:
        item = await loop.run_in_executor(executor, _sync_next, iterator)
        if isinstance(item, _StopSentinel):
            return
        yield item


# ---------------------------------------------------------------------------
# Async streaming helpers with first-token and idle-token deadlines
# ---------------------------------------------------------------------------


async def consume_stream_with_timeouts(
    lines_iter: Any,
    *,
    policy: Optional[InferenceTimeoutPolicy] = None,
    provider: Optional[str] = None,
    publish_chunk_fn: Callable[[str, bool], None],
    tag_split_enabled: bool = False,
    publish: Optional[Callable[[str, Any], None]] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    executor: Any = None,
) -> Tuple[List[str], Optional[InferenceTimeoutError]]:
    """Consume an async or sync lines iterator with first-token and idle-token
    deadlines applied only on actual token chunks (not heartbeats/empties).

    Returns ``(accumulated_parts, timeout_error_or_None)``.

    Key semantics:
    * The first *token* (a line that parses to a non-empty content/reasoning
      delta) must arrive within ``policy.first_token`` seconds.
    * Each subsequent *token* must arrive within ``policy.stream_idle`` seconds.
    * Heartbeat lines, empty lines, and malformed/non-data lines do NOT reset
      the deadline — they are transparently skipped while the same deadline
      continues to count down.
    * On timeout the raw response is closed if possible (``close()``/
      ``response.close()``), and a structured ``InferenceTimeoutError`` is
      returned as the second element — callers must NOT silently return a
      partial success.
    * Sync iterables have each ``next()`` call offloaded to a thread executor
      so blocking I/O cannot hold the event loop.

    Args:
        lines_iter:       An ``AsyncIterator``, async iterable, or sync iterable
                          (anything with ``__iter__`` / ``iter_lines``).
        policy:           Timeout policy; falls back to process default.
        provider:         Provider key for structured metadata.
        publish_chunk_fn: Called with ``(text, is_reasoning)`` for each token.
        tag_split_enabled: Enable ``<think>`` tag splitting.
        publish:          Optional ``(event, payload)`` callable for event bus.
        loop:             Event loop; defaults to ``asyncio.get_event_loop()``.
        executor:         Thread executor; defaults to loop default.
    """
    _policy = policy or get_default_policy()
    _loop = loop or asyncio.get_event_loop()
    accumulated: List[str] = []
    inside_think = False
    timeout_err: Optional[InferenceTimeoutError] = None
    # Tracks whether we have seen the first *token chunk* (not just any raw line)
    first_token_seen = False

    # ------------------------------------------------------------------
    # Build async iterator — pull sync iterables through the executor so
    # wait_for can interrupt blocking next() calls.
    # ------------------------------------------------------------------
    if hasattr(lines_iter, "__aiter__"):
        aiter: AsyncIterator[Any] = lines_iter.__aiter__()
    else:
        # Sync: could be an object with iter_lines() or a plain iterable
        if hasattr(lines_iter, "iter_lines"):
            sync_iter = lines_iter.iter_lines()
        else:
            sync_iter = iter(lines_iter)
        aiter = _iter_sync_in_executor(sync_iter, _loop, executor)

    # ------------------------------------------------------------------
    # Helper: await next raw line with the appropriate deadline.
    # The deadline is chosen BEFORE the await and does NOT reset for
    # heartbeat / empty / non-token lines — it only resets after a
    # confirmed token chunk has been processed.
    # ------------------------------------------------------------------
    async def _await_next_with_deadline(phase: str, secs: float) -> Any:
        """Return the next raw line, raising InferenceTimeoutError on expiry."""
        try:
            return await asyncio.wait_for(aiter.__anext__(), timeout=secs)
        except StopAsyncIteration:
            raise
        except asyncio.TimeoutError:
            raise InferenceTimeoutError(
                phase=phase,
                timeout_secs=secs,
                provider=provider,
            )

    # ------------------------------------------------------------------
    # Main loop — deadline restarts only when a token chunk is processed
    # ------------------------------------------------------------------
    phase = "first_token"
    secs = _policy.first_token

    while True:
        try:
            raw_line = await _await_next_with_deadline(phase, secs)
        except StopAsyncIteration:
            break
        except InferenceTimeoutError as exc:
            timeout_err = exc
            log_timeout_event(exc, publish=publish)
            # Attempt to close the raw response object to release the connection
            _close_response(lines_iter)
            break

        # ------ parse the raw line ----------------------------------------
        data = decode_sse_line(raw_line)
        if data is None:
            # Heartbeat, empty line, or non-SSE line — skip; do NOT reset deadline
            continue
        if data == "[DONE]":
            break

        chunk = parse_sse_chunk(data)
        if chunk is None:
            # Malformed JSON — skip; do NOT reset deadline
            continue

        extracted = extract_stream_deltas(chunk)
        if extracted is None:
            # Parseable JSON but no choices/delta — skip; do NOT reset deadline
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
                publish_chunk=publish_chunk_fn,
            )
            if prepublished_text:
                accumulated.extend(prepublished_text)
            if split_reasoning:
                reasoning_delta = split_reasoning

        if reasoning_delta:
            publish_chunk_fn(reasoning_delta, True)

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
            publish_chunk_fn(token_text, False)

        # Only reset deadline after a real token (content or reasoning)
        if token_text or reasoning_delta:
            first_token_seen = True
            phase = "stream_idle"
            secs = _policy.stream_idle

    return accumulated, timeout_err


def _close_response(obj: Any) -> None:
    """Best-effort close of a raw HTTP response or response-like object."""
    for attr in ("close", "response"):
        try:
            candidate = getattr(obj, attr, None)
            if callable(candidate):
                candidate()
                return
            # For obj.response.close()
            if candidate is not None:
                close_fn = getattr(candidate, "close", None)
                if callable(close_fn):
                    close_fn()
                    return
        except Exception:
            pass
