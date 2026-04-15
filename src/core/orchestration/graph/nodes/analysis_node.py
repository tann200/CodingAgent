import logging
import os
import threading
from typing import Dict, Any

from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.tools.repo_summary import generate_repo_summary

logger = logging.getLogger(__name__)

# F8 / F15 fix: Cache indexed directories keyed by (resolved_path, mtime) so
# index_repository() is skipped on repeated calls to the same unchanged directory,
# but re-runs when the working dir changes or its mtime changes (stale cache fix).
# RA-3 fix: cap at 128 entries with simple LRU eviction so a long-running process
# working across many directories does not leak memory indefinitely.
# VOL7-7: Lock guards _INDEXED_DIRS mutations to prevent dict-size-during-iteration
# errors in concurrent asyncio tasks sharing the same event loop thread.
_INDEXED_DIRS_MAX = 128
_INDEXED_DIRS: dict = {}  # {resolved_path: mtime_ns}  (insertion-ordered via Python 3.7+)
_INDEXED_DIRS_LOCK = threading.Lock()

# PB-2: Cache generate_repo_summary() results keyed by resolved working_dir.
# Repo summaries are expensive (file-system walk + LLM) and rarely change during
# a session.  Keyed on the resolved path so symlinks don't cause cache misses.
_REPO_SUMMARY_CACHE: dict = {}  # {resolved_path: summary_result}
# MED-16 fix: lock guards _REPO_SUMMARY_CACHE mutations (concurrent tasks share
# the same event-loop thread but asyncio coroutines can interleave around
# awaits; module-level dicts are also accessed from worker threads via
# run_in_executor, so a plain threading.Lock is the right primitive).
_REPO_SUMMARY_CACHE_LOCK = threading.Lock()

# PB-3: SymbolGraph singleton per working_dir — avoids re-parsing the same files
# on every analysis_node invocation.
_SYMBOL_GRAPH_CACHE: dict = {}  # {resolved_path: SymbolGraph}
# MED-16 fix: same reasoning as _REPO_SUMMARY_CACHE_LOCK above.
_SYMBOL_GRAPH_CACHE_LOCK = threading.Lock()


def _extract_module_candidates(task: str) -> list:
    """RA-3: Extract likely module/file stems from a task description.

    Returns a short list of lowercase identifiers (snake_case or CamelCase stems)
    that are plausible module names.  Used by the lightweight test-map builder so
    that even fast-path (simple-task) runs can surface relevant test files.
    """
    import re as _re

    _SKIP = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "add",
        "fix",
        "use",
        "run",
        "get",
        "set",
        "new",
        "old",
        "all",
        "make",
        "update",
        "create",
        "delete",
        "remove",
        "implement",
        "change",
        "file",
        "function",
        "method",
        "class",
        "module",
        "code",
        "test",
        "please",
        "should",
        "must",
        "will",
        "when",
        "where",
        "how",
    }
    candidates = _re.findall(r"\b[A-Z][a-zA-Z0-9]{2,}\b|\b[a-z_][a-z0-9_]{2,}\b", task)
    # Convert CamelCase to snake_case stem for broader matching
    result = []
    for c in candidates:
        stem = c.lower()
        if stem not in _SKIP and len(stem) >= 3:
            result.append(stem)
    return result[:5]


def _build_lightweight_test_map(task: str, working_dir: str) -> dict:
    """RA-3: Build a test_map for simple/fast-path tasks using the cached SymbolGraph.

    Unlike the full Phase 2.4 enrichment, this function:
    - Does NOT index new files or start the heavy analysis pipeline
    - Queries only the module-level ``_SYMBOL_GRAPH_CACHE`` (already populated from a
      prior complex-task run, or returns an empty dict if the cache is cold)
    - Extracts module stems from the task description and looks up matching test files

    Returns a ``{module_stem: [test_path, ...]}`` dict (values are plain strings),
    or an empty dict when no cached graph is available or no tests are found.
    """
    try:
        _sg_key = str(os.path.realpath(working_dir))
        with _SYMBOL_GRAPH_CACHE_LOCK:
            sg = _SYMBOL_GRAPH_CACHE.get(_sg_key)
        if sg is None:
            return {}

        candidates = _extract_module_candidates(task)
        test_map: dict = {}
        for stem in candidates:
            raw = sg.find_tests_for_module(stem)
            if raw:
                # Normalise: find_tests_for_module returns List[Dict] with "file" key
                paths = []
                for item in raw[:3]:
                    if isinstance(item, dict):
                        p = item.get("file") or item.get("file_path")
                        if p:
                            paths.append(p)
                    elif isinstance(item, str):
                        paths.append(item)
                if paths:
                    test_map[stem] = paths
        return test_map
    except Exception as _e:
        logger.debug(
            "analysis_node: lightweight test_map build failed (non-critical): %s", _e
        )
        return {}


def _is_already_indexed(working_dir: str) -> bool:
    """Return True if working_dir was indexed and its mtime has not changed."""
    try:
        resolved = str(os.path.realpath(working_dir))
        with _INDEXED_DIRS_LOCK:
            mtime = os.stat(resolved).st_mtime_ns
            return _INDEXED_DIRS.get(resolved) == mtime
    except Exception:
        return False


def _mark_indexed(working_dir: str) -> None:
    """Record that working_dir has been indexed at its current mtime."""
    try:
        resolved = str(os.path.realpath(working_dir))
        with _INDEXED_DIRS_LOCK:
            mtime = os.stat(resolved).st_mtime_ns
            # Move-to-end (LRU update) if already present, else insert at end
            _INDEXED_DIRS.pop(resolved, None)
            _INDEXED_DIRS[resolved] = mtime
            # Evict oldest entry when over capacity
            while len(_INDEXED_DIRS) > _INDEXED_DIRS_MAX:
                _INDEXED_DIRS.pop(next(iter(_INDEXED_DIRS)))
    except Exception:
        pass


def clear_repo_summary_cache(working_dir: str | None = None) -> None:
    """PERF-1: Evict stale repo summary cache entries.

    Called at session start (and optionally on large file writes) to prevent
    the agent from planning with an outdated repository summary.

    Parameters
    ----------
    working_dir:
        If provided, evict only the entry for this directory.
        If ``None``, evict all cached entries (full reset).
    """
    with _REPO_SUMMARY_CACHE_LOCK:
        if working_dir is None:
            _REPO_SUMMARY_CACHE.clear()
            logger.debug("analysis_node: repo summary cache cleared (all entries)")
        else:
            resolved = str(os.path.realpath(working_dir))
            removed = _REPO_SUMMARY_CACHE.pop(resolved, None)
            if removed is not None:
                logger.debug(
                    "analysis_node: repo summary cache evicted for %s", resolved
                )


async def analysis_node(state: StateLike, config: Any) -> Dict[str, Any]:
    """
    Analysis Layer: Explores the repository to gather relevant context before planning.
    Uses repository intelligence tools to find relevant files, symbols, and dependencies.
    Automatically generates repo summary at the start and injects it into context.
    Uses the 'analyst' role for repository exploration.

    FAST-PATH: If perception already decided on an action AND the task is NOT complex,
    bypass heavy repository analysis.  C3 fix: complex tasks are always analysed even
    when next_action is set, so the builder's W3 routing is not nullified.
    """
    logger.info("=== analysis_node START ===")

    # HR-11 fix: Initialize analysis_failed flag
    analysis_failed = False

    # C3 fix: only bypass for genuinely simple tasks.  Import the same complexity
    # heuristic used by the builder so the two layers stay in sync.
    if state.get("next_action"):
        try:
            from src.core.orchestration.graph.builder import _task_is_complex

            is_complex = _task_is_complex(state)
        except Exception:
            is_complex = False

        if not is_complex:
            logger.info(
                "analysis_node: Fast path active (simple task, action already determined). "
                "Bypassing heavy analysis."
            )
            # WR-3 fix: explicitly clear call_graph to prevent stale data from
            # previous tasks being injected into new task's planning.
            # RA-3 fix: still build a lightweight test_map from the cached SymbolGraph
            # so planning_node can inject test-coverage hints even for simple tasks.
            _fp_task = state.get("task") or ""
            _fp_wd = state.get("working_dir", ".")
            _fp_test_map = _build_lightweight_test_map(_fp_task, _fp_wd)
            if _fp_test_map:
                logger.info(
                    "analysis_node: RA-3 lightweight test_map produced %d entries for fast-path task",
                    len(_fp_test_map),
                )
            return {
                "analysis_summary": "Skipped (Fast Path)",
                "relevant_files": [],
                "key_symbols": [],
                "repo_summary_data": "Skipped for efficiency",
                "call_graph": None,
                "test_map": _fp_test_map if _fp_test_map else None,
            }
        else:
            logger.info(
                "analysis_node: Complex task detected — running full analysis despite next_action "
                "(C3: W3 fast-path bypass suppressed for complex tasks)"
            )

    orchestrator = _resolve_orchestrator(state, config)
    if orchestrator is None:
        logger.error("analysis_node: orchestrator is None in config")
        return {
            "analysis_summary": "Orchestrator not found",
            "relevant_files": [],
            "key_symbols": [],
        }

    task = state.get("task") or ""
    working_dir = state.get("working_dir", ".")

    # Phase 1: Automatic Repo Summary - Execute at start before any LLM planning
    repo_summary_data = ""
    try:
        # PB-2: Check module-level cache before calling the expensive summary generator.
        _resolved_wd = str(os.path.realpath(working_dir))
        with _REPO_SUMMARY_CACHE_LOCK:
            summary_result = _REPO_SUMMARY_CACHE.get(_resolved_wd)
        if summary_result is not None:
            logger.debug("analysis_node: repo summary cache hit")
        else:
            summary_result = generate_repo_summary(working_dir)
            if summary_result:
                with _REPO_SUMMARY_CACHE_LOCK:
                    _REPO_SUMMARY_CACHE[_resolved_wd] = summary_result
        if summary_result.get("status") == "ok" or "summary" in summary_result:
            summary_text = summary_result.get("summary", "")
            framework = summary_result.get("framework", "Unknown")
            languages = summary_result.get("languages", [])
            test_framework = summary_result.get("test_framework", "None")
            entrypoints = summary_result.get("entrypoints", [])
            modules = summary_result.get("modules", [])

            repo_summary_data = f"""REPO SUMMARY:
- Framework: {framework}
- Languages: {", ".join(languages) if languages else "Unknown"}
- Test Framework: {test_framework}
- Entrypoints: {", ".join(entrypoints) if entrypoints else "None detected"}
- Modules: {", ".join(modules) if modules else "None detected"}
- Quick Summary: {summary_text}

Use this repository context to plan your deep-dive searches."""
            logger.info(
                f"analysis_node: generated repo summary - framework={framework}"
            )
    except Exception as e:
        logger.warning(f"analysis_node: repo summary generation failed: {e}")
        repo_summary_data = "Repo summary unavailable."

    relevant_files = []
    key_symbols = []
    analysis_summary = ""

    # Phase 1.5: Semantic Search via Vector Store
    # Use semantic search to find relevant symbols before keyword search
    semantic_results = []
    try:
        from src.core.indexing.vector_store import VectorStore
        from src.core.indexing.repo_indexer import index_repository

        # F8/F15: Only index when the directory has not been indexed yet or its mtime changed.
        if not _is_already_indexed(working_dir):
            index_repository(working_dir)
            _mark_indexed(working_dir)

        # Search the vector store for semantically similar symbols
        vs = VectorStore(working_dir)
        semantic_results = vs.search(task, limit=10)

        if semantic_results:
            logger.info(
                f"analysis_node: found {len(semantic_results)} semantically similar symbols"
            )

            # Add semantically relevant files to the search results
            for result in semantic_results:
                fp = result.get("file_path")
                if fp and fp not in relevant_files:
                    relevant_files.append(fp)
                sym = result.get("symbol_name")
                if sym and sym not in key_symbols:
                    key_symbols.append(sym)
    except ImportError:
        logger.debug(
            "analysis_node: vector_store not available, skipping semantic search"
        )
    except Exception as e:
        logger.warning(f"analysis_node: semantic search failed: {e}")

    def _call_tool_if_exists(tool_name, **kwargs):
        try:
            t = orchestrator.tool_registry.get(tool_name)
            if t and callable(t.get("fn")):
                return t["fn"](**kwargs)
        except Exception as e:
            logger.warning(f"analysis_node: tool {tool_name} failed: {e}")
        return None

    try:
        sc = _call_tool_if_exists("search_code", query=task, workdir=working_dir)
        if sc:
            results_data = sc.get("results") if isinstance(sc, dict) else sc
            if isinstance(results_data, list):
                for r in results_data[:5]:
                    fp = r.get("file_path") or r.get("file")
                    if fp and fp not in relevant_files:
                        relevant_files.append(fp)

        # F11: Extract identifiers (CamelCase / snake_case) from the task description
        # instead of blindly using the first word (which is usually a verb like "implement").
        import re as _re

        symbol_candidates = _re.findall(
            r"\b[A-Z][a-zA-Z0-9]{2,}\b|\b[a-z_][a-z0-9_]{2,}\b", task
        )
        # Filter out common English stopwords / verbs that are not identifiers
        _SKIP_WORDS = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "from",
            "into",
            "add",
            "fix",
            "use",
            "run",
            "get",
            "set",
            "new",
            "old",
            "all",
            "make",
            "update",
            "create",
            "delete",
            "remove",
            "implement",
            "change",
        }
        symbol_candidates = [
            s for s in symbol_candidates if s.lower() not in _SKIP_WORDS
        ]
        for candidate in symbol_candidates[:3]:
            fs = _call_tool_if_exists(
                "find_symbol", name=candidate, workdir=working_dir
            )
            if fs and isinstance(fs, dict):
                fp = fs.get("file_path")
                if fp and fp not in relevant_files:
                    relevant_files.append(fp)
                sym = fs.get("symbol_name")
                if sym and sym not in key_symbols:
                    key_symbols.append(sym)

        from pathlib import Path as _Path
        from src.core.indexing.symbol_graph import _SUPPORTED_SUFFIXES as _SG_SUFFIXES

        gl = _call_tool_if_exists("glob", pattern="**/*", workdir=working_dir)
        if gl and isinstance(gl, dict):
            items = gl.get("matches", [])
            for item in items[:40]:
                fp = item.get("name") if isinstance(item, dict) else item
                if fp and _Path(fp).suffix in _SG_SUFFIXES and fp not in relevant_files:
                    relevant_files.append(fp)

        if relevant_files:
            analysis_summary = (
                f"Found {len(relevant_files)} relevant files for task: {task[:50]}..."
            )
        else:
            analysis_summary = f"No specific files found. Task: {task[:50]}..."

        logger.info(
            f"analysis_node: found {len(relevant_files)} files, {len(key_symbols)} symbols"
        )
    except Exception as e:
        logger.error(f"analysis_node: analysis failed: {e}")
        analysis_summary = f"Analysis failed: {e}"
        # HR-11 fix: set analysis_failed flag so plan_validator can warn about
        # plans built without proper file context
        analysis_failed = True

    # Phase 2.4: Symbol graph enrichment — call graph context for planning
    symbol_context = ""
    call_graph_data: dict = {}
    test_map_data: dict = {}
    try:
        from src.core.indexing.symbol_graph import SymbolGraph
        from pathlib import Path

        # PB-3: Reuse a cached SymbolGraph for the same working_dir to avoid
        # re-parsing every file on every analysis_node call.
        _sg_key = str(os.path.realpath(working_dir))
        with _SYMBOL_GRAPH_CACHE_LOCK:
            sg = _SYMBOL_GRAPH_CACHE.get(_sg_key)
        if sg is None:
            sg = SymbolGraph(working_dir)
            with _SYMBOL_GRAPH_CACHE_LOCK:
                _SYMBOL_GRAPH_CACHE[_sg_key] = sg

        # Update index for all found relevant files (multi-lang: update_file handles suffix check)
        for fp in relevant_files[:25]:
            full_path = Path(working_dir) / fp
            if full_path.exists():
                sg.update_file(str(full_path))

        # P3-1: Collect call graph as structured JSON dict {symbol: [callers]}
        call_graph_data: dict = {}
        for sym in key_symbols[:5]:
            callers = sg.find_calls(sym)
            if callers:
                call_graph_data[sym] = callers[:5]

        # P3-1: Collect test map as structured JSON dict {module_stem: [test_paths]}
        # RA-3 fix: normalise find_tests_for_module output to plain string paths so
        # planning_node's ', '.join(unique_tests) doesn't fail on dict items.
        test_map_data: dict = {}
        for fp in relevant_files[:5]:
            module_name = Path(fp).stem
            tests = sg.find_tests_for_module(module_name)
            if tests:
                paths = []
                for item in tests[:3]:
                    if isinstance(item, dict):
                        p = item.get("file") or item.get("file_path")
                        if p:
                            paths.append(p)
                    elif isinstance(item, str):
                        paths.append(item)
                if paths:
                    test_map_data[module_name] = paths

        # Build prose summary for analysis_summary (backwards compat)
        if call_graph_data or test_map_data:
            symbol_context = "Symbol graph:\n"
            for sym, callers in call_graph_data.items():
                symbol_context += f"  '{sym}' called by: {', '.join(callers)}\n"
            for mod, tests in test_map_data.items():
                symbol_context += f"  Tests for {mod}: {', '.join(tests)}\n"
    except Exception as e:
        call_graph_data = {}
        test_map_data = {}
        logger.warning(f"analysis_node: symbol graph enrichment failed: {e}")

    # Phase 3: Cap relevant_files to a reasonable limit.
    # HR-1 fix: The ContextController used hardcoded line_count=50 and
    # estimated_tokens=200 for every file regardless of actual size, making its
    # budget enforcement meaningless (and worse than a simple slice since it
    # penalised files by insertion order, not relevance). Replace with a direct
    # cap so the most-relevant files (from semantic search, first in list) are
    # always kept.
    MAX_RELEVANT_FILES = 25
    if len(relevant_files) > MAX_RELEVANT_FILES:
        logger.info(
            f"analysis_node: capping relevant_files {len(relevant_files)} → {MAX_RELEVANT_FILES}"
        )
        relevant_files = relevant_files[:MAX_RELEVANT_FILES]

    return {
        "analysis_summary": analysis_summary
        + ("\n" + symbol_context if symbol_context else ""),
        "relevant_files": relevant_files,
        "key_symbols": key_symbols,
        "repo_summary_data": repo_summary_data,
        # P3-1: Structured JSON dependency data for planning_node
        "call_graph": call_graph_data if call_graph_data else None,
        "test_map": test_map_data if test_map_data else None,
        # HR-11 fix: indicate whether analysis succeeded or failed
        "analysis_failed": analysis_failed if "analysis_failed" in locals() else False,
    }
