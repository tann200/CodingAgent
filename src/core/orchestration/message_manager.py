from typing import List, Dict, Any, Callable, Optional
import re
import logging

logger = logging.getLogger(__name__)


class MessageManager:
    """Message manager that stores messages and enforces a token window.

    By default the manager stores messages in a list. When `max_tokens` is
    provided, the manager keeps the total estimated token count of stored
    messages below that threshold.

    When `compact_callback` is supplied, messages that would be dropped are
    first summarised by the callback and the summary is injected inline into
    the conversation (mimicking Claude Code / OpenCode compaction).  Without a
    callback the old silent-drop behaviour is preserved.

    CRITICAL LOCAL LLM FIX: When truncating, messages are dropped in
    User/Assistant pairs to prevent breaking the strict alternating role
    sequences required by local chat templates (like Llama 3 or Qwen).
    """

    # Reserve this many tokens for the inline compaction summary so it fits
    # after insertion without immediately triggering another truncation round.
    _COMPACT_BUDGET = 600

    def __init__(
        self,
        max_tokens: Optional[int] = None,
        event_bus: Optional[Any] = None,
        compact_callback: Optional[Callable[[List[Dict]], str]] = None,
    ):
        self.messages: List[Dict[str, Any]] = []
        self.max_tokens = int(max_tokens) if max_tokens is not None else None
        self.event_bus = event_bus
        # compact_callback(messages) -> prose summary string
        self.compact_callback = compact_callback

    def append(self, role: str, content: Any):
        # Normalize content to string for token estimation
        entry = {"role": role, "content": content}
        self.messages.append(entry)
        if self.max_tokens is not None:
            try:
                self._truncate_to_window()
            except Exception as e:
                # Truncation must not raise to callers; log for telemetry
                try:
                    logger.exception(f"MessageManager truncation failed: {e}")
                except Exception:
                    pass

    def all(self) -> List[Dict[str, Any]]:
        return list(self.messages)

    def clear(self):
        self.messages.clear()

    def set_system_prompt(self, content: str) -> None:
        """Ensure the first message is a system message with the given content."""
        try:
            if not self.messages:
                self.messages.insert(0, {"role": "system", "content": content})
                return
            first = self.messages[0]
            if isinstance(first, dict) and first.get("role") == "system":
                if first.get("content") != content:
                    self.messages[0] = {"role": "system", "content": content}
            else:
                self.messages.insert(0, {"role": "system", "content": content})
        except Exception:
            pass

    def _estimate_tokens(self, text: str) -> int:
        """Estimate tokens for a given text."""
        if not text:
            return 0
        try:
            import tiktoken
            try:
                enc = tiktoken.encoding_for_model("gpt-4")
            except Exception:
                try:
                    enc = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    enc = None
            if enc:
                return len(enc.encode(text))
        except Exception:
            pass
        # fallback proxy: split on words and punctuation
        try:
            toks = re.findall(r"\w+|[^\s\w]", text)
            return max(1, len(toks))
        except Exception:
            return max(1, len(text) // 4)

    def _total_tokens(self) -> int:
        total = 0
        for m in self.messages:
            c = m.get("content")
            s = c if isinstance(c, str) else str(c)
            total += self._estimate_tokens(s)
        return total

    def _truncate_to_window(self) -> None:
        """
        Drop oldest non-system messages until the conversation fits within
        max_tokens.

        When a compact_callback is configured the dropped messages are first
        summarised and the prose summary is inserted inline right after the
        system message, so the agent always has access to prior context
        without needing a tool call.  This mirrors the compaction behaviour of
        Claude Code / OpenCode / Kilocode.
        """
        if self.max_tokens is None:
            return

        before_total = self._total_tokens()
        if before_total <= self.max_tokens:
            return

        # When compaction is enabled we drop a little extra to leave room for
        # the summary message that will be inserted afterwards.
        target = (
            self.max_tokens - self._COMPACT_BUDGET
            if self.compact_callback
            else self.max_tokens
        )

        kept = list(self.messages)
        dropped_messages: List[Dict] = []
        dropped_tokens = 0

        while kept and before_total > target:
            # Find the oldest droppable (non-system) message
            drop_idx = None
            for i, m in enumerate(kept):
                if m.get("role") != "system":
                    drop_idx = i
                    break

            if drop_idx is None:
                # Only system messages remain.
                # Drop them only if we're still above the hard max_tokens limit,
                # not just to create a compact buffer.
                if before_total > self.max_tokens:
                    drop_idx = 0
                else:
                    break

            dropped = kept.pop(drop_idx)
            dropped_messages.append(dropped)
            c = dropped.get("content")
            s = c if isinstance(c, str) else str(c)
            tcount = self._estimate_tokens(s)
            before_total -= tcount
            dropped_tokens += tcount

            # FIX: If we just dropped a 'user' message, and the next available
            # non-system message is 'assistant' / 'tool', drop it too.
            # Local models crash if the alternating user→assistant sequence breaks.
            if dropped.get("role") == "user" and drop_idx < len(kept):
                next_msg = kept[drop_idx]
                if next_msg.get("role") in ("assistant", "tool"):
                    dropped_assoc = kept.pop(drop_idx)
                    dropped_messages.append(dropped_assoc)
                    assoc_c = dropped_assoc.get("content")
                    assoc_s = assoc_c if isinstance(assoc_c, str) else str(assoc_c)
                    assoc_tcount = self._estimate_tokens(assoc_s)
                    before_total -= assoc_tcount
                    dropped_tokens += assoc_tcount

        # ── Inline compaction ─────────────────────────────────────────────
        # If we have a compaction callback, summarise what was dropped and
        # inject the summary as a synthetic message right after the system
        # message so the agent can always see what happened before.
        if dropped_messages and self.compact_callback:
            try:
                summary = self.compact_callback(dropped_messages)
                if summary:
                    compact_msg = {
                        "role": "user",
                        "content": (
                            "<compacted_context>\n"
                            f"{summary}\n"
                            "</compacted_context>"
                        ),
                    }
                    # Insert directly after system message (index 0) so it is
                    # the oldest non-system message and persists as long as possible.
                    insert_idx = 1 if kept and kept[0].get("role") == "system" else 0
                    kept.insert(insert_idx, compact_msg)
                    logger.info(
                        f"MessageManager: inserted compacted_context "
                        f"({len(summary)} chars) replacing {len(dropped_messages)} msgs"
                    )
            except Exception as cb_err:
                logger.warning(f"MessageManager: compact_callback failed (non-fatal): {cb_err}")
        # ─────────────────────────────────────────────────────────────────

        self.messages = kept
        after_total = self._total_tokens()
        dropped_count = len(dropped_messages)

        try:
            logger.info(
                f"MessageManager.truncate: dropped_count={dropped_count} "
                f"dropped_tokens={dropped_tokens} tokens_after={after_total}"
            )
        except Exception:
            pass

        try:
            if self.event_bus and hasattr(self.event_bus, "publish"):
                payload = {
                    "dropped_count": dropped_count,
                    "dropped_tokens": dropped_tokens,
                    "tokens_after": after_total,
                }
                self.event_bus.publish("message.truncation", payload)
        except Exception:
            pass