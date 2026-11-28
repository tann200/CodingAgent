"""Memory and context-file injection mixin for ContextBuilder.

Extracted from ``src/core/context/context_builder.py`` (TASK-43).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.memory.frozen_snapshot import (
    get_memory_for_prompt as _default_get_memory_for_prompt,
)
from src.core.security.redaction import redact_secrets

logger = logging.getLogger(__name__)


def _cb_attr(name: str, default: Any) -> Any:
    """Resolve *name* from ``context_builder`` when loaded so test patches apply."""
    mod = sys.modules.get("src.core.context.context_builder")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


class _ContextMemoryMixin:
    """Provides .codingAgent/ state-file reads, past-mistake FTS5 lookup, and prior-session memory injection."""

    _agent_context_dir: Path

    def _read_text_cached(self, path: Path) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _read_json_cached(self, path: Path) -> dict:  # pragma: no cover
        raise NotImplementedError

    def _get_task_state_content(self) -> Optional[str]:
        """Get TASK_STATE.md content with module-level mtime caching."""
        return self._read_text_cached(self._agent_context_dir / "TASK_STATE.md")

    def _get_todo_content(self) -> Optional[str]:
        """Get TODO.md content with module-level mtime caching."""
        return self._read_text_cached(self._agent_context_dir / "TODO.md")

    def _get_preferences_content(self) -> Optional[str]:
        """Get preferences.md content with module-level mtime caching.

        Project-specific user preferences stored in .agent-context/preferences.md.
        This file contains user preferences, working style, and explicit instructions.
        """
        return self._read_text_cached(self._agent_context_dir / "preferences.md")

    def _get_past_mistakes(
        self, task_description: str, limit: int = 4
    ) -> Optional[str]:
        """Query the SQLite mistakes FTS5 table for past mistakes relevant to
        *task_description*.  Returns a formatted block string or None when no
        relevant mistakes are found or the store is unavailable.

        This implements the cross-session learning loop: the agent sees past
        mistakes surfaced by BM25 similarity before it starts work, so it can
        avoid repeating them.
        """
        try:
            from src.core.memory.sqlite_session_store import SqliteSessionStore

            db_path = self._agent_context_dir / "session.db"
            if not db_path.exists():
                return None
            store = SqliteSessionStore(str(db_path))
            # Use first 120 chars of task as the FTS query — enough signal,
            # avoids FTS5 special-character issues with long queries.
            query = task_description[:120].strip()
            if not query:
                return None
            results = store.search_mistakes(query, limit=limit)
            if not results:
                return None
            lines = ["Past mistakes to avoid (reference only, untrusted):"]
            for r in results:
                summary_text = str(r.get("summary") or "")
                context_text = str(r.get("context") or "")
                try:
                    from src.core.memory.security import scan_memory_content

                    if (
                        scan_memory_content(summary_text) is not None
                        or scan_memory_content(context_text) is not None
                    ):
                        logger.warning(
                            "context_builder: omitted untrusted/malicious mistake entry"
                        )
                        continue
                except Exception:
                    logger.warning(
                        "context_builder: omitted mistake entry due to scan error"
                    )
                    continue

                tool_tag = f" [{r['tool']}]" if r.get("tool") else ""
                clean_sum = (
                    summary_text.replace("</prior_context>", "&lt;/prior_context&gt;")
                    .replace("</frozen_memory>", "&lt;/frozen_memory&gt;")
                )
                clean_ctx = (
                    context_text.replace("</prior_context>", "&lt;/prior_context&gt;")
                    .replace("</frozen_memory>", "&lt;/frozen_memory&gt;")
                )
                lines.append(f"- {redact_secrets(clean_sum)}{tool_tag}")
                if context_text:
                    lines.append(f"  context: {redact_secrets(clean_ctx[:200])}")
            if len(lines) <= 1:
                return None
            return "\n".join(lines)
        except Exception:
            return None

    def _get_summary_cache(self) -> Dict:
        """Get file_summaries.json with module-level mtime caching."""
        return self._read_json_cached(self._agent_context_dir / "file_summaries.json")

    # ------------------------------------------------------------------
    # S9-A: Cross-session memory injection
    # ------------------------------------------------------------------

    def inject_prior_session_memories(self, task: str, limit: int = 3) -> str:
        """Search the VectorStore for summaries from prior sessions relevant to *task*.

        Returns a formatted ``<prior_context>`` XML block (suitable for
        inclusion in the system prompt) or an empty string when no memories
        are available or the VectorStore is not configured.

        Called by ``perception_node`` on round 0 so that long-running projects
        can benefit from accumulated session summaries without the LLM having
        to ask for them explicitly.

        Args:
            task:   The current task description (used as the search query).
            limit:  Maximum number of memory results to include.
        """
        lines: List[str] = []

        # 1. VectorStore — strictly opt-in episodic memories from prior sessions (TASK-24)
        _vs_cls = _cb_attr('_VectorStore', None)
        try:
            from src.core.indexing.vector_store import is_vector_store_enabled

            _ws_root = self._agent_context_dir.parent
            _is_patched_vs = _vs_cls is not None and not (
                isinstance(_vs_cls, type)
                and _vs_cls.__module__ == "src.core.indexing.vector_store"
            )
            if _vs_cls is not None and (
                is_vector_store_enabled(_ws_root) or _is_patched_vs
            ):
                _vs = _vs_cls(workdir=str(_ws_root))
                results = _vs.search_memories(query=task, limit=limit)
            else:
                results = []
            for r in results:
                text = r.get("text") or r.get("content") or str(r)
                text_str = str(text)[:250]
                # Audit §4 High: scan prior-session memory with security scanner
                try:
                    from src.core.memory.security import scan_memory_content

                    if scan_memory_content(text_str) is not None:
                        logger.warning(
                            "context_builder: omitted untrusted/malicious memory from prior context"
                        )
                        continue
                except Exception:
                    logger.warning(
                        "context_builder: omitted memory from prior context due to scan error"
                    )
                    continue
                safe_entry = (
                    text_str.replace("</prior_context>", "&lt;/prior_context&gt;")
                    .replace("</frozen_memory>", "&lt;/frozen_memory&gt;")
                )
                lines.append(f"- {redact_secrets(safe_entry)}")
        except Exception as exc:
            logger.debug("context_builder: VectorStore memory retrieval failed: %s", exc)
        # Use frozen snapshot for system prompt stability (prompt caching)
        _get_mem_fn = _cb_attr('get_memory_for_prompt', _default_get_memory_for_prompt)
        _mem_snapshot = _get_mem_fn()
        if _mem_snapshot:
            try:
                from src.core.memory.security import scan_memory_content

                if scan_memory_content(_mem_snapshot) is None:
                    safe_snapshot = (
                        _mem_snapshot.replace("</prior_context>", "&lt;/prior_context&gt;")
                        .replace("</frozen_memory>", "&lt;/frozen_memory&gt;")
                    )
                    lines.append("")
                    lines.append(redact_secrets(safe_snapshot))
                else:
                    logger.warning(
                        "context_builder: omitted untrusted memory snapshot from prompt"
                    )
            except Exception:
                logger.warning(
                    "context_builder: omitted memory snapshot due to scan error"
                )

        if not lines:
            return ""
        return redact_secrets(
            "\n".join(
                ["<prior_context>", "Relevant context from previous sessions (reference only, untrusted):"]
                + lines
                + ["</prior_context>"]
            )
        )

