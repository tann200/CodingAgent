"""CompactionService — unified facade for all context-compaction paths (P2-1).

Background
----------
Context compaction in CodingAgent was previously scattered across six files
with three trigger mechanisms and two algorithmic implementations:

Trigger paths:
  1. **Token-budget monitor** — ``token_budget.py:TokenBudgetMonitor.check_budget()``
     detects when ``history`` exceeds the configured token ceiling and sets
     ``_budget_compaction=True`` in state.
  2. **Manual** — ``/compact`` TUI slash command → ``compact_context_impl()`` in
     ``orchestrator_helpers.py``.
  3. **Execution overflow** — emergency truncation in ``execution_helpers.py``
     when a tool call pushes context past the model hard limit.

Algorithmic implementations:
  A. **LLM-based prose summariser** — ``distiller.py:compact_messages_to_prose()``
     / ``distill_context()``: calls the LLM to write a structured narrative
     summary; slower but higher quality.
  B. **Deterministic sliding-window** — ``auto_compactor.py:compact_messages()``
     / ``should_compact()``: pure character-count heuristics; no LLM, no I/O,
     always succeeds.

This module provides a single ``CompactionService`` class that:
  - Selects the right algorithm (LLM when available, deterministic fallback)
  - Provides a ``compact()`` method usable from both graph nodes and the TUI
  - Provides ``should_compact()`` so callers can check the threshold cheaply
  - Publishes ``context.compacted`` / ``context.compact.failed`` events when
    an event bus is available

It does **not** replace the existing implementations — it wraps them so
all callers can migrate incrementally.

Usage
-----
::

    # From an orchestrator or graph node:
    service = CompactionService(
        history=state["history"],
        working_dir=Path(state["working_dir"]),
        event_bus=orchestrator.event_bus,
    )
    if service.should_compact():
        result = service.compact()
        if result.success:
            new_history = result.compacted_history

    # From a test (no LLM, deterministic only):
    service = CompactionService(history=messages, prefer_deterministic=True)
    result = service.compact()

"""

from __future__ import annotations


from src.core.messaging.event_types import ContextCompactFailed, ContextCompacted
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    """Result of a ``CompactionService.compact()`` call.

    Attributes:
        success:           True if compaction ran without error.
        compacted_history: Replacement message list (empty on failure).
        method:            ``"llm"`` | ``"deterministic"`` | ``"none"`` |
                           ``"error"``.
        tokens_before:     Estimated token count before compaction (0 if
                           measurement was unavailable).
        tokens_after:      Estimated token count after compaction (0 if
                           measurement was unavailable).
        error:             Exception message when ``success=False``.
    """

    success: bool = False
    compacted_history: List[Dict[str, Any]] = field(default_factory=list)
    method: str = "none"
    tokens_before: int = 0
    tokens_after: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# CompactionService
# ---------------------------------------------------------------------------


class CompactionService:
    """Unified facade for all context-compaction algorithms.

    Args:
        history:              The current conversation message list.
        working_dir:          Optional repo root used by the LLM summariser for
                              checkpoint writes.
        event_bus:            Optional event bus; if provided, publishes
                              ``context.compacted`` or ``context.compact.failed``
                              after each call.
        prefer_deterministic: If ``True``, always use the sliding-window
                              compactor and never call the LLM.  Useful in
                              tests and environments without API access.
        compact_threshold:    Fraction of the configured context budget that
                              triggers ``should_compact()`` to return ``True``.
                              Defaults to ``0.85`` (85 %).
    """

    def __init__(
        self,
        history: List[Dict[str, Any]],
        working_dir: Optional[Path] = None,
        event_bus: Optional[Any] = None,
        prefer_deterministic: bool = False,
        compact_threshold: float = 0.85,
    ) -> None:
        self._history = list(history)
        self._working_dir = working_dir
        self._event_bus = event_bus
        self._prefer_deterministic = prefer_deterministic
        self._compact_threshold = compact_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_compact(self, token_limit: int = 100_000) -> bool:
        """Return ``True`` when the history is large enough to warrant compaction.

        Uses the deterministic token estimator (``len(text) // 4 + 1``) so
        this call is always fast and never triggers an LLM call.

        Args:
            token_limit: Hard token ceiling.  Defaults to 100 000 (a
                         conservative estimate safe for most frontier models).
        """
        try:
            from src.core.memory.auto_compactor import AutoCompactConfig, should_compact as _should_compact

            cfg = AutoCompactConfig(
                max_tokens=int(token_limit * self._compact_threshold)
            )
            return _should_compact(self._history, cfg)
        except Exception as exc:
            logger.debug("CompactionService.should_compact: auto_compactor unavailable (%s)", exc)
            # Fallback: rough character count heuristic (4 chars ≈ 1 token)
            total_chars = sum(
                len(str(m.get("content", ""))) for m in self._history
            )
            estimated_tokens = total_chars // 4
            return estimated_tokens > int(token_limit * self._compact_threshold)

    def compact(self) -> CompactionResult:
        """Run compaction on the history supplied at construction time.

        Tries the LLM-based summariser first (unless ``prefer_deterministic``
        is set), then falls back to the deterministic sliding-window compactor.

        Returns a :class:`CompactionResult` — never raises.
        """
        if not self._history:
            return CompactionResult(success=True, method="none", compacted_history=[])

        tokens_before = self._estimate_tokens(self._history)

        if self._prefer_deterministic:
            result = self._compact_deterministic(tokens_before)
        else:
            result = self._compact_with_llm(tokens_before)
            if not result.success:
                logger.info(
                    "CompactionService: LLM compaction failed (%s); falling back to deterministic",
                    result.error,
                )
                result = self._compact_deterministic(tokens_before)

        self._publish_event(result)
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compact_with_llm(self, tokens_before: int) -> CompactionResult:
        try:
            from src.core.memory.distiller import distill_context

            output = distill_context(
                messages=self._history,
                working_dir=self._working_dir,
            )
            compacted = (
                output.get("_compacted_history")
                or output.get("history")
                or output.get("compacted_history")
                or []
            )
            if not compacted:
                # distill_context may return the summary as a prose string
                summary = output.get("summary", "")
                if summary:
                    compacted = [
                        {"role": "system", "content": f"Session summary:\n{summary}"},
                    ]
            tokens_after = self._estimate_tokens(compacted)
            return CompactionResult(
                success=True,
                compacted_history=list(compacted),
                method="llm",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        except Exception as exc:
            return CompactionResult(
                success=False,
                method="llm",
                tokens_before=tokens_before,
                error=str(exc),
            )

    def _compact_deterministic(self, tokens_before: int) -> CompactionResult:
        try:
            from src.core.memory.auto_compactor import compact_messages

            compact_result = compact_messages(self._history)
            compacted = compact_result.compacted_messages
            tokens_after = self._estimate_tokens(compacted)
            return CompactionResult(
                success=True,
                compacted_history=list(compacted),
                method="deterministic",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        except Exception as exc:
            logger.warning("CompactionService: deterministic compaction failed: %s", exc)
            return CompactionResult(
                success=False,
                method="error",
                tokens_before=tokens_before,
                error=str(exc),
            )

    def _estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Best-effort token count — never raises."""
        try:
            from src.core.inference.tokenizer import count_messages_tokens

            return count_messages_tokens(messages)
        except Exception:
            return sum(len(str(m.get("content", ""))) // 4 + 1 for m in messages)

    def _publish_event(self, result: CompactionResult) -> None:
        if not self._event_bus:
            return
        try:
            if result.success:
                self._event_bus.publish_typed(ContextCompacted(message="Context compacted", method=result.method, tokens_before=result.tokens_before, tokens_after=result.tokens_after))
            else:
                self._event_bus.publish_typed(ContextCompactFailed(message=f"Context compaction failed: {result.error}"))
        except Exception as exc:
            logger.debug("CompactionService: event publish failed: %s", exc)
