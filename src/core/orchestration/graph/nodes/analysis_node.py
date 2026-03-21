import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.tools.repo_summary import generate_repo_summary

logger = logging.getLogger(__name__)

# F8: Cache of already-indexed directories so index_repository() is called at most
# once per working directory per process lifetime (it is expensive: file walk + embeddings).
_INDEXED_DIRS: set = set()


async def analysis_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Analysis Layer: Explores the repository to gather relevant context before planning.
    Uses repository intelligence tools to find relevant files, symbols, and dependencies.
    Automatically generates repo summary at the start and injects it into context.
    Uses the 'analyst' role for repository exploration.

    FAST-PATH: If perception already decided on an action (simple 1-step task),
    bypass heavy repository analysis to save tokens and time.
    """
    logger.info("=== analysis_node START ===")

    # FAST-PATH BYPASS: If perception already decided on an action (e.g. simple 1-step task),
    # bypass the heavy and expensive repository analysis.
    if state.get("next_action"):
        logger.info(
            "analysis_node: Fast path active (Action already determined). Bypassing heavy analysis."
        )
        return {
            "analysis_summary": "Skipped (Fast Path)",
            "relevant_files": [],
            "key_symbols": [],
            "repo_summary_data": "Skipped for efficiency",
        }


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
        summary_result = generate_repo_summary(working_dir)
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

        # F8: Only index once per working_dir per process — indexing is expensive.
        if working_dir not in _INDEXED_DIRS:
            index_repository(working_dir)
            _INDEXED_DIRS.add(working_dir)

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
            r'\b[A-Z][a-zA-Z0-9]{2,}\b|\b[a-z_][a-z0-9_]{2,}\b', task
        )
        # Filter out common English stopwords / verbs that are not identifiers
        _SKIP_WORDS = {
            "the", "and", "for", "with", "that", "this", "from", "into", "add",
            "fix", "use", "run", "get", "set", "new", "old", "all", "make",
            "update", "create", "delete", "remove", "implement", "change",
        }
        symbol_candidates = [s for s in symbol_candidates if s.lower() not in _SKIP_WORDS]
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

    # Phase 2.4: Symbol graph enrichment — call graph context for planning
    symbol_context = ""
    try:
        from src.core.indexing.symbol_graph import SymbolGraph
        from pathlib import Path

        sg = SymbolGraph(working_dir)

        # Update index for all found relevant files (multi-lang: update_file handles suffix check)
        for fp in relevant_files[:10]:
            full_path = Path(working_dir) / fp
            if full_path.exists():
                sg.update_file(str(full_path))

        # Find call sites for key symbols
        call_info = []
        for sym in key_symbols[:5]:
            callers = sg.find_calls(sym)
            if callers:
                call_info.append(f"  '{sym}' called by: {', '.join(callers[:5])}")

        # Find related tests for first relevant module
        test_info = []
        if relevant_files:
            module_name = Path(relevant_files[0]).stem
            tests = sg.find_tests_for_module(module_name)
            if tests:
                test_info = tests[:3]

        if call_info or test_info:
            symbol_context = "Symbol graph:\n"
            symbol_context += "\n".join(call_info)
            if test_info:
                symbol_context += f"\nRelated tests: {', '.join(test_info)}"
    except Exception as e:
        logger.warning(f"analysis_node: symbol graph enrichment failed: {e}")

    # Phase 3: ContextController — enforce token budget on relevant_files list
    # Prioritizes files that appear in semantic search results (higher relevance)
    try:
        from src.core.context.context_controller import ContextController

        cc = ContextController()
        relevance_scores = {}
        for i, fp in enumerate(relevant_files):
            # Files from semantic search get higher scores; others get lower
            relevance_scores[fp] = 1.0 - (i * 0.05)

        file_infos = [
            {"path": fp, "line_count": 50, "estimated_tokens": 200}
            for fp in relevant_files
        ]
        history = state.get("history", [])
        included, excluded = cc.enforce_budget(
            file_infos, history, system_prompt=repo_summary_data
        )
        if excluded:
            logger.info(
                f"analysis_node: ContextController excluded {len(excluded)} low-priority files"
            )
        relevant_files = [f["path"] for f in included]
    except Exception as e:
        logger.debug(f"analysis_node: context controller skipped: {e}")

    return {
        "analysis_summary": analysis_summary
        + ("\n" + symbol_context if symbol_context else ""),
        "relevant_files": relevant_files,
        "key_symbols": key_symbols,
        "repo_summary_data": repo_summary_data,
    }
