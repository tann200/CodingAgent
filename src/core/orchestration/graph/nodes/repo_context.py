"""repo_context.py — repository-awareness helpers for the production tool loop.

P1.2 (audit CRITICAL-2): the repo index in ``src/core/indexing/`` was reachable
only from ``planning_node`` and ``execution_node``, neither of which exists in
the frontier or lite production graph.  A simple task on a capable model
therefore ran ``perception → frontier_loop → memory_sync`` with no repository
intelligence at all: no symbol lookup before acting, and no index refresh after
editing.  These helpers give the production loop the same grounding the legacy
nodes had, reusing the shared hydration helper rather than reimplementing
symbol lookup.

Every function here is best-effort and non-fatal by contract: an unindexable
workspace, a missing optional dependency, or a malformed state value must
degrade to "no repo context", never abort the loop.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Mapping

from src.tools._path_utils import changed_paths
from src.tools.constants import MODIFYING_TOOLS
from src.tools.tools_config import resolve_tool_alias

logger = logging.getLogger(__name__)

# Workdirs already incrementally indexed in this process.  ``index_repository``
# is incremental, but skipping the call entirely for a seen workdir keeps loop
# entry cheap and avoids repeated filesystem walks.
_INDEXED_WORKDIRS: dict[str, Any] = {}
_INDEX_LOCK = threading.Lock()

_MAX_REPO_CTX_FILES = 10
_MAX_REPO_CTX_SYMBOLS = 10
_MAX_REPO_CTX_CHARS = 4000


def _ensure_repo_index(working_dir: str) -> None:
    """Incrementally index *working_dir* once per process. Never raises."""
    if not working_dir:
        return
    try:
        resolved = str(Path(working_dir).resolve())
    except Exception:
        return

    try:
        from src.core.orchestration.graph.nodes.analysis_node import (
            _compute_workspace_revision,
        )

        rev = _compute_workspace_revision(resolved)
    except Exception:
        rev = None

    with _INDEX_LOCK:
        if rev is not None:
            if _INDEXED_WORKDIRS.get(resolved) == rev:
                return
        elif resolved in _INDEXED_WORKDIRS:
            return
        # Recorded before the call so a failure cannot cause a re-walk on every
        # loop entry.
        _INDEXED_WORKDIRS[resolved] = rev
    try:
        from src.core.indexing.repo_indexer import index_repository

        index_repository(resolved, incremental=True)
        logger.info("repo_context: repository indexed (%s)", resolved)
    except Exception as exc:
        logger.debug("repo_context: repo indexing skipped (non-critical): %s", exc)


def build_repo_context_block(state: Mapping[str, Any], task: str) -> str:
    """Return a compact, bounded repo-context block for *task*, or ``""``.

    Args:
        state: Graph state; ``working_dir`` selects the repository.
        task:  Task text used to match relevant symbols.

    Fails closed on a missing ``working_dir``: falling back to ``"."`` would
    index and query whatever directory the process happens to be running in and
    inject unrelated symbols into the prompt.
    """
    if not task:
        return ""
    working_dir = str(state.get("working_dir") or "").strip()
    if not working_dir:
        return ""
    _ensure_repo_index(working_dir)
    try:
        from src.core.indexing.repo_indexer import get_symbols_for_task
        from src.core.orchestration.graph.nodes.planning_helpers import (
            hydrate_repo_context_from_index,
        )

        files, symbols, _hits = hydrate_repo_context_from_index(
            working_dir=working_dir,
            task=task,
            relevant_files=[],
            key_symbols=[],
            get_symbols_for_task_fn=get_symbols_for_task,
            logger=logger,
            max_files=_MAX_REPO_CTX_FILES,
            max_symbols=_MAX_REPO_CTX_SYMBOLS,
        )
    except Exception as exc:
        logger.debug("repo_context: hydration skipped (non-critical): %s", exc)
        return ""

    lines: list[str] = []
    if symbols:
        lines.append("Relevant symbols: " + ", ".join(symbols))
    if files:
        lines.append("Relevant files: " + ", ".join(files))
    if not lines:
        return ""
    block = "<repo_context>\n" + "\n".join(lines) + "\n</repo_context>"
    if len(block) > _MAX_REPO_CTX_CHARS:
        block = block[:_MAX_REPO_CTX_CHARS] + "\n</repo_context>"
    return block


def _candidate_paths(tool_name: str, tool_args: Mapping[str, Any]) -> list[str]:
    """Return the on-disk paths a modifying tool may have touched.

    Delegates to ``_path_utils.changed_paths``, the single source of truth
    shared with the P3.2 change-log journal.  Two copies of this key list
    would let the index and the audit trail disagree about a rename (the
    index would keep a stale entry for the old filename).
    """
    return changed_paths(tool_name, tool_args)


def refresh_index_after_write(tool_name: str, tool_args: Any, working_dir: str) -> None:
    """Re-parse files into the on-disk index after a modifying tool runs.

    Mirrors ``execution_node``'s P3-T3 behavior, which is legacy-only.  Best
    effort and non-fatal by contract.

    Coverage strategy:
      * Modifying tool with a resolvable path -> cheap single-file reparse
        (``refresh_file_in_index``), which also drops symbols for deleted files.
      * Modifying tool with no single path (a multi-file patch) -> fall back to
        a full incremental re-walk so nothing is left stale.

    Runs regardless of whether the tool reported success: the file is re-parsed
    as it now exists, so a failed write is simply a no-op.
    """
    try:
        if not tool_args or not working_dir:
            return
        args = tool_args if isinstance(tool_args, dict) else {}
        canonical = resolve_tool_alias(str(tool_name))
        if canonical not in MODIFYING_TOOLS and str(tool_name) not in MODIFYING_TOOLS:
            return

        from src.core.indexing.repo_indexer import refresh_file_in_index

        paths = _candidate_paths(canonical, args)
        if not paths:
            from src.core.indexing.repo_indexer import index_repository

            index_repository(working_dir, incremental=True)
            logger.debug("repo_context: full incremental re-index after %s", tool_name)
            return
        for path in paths:
            refresh_file_in_index(path, working_dir)
        logger.debug("repo_context: refreshed %d file(s) after %s", len(paths), tool_name)
    except Exception as exc:
        logger.debug("repo_context: index refresh skipped (non-critical): %s", exc)



def reset_index_memo() -> None:
    """Clear the per-process index memo (used by tests)."""
    with _INDEX_LOCK:
        _INDEXED_WORKDIRS.clear()
