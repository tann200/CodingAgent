"""context_manager.py — Unified context management (merge of token_budget + auto_compactor).

ARCHITECTURE_V2: Merged from:
- TokenBudgetMonitor (token tracking, compaction triggers)
- auto_compactor (deterministic compaction, no LLM)

Context Layers (strict order):
1. SYSTEM (SOUL.md / base instructions)
2. TASK (current user goal)
3. WORKING MEMORY (recent steps)
4. TOOL RESULTS (compressed)
5. LONG-TERM MEMORY (top-K retrieval)

This module does NOT include LLM-based distillation (see distiller.py for that).
For small models, deterministic compaction (this module) is preferred.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
import threading

from src.core.memory.auto_compactor import (
    AutoCompactConfig,
    CompactResult,
    compact_messages,
    should_compact as _should_compact,
    estimate_messages_tokens,
)
from src.core.orchestration.token_budget import (
    TokenBudgetMonitor,
    TokenBudget,
    _UsageRatioMixin,
)

logger = logging.getLogger(__name__)


@dataclass
class ContextLayer:
    """One layer in the context stack."""

    name: str
    priority: int  # Lower = more protected
    tokens: int
    content: Optional[str] = None


@dataclass
class ContextBudget(_UsageRatioMixin):
    """Tracks context budget for a session."""

    session_id: str
    max_tokens: int
    used_tokens: int
    layers: list[ContextLayer]

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    @property
    def is_near_limit(self) -> bool:
        return self.usage_ratio >= 0.80

    @property
    def is_over_limit(self) -> bool:
        return self.usage_ratio >= 1.0


class ContextManager:
    """Unified context management: budget tracking + deterministic compaction.

    Replaces separate TokenBudgetMonitor + auto_compactor for v2 architecture.

    Usage:
        cm = ContextManager(session_id="abc", max_tokens=8192)
        cm.update_tokens(used=6000)

        if cm.should_compact():
            result = cm.compact(messages)
            messages = result.compacted_messages
    """

    _instance: Optional["ContextManager"] = None

    def __init__(
        self,
        session_id: str,
        max_tokens: int = 8192,
        warning_threshold: float = 0.70,
        compact_threshold: float = 0.85,
        preserve_recent: int = 4,
    ):
        self.session_id = session_id
        self.max_tokens = max_tokens
        self.warning_threshold = warning_threshold
        self.compact_threshold = compact_threshold
        self.preserve_recent = preserve_recent

        self._budget_monitor = TokenBudgetMonitor(
            warning_threshold=warning_threshold,
            compact_threshold=compact_threshold,
            min_turns_between_compact=3,
        )
        self._compact_config = AutoCompactConfig(
            preserve_recent=preserve_recent,
            max_tokens=int(max_tokens * compact_threshold),
        )

        self._current_turn = 0
        self._last_compact_turn = 0

    _instance_lock: threading.Lock = threading.Lock()

    @classmethod
    def get_instance(cls, session_id: str = "default") -> "ContextManager":
        with cls._instance_lock:
            if cls._instance is None or cls._instance.session_id != session_id:
                cls._instance = cls(session_id=session_id)
        return cls._instance

    def update_tokens(self, used: int, max_tokens: Optional[int] = None) -> None:
        """Update token usage for the session."""
        if max_tokens is not None:
            self.max_tokens = max_tokens
            self._compact_config.max_tokens = int(max_tokens * self.compact_threshold)
        self._budget_monitor.update(
            session_id=self.session_id,
            used_tokens=used,
            max_tokens=max_tokens,
            turn=self._current_turn,
        )

    def increment_turn(self) -> None:
        """Increment turn counter."""
        self._current_turn += 1

    def should_warn(self) -> bool:
        """Check if usage is above warning threshold."""
        budget = self._budget_monitor.get_budget(self.session_id)
        return budget.should_warn

    def should_compact(self, messages: Optional[list[dict]] = None) -> bool:
        """Check if compaction should trigger.

        If messages provided, uses deterministic check (auto_compactor).
        Otherwise uses ratio-based check (token_budget).
        """
        if messages is not None:
            return _should_compact(messages, self._compact_config)

        budget = self._budget_monitor.get_budget(self.session_id)
        if budget.usage_ratio < self.compact_threshold:
            return False
        if self._current_turn - self._last_compact_turn < 3:
            return False
        return True

    def compact(self, messages: list[dict]) -> CompactResult:
        """Compact messages deterministically.

        Returns CompactResult with:
        - summary: raw <summary> block
        - formatted_summary: human-readable version
        - compacted_messages: replacement message list
        - removed_message_count: number of messages removed
        """
        self._last_compact_turn = self._current_turn
        result = compact_messages(messages, self._compact_config)

        if result.removed_message_count > 0:
            logger.info(
                f"ContextManager: compacted {result.removed_message_count} messages "
                f"→ {len(result.compacted_messages)} messages"
            )

        return result

    def get_budget(self) -> TokenBudget:
        """Get current token budget state."""
        return self._budget_monitor.get_budget(self.session_id)

    def get_status(self) -> dict:
        """Get human-readable status dict."""
        budget = self.get_budget()
        return {
            "session_id": self.session_id,
            "max_tokens": self.max_tokens,
            "used_tokens": budget.used_tokens,
            "usage_ratio": f"{budget.usage_ratio:.1%}",
            "should_warn": budget.should_warn,
            "should_compact": self.should_compact(),
            "current_turn": self._current_turn,
            "last_compact_turn": self._last_compact_turn,
        }

    def estimate_messages(self, messages: list[dict]) -> int:
        """Estimate total tokens for messages."""
        return estimate_messages_tokens(messages)

    def admit_messages(
        self,
        messages: list[dict],
        force_compact: bool = False,
    ) -> tuple[list[dict], Optional[CompactResult]]:
        """Admit messages into context, compacting if needed.

        Returns (admitted_messages, compact_result).
        If no compaction needed, compact_result is None.
        """
        total_tokens = self.estimate_messages(messages)
        self.update_tokens(used=total_tokens)

        if self.should_compact(messages) or force_compact:
            result = self.compact(messages)
            return result.compacted_messages, result

        return messages, None

    def set_max_tokens(self, max_tokens: int) -> None:
        """Update max tokens (e.g., from runtime profile)."""
        self.max_tokens = max_tokens
        self._compact_config.max_tokens = int(max_tokens * self.compact_threshold)


def get_context_manager(
    session_id: str = "default",
    max_tokens: int = 8192,
) -> ContextManager:
    """Convenience: get or create a ContextManager instance."""
    return ContextManager.get_instance(session_id)


def quick_compact(
    messages: list[dict],
    max_tokens: int = 8192,
    preserve_recent: int = 4,
) -> CompactResult:
    """Quick compact without managing a session.

    Useful for one-off compaction checks.
    """
    config = AutoCompactConfig(
        preserve_recent=preserve_recent,
        max_tokens=int(max_tokens * 0.85),
    )
    return compact_messages(messages, config)
