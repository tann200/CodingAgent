import logging
import asyncio
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.tools.repo_summary import generate_repo_summary
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


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

    brain = get_agent_brain_manager()
    analyst_role = brain.get_role("analyst") or "You are a repository analyst."

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

        fs = _call_tool_if_exists(
            "find_symbol", name=task.split()[0] if task else "", workdir=working_dir
        )
        if fs and isinstance(fs, dict):
            fp = fs.get("file_path")
            if fp and fp not in relevant_files:
                relevant_files.append(fp)
            sym = fs.get("symbol_name")
            if sym and sym not in key_symbols:
                key_symbols.append(sym)

        gl = _call_tool_if_exists("glob", pattern="**/*.py", workdir=working_dir)
        if gl and isinstance(gl, dict):
            items = gl.get("items", [])
            for item in items[:20]:
                fp = item.get("name") if isinstance(item, dict) else item
                if fp and fp.endswith(".py") and fp not in relevant_files:
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

    return {
        "analysis_summary": analysis_summary,
        "relevant_files": relevant_files,
        "key_symbols": key_symbols,
        "repo_summary_data": repo_summary_data,
    }
