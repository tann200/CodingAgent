from typing import List, Dict, Any, Optional
import re
import logging

logger = logging.getLogger(__name__)


class MessageManager:
    """Message manager that stores messages and enforces a token window.

    By default the manager stores messages in a list. When `max_tokens` is provided,
    the manager keeps the total estimated token count of stored messages below that
    threshold by dropping the oldest messages (system messages are preserved when possible).
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
        """Ensure the first message is a system message with the given content.

        If a system message already exists at the top and its content differs,
        replace it. Otherwise insert a new system message at the front.
        This ensures the system prompt is always present or refreshed each run.
        """
        try:
            if not self.messages:
                self.messages.insert(0, {"role": "system", "content": content})
                return
            first = self.messages[0]
            if isinstance(first, dict) and first.get("role") == "system":
                if first.get("content") != content:
                    self.messages[0] = {"role": "system", "content": content}
            else:
                # prepend
                self.messages.insert(0, {"role": "system", "content": content})
        except Exception:
            # never raise from message manager
            pass

    def _estimate_tokens(self, text: str) -> int:
        """Estimate tokens for a given text. Prefer tiktoken if available; otherwise use a regex proxy."""
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
        """Drop oldest non-system messages until total tokens <= max_tokens.

        Strategy:
        - Prefer to preserve the most recent messages.
        - System messages (role == 'system') are preserved when possible, but if the system messages alone exceed
          the window, older system messages will be removed last-resort.
        """
        if self.max_tokens is None:
            return
        # Quick check
        before_total = self._total_tokens()
        if before_total <= self.max_tokens:
            return

        kept = list(self.messages)
        dropped_count = 0
        dropped_tokens = 0
        # Iterate dropping the oldest message until within budget
        while kept and before_total > self.max_tokens:
            # find index of first droppable message (prefer non-system)
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

        # assign truncated list back
        self.messages = kept
        after_total = before_total

        # Emit telemetry / logging about truncation
        try:
            logger.info(f"MessageManager.truncate: dropped_count={dropped_count} dropped_tokens={dropped_tokens} tokens_after={after_total}")
        except Exception:
            pass
        try:
            if self.event_bus and hasattr(self.event_bus, 'publish'):
                payload = {
                    'dropped_count': dropped_count,
                    'dropped_tokens': dropped_tokens,
                    'tokens_after': after_total,
                }
                try:
                    self.event_bus.publish('message.truncation', payload)
                except Exception:
                    # don't let event failures break flow
                    logger.debug('Failed to publish message.truncation event')
        except Exception:
            pass
