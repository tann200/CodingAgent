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

import asyncio
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from src.tools._path_utils import changed_paths
from src.tools.constants import MODIFYING_TOOLS
from src.tools.tools_config import resolve_tool_alias

try:
    from src.core.indexing.symbol_graph import SymbolGraph
except Exception:  # pragma: no cover - optional dependency
    SymbolGraph = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# Workdirs already incrementally indexed in this process.  ``index_repository``
# is incremental, but skipping the call entirely for a seen workdir keeps loop
# entry cheap and avoids repeated filesystem walks.
_INDEXED_WORKDIRS: dict[str, Any] = {}
_INDEX_LOCK = threading.Lock()

_REPO_SUMMARY_CACHE: dict[str, Any] = {}
_REPO_SUMMARY_CACHE_LOCK = threading.Lock()
_INDEXED_FILE_REVISIONS: dict[str, Any] = {}

_MAX_REPO_CTX_FILES = 10
_MAX_REPO_CTX_SYMBOLS = 10
_MAX_REPO_CTX_CHARS = 4000


def _compute_workspace_revision(working_dir: str) -> int:
    """Return a hash of language file paths, mtimes, and sizes for freshness tracking."""
    try:
        from src.core.indexing.repo_indexer import walk_language_files

        files = walk_language_files(Path(working_dir))
        return hash(
            tuple(
                (rel, p.stat().st_mtime_ns, p.stat().st_size)
                for rel, p in files.items()
            )
        )
    except Exception:
        try:
            return os.stat(os.path.realpath(working_dir)).st_mtime_ns
        except Exception:
            return 0


def clear_repo_summary_cache(working_dir: str | None = None) -> None:
    """Evict stale repo summary cache entries.

    Called at session start (and on file writes) to prevent stale summaries.
    """
    with _REPO_SUMMARY_CACHE_LOCK:
        if working_dir is None:
            _REPO_SUMMARY_CACHE.clear()
            _INDEXED_FILE_REVISIONS.clear()
            logger.debug("repo_context: repo summary cache cleared (all entries)")
        else:
            resolved = str(os.path.realpath(working_dir))
            _INDEXED_FILE_REVISIONS.pop(resolved, None)
            removed = _REPO_SUMMARY_CACHE.pop(resolved, None)
            if removed is not None:
                logger.debug("repo_context: repo summary cache evicted for %s", resolved)


def _ensure_repo_index(working_dir: str) -> None:
    """Incrementally index *working_dir* once per process. Never raises."""
    if not working_dir:
        return
    try:
        resolved = str(Path(working_dir).resolve())
    except Exception:
        return

    rev = _compute_workspace_revision(resolved)

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
    """Return the on-disk paths a modifying tool may have touched."""
    return changed_paths(tool_name, tool_args)


def refresh_index_after_write(tool_name: str, tool_args: Any, working_dir: str) -> None:
    """Re-parse files into the on-disk index after a modifying tool runs."""
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


async def _retrieve_context(
    state: Mapping[str, Any],
    orchestrator: Any,
) -> list[dict[str, Any]]:
    """Retrieve repository context snippets for round-0 perception."""
    from src.core.orchestration.event_bus import run_with_correlation

    retrieved_snippets: list[dict[str, Any]] = []
    try:
        if (
            state.get("rounds", 0) == 0
            and orchestrator
            and hasattr(orchestrator, "tool_registry")
        ):
            raw_task = state.get("task") or ""
            symbol_regex = re.compile(
                r"`([^`]+)`"
                r'|"([A-Za-z_]\w*)"'
                r"|([A-Z][a-z]+(?:[A-Z][a-z]+)+)"
                r"|([a-z_][a-z0-9]*(?:_[a-z0-9]+){1,})"
            )
            extracted: list[str] = []
            for match in symbol_regex.finditer(raw_task):
                token = next(group for group in match.groups() if group)
                if token and token not in extracted:
                    extracted.append(token)
            query = extracted[0] if extracted else raw_task
            symbol_queries = extracted if extracted else [raw_task]

            loop = asyncio.get_running_loop()

            def _safe_call(tool_name: str, **kwargs: Any) -> Any:
                try:
                    tool = orchestrator.tool_registry.get(tool_name)
                    if tool and callable(tool.get("fn")):
                        return tool["fn"](**kwargs)
                except Exception:
                    pass
                return None

            workdir = state.get("working_dir")

            async def _fetch_search_code() -> dict[str, Any] | None:
                queries = symbol_queries[:3] if symbol_queries else [query]
                results = await asyncio.gather(
                    *[
                        run_with_correlation(
                            loop,
                            None,
                            lambda _q=_q: _safe_call(
                                "search_code", query=_q, workdir=workdir
                            ),
                        )
                        for _q in queries
                    ],
                    return_exceptions=True,
                )
                merged: list[Any] = []
                for result in results:
                    if result and not isinstance(result, Exception):
                        if isinstance(result, dict):
                            merged.extend(result.get("results", []))
                        elif isinstance(result, list):
                            merged.extend(result)
                return {"results": merged} if merged else None

            async def _fetch_symbols() -> list[Any]:
                results = []
                for symbol_query in symbol_queries[:3]:
                    result = await run_with_correlation(
                        loop,
                        None,
                        lambda sq=symbol_query: _safe_call(
                            "find_symbol", name=sq, workdir=workdir
                        ),
                    )
                    results.append(result)
                return results

            async def _fetch_references() -> Any:
                return await run_with_correlation(
                    loop,
                    None,
                    lambda: _safe_call("find_references", name=query, workdir=workdir),
                )

            async def _fetch_test_files() -> list[Any]:
                results: list[Any] = []
                try:
                    if SymbolGraph is None:
                        return results
                    symbol_graph = SymbolGraph(workdir)
                    for symbol_query in symbol_queries[:2]:
                        tests = await run_with_correlation(
                            loop,
                            None,
                            lambda sq=symbol_query: symbol_graph.find_tests_for_module(sq),
                        )
                        if tests and isinstance(tests, list):
                            results.extend(tests[:2])
                except Exception:
                    pass
                return results

            (
                search_code_result,
                symbol_results,
                references_result,
                test_file_results,
            ) = await asyncio.gather(
                _fetch_search_code(),
                _fetch_symbols(),
                _fetch_references(),
                _fetch_test_files(),
                return_exceptions=True,
            )

            if search_code_result and not isinstance(search_code_result, Exception):
                raw_list = (
                    search_code_result.get("results")
                    if isinstance(search_code_result, dict)
                    else None
                ) or (search_code_result if isinstance(search_code_result, list) else [])
                for result in raw_list:
                    if isinstance(result, dict):
                        retrieved_snippets.append(
                            {
                                "file_path": result.get("file_path") or result.get("file"),
                                "snippet": result.get("snippet")
                                or result.get("text")
                                or result.get("content"),
                                "reason": "search_code",
                            }
                        )

            if (
                symbol_results
                and not isinstance(symbol_results, Exception)
                and isinstance(symbol_results, list)
            ):
                for found_symbol in symbol_results:
                    if (
                        found_symbol
                        and isinstance(found_symbol, dict)
                        and found_symbol.get("file_path")
                    ):
                        retrieved_snippets.append(
                            {
                                "file_path": found_symbol.get("file_path"),
                                "snippet": found_symbol.get("snippet"),
                                "reason": "find_symbol",
                            }
                        )

            if (
                references_result
                and not isinstance(references_result, Exception)
                and isinstance(references_result, list)
            ):
                for reference in references_result:
                    if isinstance(reference, dict):
                        retrieved_snippets.append(
                            {
                                "file_path": reference.get("file_path"),
                                "snippet": reference.get("excerpt")
                                or reference.get("context"),
                                "reason": "find_references",
                            }
                        )

            if (
                test_file_results
                and not isinstance(test_file_results, Exception)
                and isinstance(test_file_results, list)
            ):
                for test_path in test_file_results[:3]:
                    if isinstance(test_path, str) and test_path:
                        retrieved_snippets.append(
                            {
                                "file_path": test_path,
                                "snippet": None,
                                "reason": "find_tests_for_module",
                            }
                        )
    except Exception as retrieval_error:
        logger.debug(
            "repo_context: context retrieval failed (non-fatal, continuing with empty snippets): %s",
            retrieval_error,
        )
        retrieved_snippets = []

    return retrieved_snippets


def reset_index_memo() -> None:
    """Clear the per-process index memo (used by tests)."""
    with _INDEX_LOCK:
        _INDEXED_WORKDIRS.clear()
    clear_repo_summary_cache(None)
