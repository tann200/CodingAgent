from typing import List, Dict, Any, Optional
import re
import logging

logger = logging.getLogger(__name__)


class MessageManager:
    """Message manager that stores messages and enforces a token window.

    By default the manager stores messages in a list. When `max_tokens` is provided,
    the manager keeps the total estimated token count of stored messages below that
    threshold by dropping the oldest messages (system messages are preserved when possible).

    CRITICAL LOCAL LLM FIX: When truncating, this manager now drops messages in
    User/Assistant pairs to prevent breaking the strict alternating role sequences
    required by local chat templates (like Llama 3 or Qwen).
    """

    def __init__(self, max_tokens: Optional[int] = None, event_bus: Optional[Any] = None):
        self.messages: List[Dict[str, Any]] = []
        self.max_tokens = int(max_tokens) if max_tokens is not None else None
        self.event_bus = event_bus

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
        """Drop oldest non-system messages until total tokens <= max_tokens."""
        if self.max_tokens is None:
            return

        before_total = self._total_tokens()
        if before_total <= self.max_tokens:
            return

        kept = list(self.messages)
        dropped_count = 0
        dropped_tokens = 0

        while kept and before_total > self.max_tokens:
            # Find index of first droppable message (prefer non-system)
            drop_idx = None
            for i, m in enumerate(kept):
                if m.get("role") != "system":
                    drop_idx = i
                    break

            if drop_idx is None:
                # all remaining are system messages; drop the oldest
                drop_idx = 0

            dropped = kept.pop(drop_idx)
            c = dropped.get("content")
            s = c if isinstance(c, str) else str(c)
            tcount = self._estimate_tokens(s)
            before_total -= tcount
            dropped_count += 1
            dropped_tokens += tcount

            # FIX: If we just dropped a 'user' message, and the next available
            # non-system message is 'assistant', we MUST drop it too.
            # Local models crash if the alternating 'user -> assistant' sequence is broken.
            if dropped.get("role") == "user" and drop_idx < len(kept):
                next_msg = kept[drop_idx]
                if next_msg.get("role") == "assistant" or next_msg.get("role") == "tool":
                    dropped_assoc = kept.pop(drop_idx)
                    assoc_c = dropped_assoc.get("content")
                    assoc_s = assoc_c if isinstance(assoc_c, str) else str(assoc_c)
                    assoc_tcount = self._estimate_tokens(assoc_s)
                    before_total -= assoc_tcount
                    dropped_count += 1
                    dropped_tokens += assoc_tcount

        self.messages = kept
        after_total = before_total

        try:
            logger.info(
                f"MessageManager.truncate: dropped_count={dropped_count} dropped_tokens={dropped_tokens} tokens_after={after_total}")
        except Exception:
            pass

        try:
            if self.event_bus and hasattr(self.event_bus, 'publish'):
                payload = {
                    'dropped_count': dropped_count,
                    'dropped_tokens': dropped_tokens,
                    'tokens_after': after_total,
                }
                self.event_bus.publish('message.truncation', payload)
        except Exception:
            pass