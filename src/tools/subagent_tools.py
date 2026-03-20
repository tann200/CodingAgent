"""
Subagent tools for spawning isolated autonomous agents.

These tools allow the main agent to spawn subagents for specific tasks,
keeping the main agent's context window clean.
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from pathlib import Path

from src.core.orchestration.graph_factory import GraphFactory
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


def delegate_task(
    role: str,
    subtask_description: str,
    working_dir: Optional[str] = None,
) -> str:
    """
    Spawns an isolated autonomous subagent to complete a specific subtask.
    Use this for deep research, isolated debugging, or heavy refactoring
    to keep your own context window clean.

    Args:
        role: The role of the subagent. Valid values:
              'analyst'    (or 'researcher') - deep research / repo exploration
              'operational' (or 'coder')     - code implementation and edits
              'reviewer'                     - code review and QA
              'strategic'  (or 'planner')    - task decomposition and planning
              'debugger'                     - root-cause analysis and fixes
        subtask_description: Highly detailed instructions for the subtask
        working_dir: The directory to execute in (defaults to current directory)

    Returns:
        Summary of the subagent's work and final result

    Raises:
        ValueError: If an invalid role is provided
    """
    valid_roles = {"researcher", "coder", "reviewer", "planner",
                   "analyst", "operational", "strategic", "debugger"}
    if role not in valid_roles:
        return (
            f"Error: Invalid role '{role}'. Valid roles are: {', '.join(valid_roles)}"
        )

    workdir = working_dir or "."
    workdir_path = Path(workdir).resolve()

    logger.info(
        f"delegate_task: spawning {role} subagent for: {subtask_description[:100]}..."
    )

    try:
        # 1. Resolve the appropriate graph based on the role
        graph = GraphFactory.get_graph(role)

        if graph is None:
            return f"Error: Could not create graph for role '{role}'"

        # 2. Setup the isolated initial state
        # Map legacy role names to canonical so AgentBrainManager finds the right prompts
        _legacy_to_canonical = {
            "researcher": "analyst",
            "coder": "operational",
            "planner": "strategic",
            "reviewer": "reviewer",
            # canonical names pass through unchanged
            "analyst": "analyst",
            "operational": "operational",
            "strategic": "strategic",
            "debugger": "debugger",
        }
        canonical_role = _legacy_to_canonical.get(role, role)
        brain = get_agent_brain_manager()
        system_prompt = brain.compile_system_prompt(canonical_role)

        initial_state = {
            "task": subtask_description,
            "session_id": None,
            "working_dir": str(workdir_path),
            "history": [],
            "system_prompt": system_prompt,
            "rounds": 0,
            "errors": [],
            "verified_reads": [],
            "next_action": None,
            "current_plan": [],
            "current_step": 0,
            "last_result": None,
            "plan_validation": None,
            "verification_result": None,
            "evaluation_result": None,
            "delegations": [],
            "delegation_results": None,
        }

        # 3. Execute the subagent synchronously (blocking until done).
        # Always run in a dedicated thread so we never conflict with an existing event loop.
        # The lambda ensures the coroutine is created inside the worker thread, not on the
        # calling thread (which would be unsafe when passed across threads).
        import concurrent.futures

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: asyncio.run(graph.ainvoke(initial_state))
                )
                final_state = future.result()
        except AttributeError:
            # Fallback to sync invoke if ainvoke not available
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: graph.invoke(initial_state))
                final_state = future.result()

        # 4. Extract and summarize the result
        if isinstance(final_state, dict):
            last_result = final_state.get("last_result", {})
            history = final_state.get("history", [])
            task = final_state.get("task", "")
            errors = final_state.get("errors", [])

            # Check for errors during execution
            if errors:
                error_summary = "\n".join(errors[:5])  # Limit to 5 errors
                return f"Subagent [{role}] completed with errors:\n{error_summary}"

            # Pull the last assistant message as the summary of work done
            summary = "Subagent completed execution."
            for msg in reversed(history):
                if isinstance(msg, dict):
                    role_val = msg.get("role")
                    content = msg.get("content")
                    if role_val == "assistant" and content:
                        summary = content
                        break

            # Format a clean result
            result_parts = [
                f"## Subagent [{role}] Execution Complete",
                "",
                f"**Task:** {task[:200]}..."
                if len(task) > 200
                else f"**Task:** {task}",
                "",
                f"**Summary:** {summary[:500]}..."
                if len(summary) > 500
                else f"**Summary:** {summary}",
            ]

            if last_result:
                if isinstance(last_result, dict):
                    status = last_result.get("status", "unknown")
                    result_parts.append(f"**Status:** {status}")

                    # Include file operations info
                    if last_result.get("file"):
                        result_parts.append(f"**File:** {last_result.get('file')}")

                    error = last_result.get("error")
                    if error:
                        result_parts.append(f"**Error:** {error}")
                else:
                    result_parts.append(f"**Result:** {str(last_result)[:200]}")

            return "\n".join(result_parts)

        else:
            return f"Subagent [{role}] finished with unexpected result type: {type(final_state)}"

    except Exception as e:
        logger.error(f"delegate_task: subagent failed: {e}")
        return f"Subagent [{role}] failed during execution: {str(e)}"


def list_subagent_roles() -> Dict[str, Any]:
    """
    List available subagent roles and their purposes.

    Returns:
        Dictionary of available roles and descriptions
    """
    roles = {
        "analyst": {
            "description": "Deep research and repository analysis",
            "best_for": "Exploring codebase, finding patterns, understanding architecture",
            "aliases": ["researcher"],
        },
        "operational": {
            "description": "Code implementation and refactoring",
            "best_for": "Writing new code, implementing features, editing files",
            "aliases": ["coder"],
        },
        "strategic": {
            "description": "Task decomposition and planning",
            "best_for": "Breaking down complex tasks, creating execution plans",
            "aliases": ["planner"],
        },
        "reviewer": {
            "description": "Code review and verification",
            "best_for": "Reviewing patches, checking for issues, verifying changes",
            "aliases": [],
        },
        "debugger": {
            "description": "Root-cause analysis and bug fixing",
            "best_for": "Diagnosing failures, analysing tracebacks, producing fixes",
            "aliases": [],
        },
    }

    return {
        "status": "ok",
        "available_roles": roles,
        "note": "Pass the canonical role name (e.g. 'analyst') or any alias to delegate_task.",
    }


async def delegate_task_async(
    role: str,
    subtask_description: str,
    working_dir: Optional[str] = None,
) -> str:
    """
    Async version of delegate_task for use in async contexts.

    Args:
        role: The role of the subagent
        subtask_description: Detailed instructions for the subtask
        working_dir: The directory to execute in

    Returns:
        Summary of the subagent's work
    """
    # Run the sync version in a thread to avoid blocking.
    # max_workers=1: only one task is submitted per executor, no need for an unbounded pool (NEW-16).
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            delegate_task,
            role,
            subtask_description,
            working_dir,
        )
        return future.result()
