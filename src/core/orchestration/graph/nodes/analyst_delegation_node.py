"""
#56: Analyst delegation node — runs between analysis and planning for complex tasks.

Delegates to analyst subagent(s) to produce a deep-dive <findings> report that
the planning_node can use to generate a higher-quality, better-scoped plan.

GAP-FRONTIER-3: For FRONTIER/LARGE model tiers with complex tasks, spawns up to 3
parallel analyst subagents covering different aspects of the codebase simultaneously:
  - Analyst 1: file structure + entry points
  - Analyst 2: symbol graph + dependencies
  - Analyst 3: test coverage + existing patterns

Their results are merged into a single analyst_findings block.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Mapping, Dict, Any, List, Optional

from src.core.orchestration.graph.state import AgentState
from src.tools.subagent_tools import delegate_task_async

logger = logging.getLogger(__name__)

# Tiers that receive parallel analyst delegation.
_PARALLEL_ANALYST_TIERS = {"frontier", "large"}


def _build_parallel_subtasks(task: str, files_hint: str, analysis_summary: str) -> List[str]:
    """Return three focused analyst subtasks for parallel execution."""
    base = f"Task: {task}\n\nRelevant files: {files_hint}\nAnalysis summary: {analysis_summary}\n\n"
    return [
        base + (
            "FOCUS: File structure and entry points.\n"
            "Map the relevant files, identify module boundaries, entry points, "
            "and public APIs that this task will touch. "
            "Output a <findings> block listing each file with its purpose and relevant exports."
        ),
        base + (
            "FOCUS: Symbol graph and dependencies.\n"
            "Identify the key classes, functions, and data structures involved. "
            "Map call-graph relationships — who calls what, shared state, "
            "and which changes will have cascading effects. "
            "Output a <findings> block listing dependencies and risk areas."
        ),
        base + (
            "FOCUS: Test coverage and existing patterns.\n"
            "Find existing tests for the relevant code. Identify test patterns used "
            "in this project (pytest fixtures, mocks, helpers). "
            "Note any gaps in test coverage relevant to this task. "
            "Output a <findings> block with test file paths and recommendations."
        ),
    ]


def _merge_findings(results: List[str]) -> str:
    """Merge multiple analyst findings into one block."""
    sections = ["file_structure", "dependencies", "test_coverage"]
    merged_parts = []
    for i, (section, result) in enumerate(zip(sections, results)):
        if result and result.strip():
            merged_parts.append(f"<analyst_{section}>\n{result.strip()}\n</analyst_{section}>")
    return "\n\n".join(merged_parts) if merged_parts else ""


async def analyst_delegation_node(
    state: Mapping[str, Any], config: Any
) -> Dict[str, Any]:
    """
    #56 + GAP-FRONTIER-3: Early delegation phase — spawns analyst subagent(s) before planning.

    For FRONTIER/LARGE tiers: spawns 3 parallel analysts (file structure, dependencies,
    test coverage) and merges results.

    For all other tiers: spawns a single analyst as before.

    On any error the node returns empty analyst_findings so the pipeline is never blocked.
    """
    logger.info("=== analyst_delegation_node START ===")

    task = state.get("task") or ""
    analysis_summary = state.get("analysis_summary") or ""
    relevant_files = state.get("relevant_files") or []
    working_dir = state.get("working_dir", ".")
    model_tier = (state.get("model_tier") or "").lower()

    files_hint = (
        ", ".join(str(f) for f in relevant_files[:10]) if relevant_files else "unknown"
    )

    # WF-VOL22-1: consistent asyncio-level cancellation timeout
    _analyst_timeout: int = 300
    try:
        from src.core.orchestration.project_settings import (
            get_active_settings as _gas_an,
        )

        _ps_an = _gas_an()
        if _ps_an is not None and _ps_an.max_llm_wait_seconds:
            _analyst_timeout = int(_ps_an.max_llm_wait_seconds * 2.5)
    except Exception:
        pass

    findings = ""

    if model_tier in _PARALLEL_ANALYST_TIERS:
        # GAP-FRONTIER-3: parallel analyst subagents for FRONTIER/LARGE
        logger.info(
            "analyst_delegation_node: tier=%s — spawning 3 parallel analyst subagents",
            model_tier,
        )
        subtasks = _build_parallel_subtasks(task, files_hint, analysis_summary)
        try:
            raw_results = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        delegate_task_async(
                            role="analyst",
                            subtask_description=st,
                            working_dir=working_dir,
                        )
                        for st in subtasks
                    ],
                    return_exceptions=True,
                ),
                timeout=_analyst_timeout,
            )
            str_results: List[str] = []
            for r in raw_results:
                if isinstance(r, Exception):
                    logger.warning("analyst_delegation_node: parallel analyst failed: %s", r)
                    str_results.append("")
                else:
                    str_results.append(r if isinstance(r, str) else str(r))
            findings = _merge_findings(str_results)
            logger.info(
                "analyst_delegation_node: parallel analysts returned %d chars total",
                len(findings),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "analyst_delegation_node: parallel analysts timed out after %ds", _analyst_timeout
            )
        except Exception as e:
            logger.warning("analyst_delegation_node: parallel delegation failed: %s", e)
    else:
        # Single analyst (original behaviour for NANO/SMALL/MEDIUM)
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
            result = await asyncio.wait_for(
                delegate_task_async(
                    role="analyst",
                    subtask_description=subtask,
                    working_dir=working_dir,
                ),
                timeout=_analyst_timeout,
            )
            findings = result if isinstance(result, str) else str(result)
            logger.info(
                "analyst_delegation_node: analyst returned %d chars", len(findings)
            )
        except Exception as e:
            logger.warning("analyst_delegation_node: analyst delegation failed: %s", e)

    logger.info("=== analyst_delegation_node END ===")
    return {"analyst_findings": findings}
