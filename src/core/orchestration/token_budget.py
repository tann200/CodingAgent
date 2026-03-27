"""
Token budget monitoring for auto-compaction.
Integrates with existing memory_update_node and distill_context.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TokenBudget:
    """Tracks token usage and compaction history."""

    used_tokens: int
    max_tokens: int
    warning_threshold: float = 0.70
    compact_threshold: float = 0.85
    last_compact_turn: int = 0
    current_turn: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def usage_ratio(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return self.used_tokens / self.max_tokens

    @property
    def should_warn(self) -> bool:
        return self.usage_ratio >= self.warning_threshold

    @property
    def should_compact(self) -> bool:
        if self.usage_ratio < self.compact_threshold:
            return False
        if self.current_turn - self.last_compact_turn < 5:
            return False
        return True

    def record_compaction(self):
        self.last_compact_turn = self.current_turn


class TokenBudgetMonitor:
    """
    Monitors token budget and triggers compaction via existing distillation.

    Usage:
        monitor = TokenBudgetMonitor()
        monitor.update(used=5000, max=6000, turn=10)

        if monitor.should_compact:
            state["_should_distill"] = True
    """

    _instance = None

    def __init__(
        self,
        warning_threshold: float = 0.70,
        compact_threshold: float = 0.85,
        min_turns_between_compact: int = 5,
    ):
        self.warning_threshold = warning_threshold
        self.compact_threshold = compact_threshold
        self.min_turns = min_turns_between_compact
        self._budgets: dict[str, TokenBudget] = {}

    @classmethod
    def get_instance(cls) -> "TokenBudgetMonitor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_budget(self, session_id: str) -> TokenBudget:
        if session_id not in self._budgets:
            # CF-6 fix: use a realistic default (32,768) rather than 6,000.
            # 6,000 caused max_tokens to be overwritten with the current token
            # count on the first update(), making usage_ratio always 1.0.
            self._budgets[session_id] = TokenBudget(
                used_tokens=0,
                max_tokens=32_768,
                warning_threshold=self.warning_threshold,
                compact_threshold=self.compact_threshold,
            )
        return self._budgets[session_id]

    def update(
        self,
        session_id: str,
        used_tokens: int,
        max_tokens: Optional[int] = None,
        turn: Optional[int] = None,
    ):
        """Update budget for a session."""
        budget = self.get_budget(session_id)
        budget.used_tokens = used_tokens
        if max_tokens:
            budget.max_tokens = max_tokens
        if turn is not None:
            budget.current_turn = turn

    def check_budget(self, state, orchestrator=None) -> str:
        """
        Check if compaction should trigger.

        Evaluates AgentState.history BEFORE ContextBuilder truncates it.
        """
        history = state.get("history", [])

        raw_tokens = sum(self._estimate_tokens(m) for m in history)

        p2p_context = state.get("_p2p_context", [])
        p2p_tokens = sum(self._estimate_tokens(m) for m in p2p_context)

        total_raw = raw_tokens + p2p_tokens

        session_id = state.get("session_id", "default")

        # CF-6 fix: do NOT grow max_tokens to match current usage — that makes
        # usage_ratio always ≈ 1.0 and triggers compaction on every turn.
        # Instead use a fixed default (32,768 tokens — typical for modern local
        # models) unless the orchestrator has provided a provider-specific value.
        # The provider context window can be supplied via state["_context_budget"]
        # (set by provider_context.get_context_budget()) or as an explicit update()
        # call from llm_manager after the first call.
        existing_budget = self.get_budget(session_id)
        # Prefer the provider-supplied context budget when available (set by
        # provider_context.get_context_budget() or llm_manager after first call).
        # Fall back to the current cached value so we never reset an explicitly
        # tuned value (e.g. a small model with a 4096-token window).
        context_budget = state.get("_context_budget")
        if context_budget and int(context_budget) > 0:
            max_tokens = int(context_budget)
        else:
            max_tokens = existing_budget.max_tokens

        self.update(
            session_id=session_id,
            used_tokens=total_raw,
            max_tokens=max_tokens,
            turn=state.get("rounds", 0),
        )

        logger.info(
            f"TokenBudget: raw={total_raw}/{max_tokens} "
            f"({total_raw / max_tokens:.1%}), "
            f"history={len(history)} msgs, "
            f"p2p={p2p_tokens} tokens"
        )

        budget = self.get_budget(session_id)
        if budget.should_compact:
            logger.warning(
                f"TokenBudget: {total_raw / max_tokens:.1%} raw usage, "
                "triggering pre-truncation compaction"
            )
            return "compact"

        return "ok"

    def check_and_prepare_compaction(self, session_id: str) -> bool:
        """Check if compaction should trigger."""
        budget = self.get_budget(session_id)

        if budget.should_compact:
            logger.info(
                f"TokenBudget: {budget.usage_ratio:.0%} usage, "
                f"triggering auto-compact at turn {budget.current_turn}"
            )
            budget.record_compaction()
            return True

        return False

    def get_status_message(self, session_id: str) -> str:
        """Get human-readable status."""
        budget = self.get_budget(session_id)

        if budget.should_compact:
            return f"Token budget at {budget.usage_ratio:.0%} - will compact"
        elif budget.should_warn:
            return f"Token budget at {budget.usage_ratio:.0%}"
        else:
            return f"Token budget: {budget.usage_ratio:.0%}"

    def record_usage(
        self,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Record LLM token usage for budget tracking."""
        budget = self.get_budget(session_id)
        budget.used_tokens = max(budget.used_tokens, total_tokens)
        budget.prompt_tokens = prompt_tokens
        budget.completion_tokens = completion_tokens
        logger.debug(
            f"TokenBudget: recorded usage for {session_id}: "
            f"prompt={prompt_tokens}, completion={completion_tokens}, total={total_tokens}"
        )

    def _estimate_tokens(self, message: dict) -> int:
        """Estimate tokens for a single message (~4 chars per token)."""
        content = message.get("content", "")
        role = message.get("role", "")
        return max(1, (len(role) + len(content)) // 4)


def get_token_budget_monitor() -> TokenBudgetMonitor:
    """Get the global token budget monitor instance."""
    return TokenBudgetMonitor.get_instance()
