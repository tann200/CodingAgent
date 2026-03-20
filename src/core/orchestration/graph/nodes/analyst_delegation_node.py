"""
#56: Analyst delegation node — runs between analysis and planning for complex tasks.

Delegates to an 'analyst' subagent to produce a deep-dive <findings> report that
the planning_node can use to generate a higher-quality, better-scoped plan.
"""
from __future__ import annotations
import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.tools.subagent_tools import delegate_task_async

logger = logging.getLogger(__name__)


async def analyst_delegation_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    #56: Early delegation phase — spawns an analyst subagent before planning.

    Reads analysis output (relevant_files, analysis_summary, task) and asks an
    analyst subagent to produce a structured <findings> report.  The result is
    stored in state["analyst_findings"] and picked up by planning_node.

    On any error the node returns an empty analyst_findings so the pipeline is
    never blocked.
    """
    logger.info("=== analyst_delegation_node START ===")

    task = state.get("task") or ""
    analysis_summary = state.get("analysis_summary") or ""
    relevant_files = state.get("relevant_files") or []
    working_dir = state.get("working_dir", ".")

    files_hint = ", ".join(str(f) for f in relevant_files[:10]) if relevant_files else "unknown"

    subtask = (
        f"Task: {task}\n\n"
        f"The static analysis phase identified these relevant files: {files_hint}\n"
        f"Analysis summary: {analysis_summary}\n\n"
        "Perform a focused deep-dive on the identified files and task. "
        "Output a structured <findings> block covering:\n"
        "1. Key classes / functions that must change\n"
        "2. Dependencies and call-graph impact\n"
        "3. Risk areas (e.g. shared state, public API breaks)\n"
        "4. Suggested implementation approach\n"
        "Keep findings concise — this will be injected directly into the planner prompt."
    )

    try:
        result = await delegate_task_async(
            role="analyst",
            subtask_description=subtask,
            working_dir=working_dir,
        )
        findings = result if isinstance(result, str) else str(result)
        logger.info(
            f"analyst_delegation_node: analyst returned {len(findings)} chars"
        )
    except Exception as e:
        logger.warning(f"analyst_delegation_node: analyst delegation failed: {e}")
        findings = ""

    logger.info("=== analyst_delegation_node END ===")
    return {"analyst_findings": findings}
