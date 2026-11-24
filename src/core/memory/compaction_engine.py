"""compaction_engine.py — Unified on-demand TurnGroup-atomic CompactionEngine.

TASK-24: Consolidates context-compaction decisions and TurnGroup-safe history
compaction into a single on-demand engine (<= 220 LOC) invoked strictly when
estimated tokens cross the context threshold or on explicit ``/compact``.
Never spawns background LLM calls on short tasks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.messaging.event_types import ContextCompactFailed, ContextCompacted

logger = logging.getLogger(__name__)


@dataclass
class CompactionResult:
    """Result of an on-demand ``CompactionEngine.compact()`` invocation."""

    success: bool = False
    compacted_history: List[Dict[str, Any]] = field(default_factory=list)
    method: str = "none"
    tokens_before: int = 0
    tokens_after: int = 0
    error: str = ""


def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    try:
        from src.core.context.token_estimator import estimate_messages_tokens

        return int(estimate_messages_tokens(messages))
    except Exception:
        total_chars = sum(len(str(m.get("content") or "")) for m in messages)
        return max(0, total_chars // 4)


class CompactionEngine:
    """Single on-demand context compaction engine (deterministic + optional LLM)."""

    def __init__(
        self,
        history: List[Dict[str, Any]],
        working_dir: Optional[Path] = None,
        event_bus: Optional[Any] = None,
        prefer_deterministic: bool = False,
        compact_threshold: float = 0.85,
        keep_recent_groups: int = 2,
    ) -> None:
        self._history = list(history)
        self._working_dir = Path(working_dir) if working_dir is not None else None
        self._event_bus = event_bus
        self._prefer_deterministic = prefer_deterministic
        self._compact_threshold = compact_threshold
        self._keep_recent_groups = max(1, keep_recent_groups)

    def _estimate_tokens(
        self, messages: Optional[List[Dict[str, Any]]] = None
    ) -> int:
        msgs = self._history if messages is None else messages
        return _estimate_tokens(msgs)

    def should_compact(
        self,
        token_limit: Optional[int] = None,
        *,
        token_budget: Optional[int] = None,
    ) -> bool:
        """Return True iff *history* exceeds the configured context budget threshold."""
        if not self._history:
            return False
        limit = token_limit if token_limit is not None else token_budget
        try:
            tokens = self._estimate_tokens(self._history)
            if limit and limit > 0:
                return tokens >= int(limit * self._compact_threshold)
            from src.core.memory.auto_compactor import (
                AutoCompactConfig,
                should_compact as _ac_should,
            )

            return bool(_ac_should(self._history, AutoCompactConfig()))
        except Exception:
            try:
                total_chars = sum(len(str(m.get("content") or "")) for m in self._history)
                est = max(0, total_chars // 4)
                if limit and limit > 0:
                    return est >= int(limit * self._compact_threshold)
                return est >= 10_000
            except Exception:
                return False

    def compact(self) -> CompactionResult:
        """Compact history on demand, preserving TurnGroup atomicity."""
        if not self._history:
            return CompactionResult(success=True, compacted_history=[], method="none")

        tokens_before = self._estimate_tokens(self._history)

        if not self._prefer_deterministic:
            llm_res = self._try_llm_compaction(tokens_before)
            if llm_res is not None:
                self._publish_result(llm_res)
                return llm_res

        det_res = self._run_deterministic_compaction(tokens_before)
        self._publish_result(det_res)
        return det_res

    def _try_llm_compaction(self, tokens_before: int) -> Optional[CompactionResult]:
        try:
            from src.core.memory.distiller import distill_context

            output = distill_context(
                self._history,
                working_dir=self._working_dir,
            )
            if isinstance(output, dict):
                raw_hist = output.get("_compacted_history") or output.get("history")
                if isinstance(raw_hist, list) and raw_hist:
                    compacted = list(raw_hist)
                    return CompactionResult(
                        success=True,
                        compacted_history=compacted,
                        method="llm",
                        tokens_before=tokens_before,
                        tokens_after=self._estimate_tokens(compacted),
                    )
                summary_text = output.get("summary")
                if isinstance(summary_text, str) and summary_text.strip():
                    compacted = self._wrap_prose_summary(summary_text.strip())
                    return CompactionResult(
                        success=True,
                        compacted_history=compacted,
                        method="llm",
                        tokens_before=tokens_before,
                        tokens_after=self._estimate_tokens(compacted),
                    )
            if isinstance(output, str) and output.strip():
                compacted = self._wrap_prose_summary(output.strip())
                return CompactionResult(
                    success=True,
                    compacted_history=compacted,
                    method="llm",
                    tokens_before=tokens_before,
                    tokens_after=self._estimate_tokens(compacted),
                )
        except Exception as exc:
            logger.debug("CompactionEngine: LLM compaction unavailable (%s); falling back", exc)
        return None

    def _wrap_prose_summary(self, prose: str) -> List[Dict[str, Any]]:
        try:
            from src.core.orchestration.graph.nodes.compact_node import group_atomic_turns

            groups = group_atomic_turns(self._history)
            tail = [m for g in groups[-self._keep_recent_groups :] for m in g]
        except Exception:
            tail = self._history[-4:]
        return [
            {
                "role": "system",
                "content": f"[COMPACTED] <summary>\n{prose}\n</summary>",
            }
        ] + tail

    def _run_deterministic_compaction(self, tokens_before: int) -> CompactionResult:
        try:
            from src.core.memory.auto_compactor import compact_messages

            compact_res = compact_messages(self._history)
            compacted: List[Dict[str, Any]] = list(compact_res.compacted_messages)
            return CompactionResult(
                success=True,
                compacted_history=compacted,
                method="deterministic",
                tokens_before=tokens_before,
                tokens_after=_estimate_tokens(compacted),
            )
        except Exception as exc:
            logger.warning("CompactionEngine: deterministic compaction failed: %s", exc)
            return CompactionResult(
                success=False,
                compacted_history=[],
                method="error",
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error=str(exc),
            )

    def _publish_result(self, res: CompactionResult) -> None:
        if self._event_bus is None:
            return
        try:
            if res.success:
                self._event_bus.publish_typed(
                    ContextCompacted(
                        message=f"Compacted context via {res.method}",
                        method=res.method,
                        tokens_before=res.tokens_before,
                        tokens_after=res.tokens_after,
                    )
                )
            else:
                self._event_bus.publish_typed(
                    ContextCompactFailed(message=res.error or "Compaction failed")
                )
        except Exception:
            pass


__all__ = ["CompactionEngine", "CompactionResult"]
