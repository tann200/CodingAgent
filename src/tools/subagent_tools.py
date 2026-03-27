"""
Subagent tools for spawning isolated autonomous agents.

These tools allow the main agent to spawn subagents for specific tasks,
keeping the main agent's context window clean.

When used as a standalone module (without src.core), the delegate_task
function will still work but requires an externally-supplied graph and
system prompt via the `tools_config.configure()` mechanism.
"""

import asyncio
import logging
import os
from typing import Dict, Any, Optional
from pathlib import Path

from src.tools._tool import tool

# Lazy imports — degrade gracefully when src.core is not available
try:
    from src.core.orchestration.graph_factory import GraphFactory
except ImportError:
    GraphFactory = None

try:
    from src.core.orchestration.agent_brain import get_agent_brain_manager
except ImportError:
    get_agent_brain_manager = None

try:
    from src.core.orchestration.role_config import (
        normalize_role,
        get_role_config,
        is_tool_allowed_for_role,
    )
except ImportError:

    def normalize_role(role: str) -> str:
        """Identity fallback when role_config is not available."""
        return role

    def get_role_config(role: str) -> Dict[str, Any]:
        return {}

    def is_tool_allowed_for_role(tool_name: str, role: str) -> bool:
        return True


logger = logging.getLogger(__name__)


class SubagentOrchestrator:
    """
    Minimal orchestrator-like object for subagent role enforcement.

    This provides:
    - current_role for tool restriction
    - Basic tool registry
    - No execution (subagent handles its own execution)
    """

    def __init__(self, role: str, working_dir: str):
        self.current_role = normalize_role(role)
        self.working_dir = Path(working_dir)
        self.tool_registry = None  # Will be set by subagent
        self.cancel_event = None

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if tool is allowed for this role."""
        return is_tool_allowed_for_role(tool_name, self.current_role)

    def get_denied_tools(self) -> list:
        """Get list of denied tools for this role."""
        config = get_role_config(self.current_role)
        if config:
            return config.get("denied_tools", [])
        return []


@tool(side_effects=["execute"], tags=["planning"])
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
    valid_roles = {
        "researcher",
        "coder",
        "reviewer",
        "planner",
        "analyst",
        "operational",
        "strategic",
        "debugger",
    }
    if role not in valid_roles:
        return (
            f"Error: Invalid role '{role}'. Valid roles are: {', '.join(valid_roles)}"
        )

    workdir = working_dir or "."
    workdir_path = Path(workdir).resolve()

    # HR-5 fix: Check delegation depth to prevent unbounded recursive spawning
    # Depth is passed via environment variable from the calling delegation_node
    import os

    depth = int(os.environ.get("CODINGAGENT_DELEGATION_DEPTH", "0"))
    if depth >= 3:
        return (
            f"Error: Maximum delegation depth (3) exceeded. "
            f"Refusing to spawn additional subagent to prevent infinite recursion."
        )

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

        # Create role-aware orchestrator for tool restriction enforcement
        subagent_orchestrator = SubagentOrchestrator(
            role=role, working_dir=str(workdir_path)
        )

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
            "current_role": subagent_orchestrator.current_role,  # Pass role to state
        }

        # 3. Execute the subagent synchronously (blocking until done).
        # Always run in a dedicated thread so we never conflict with an existing event loop.
        # The lambda ensures the coroutine is created inside the worker thread, not on the
        # calling thread (which would be unsafe when passed across threads).
        import concurrent.futures

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: asyncio.run(
                        graph.ainvoke(
                            initial_state,
                            {
                                "configurable": {"orchestrator": subagent_orchestrator},
                                "recursion_limit": 50,
                            },
                        )
                    )
                )
                final_state = future.result()
        except AttributeError:
            # Fallback to sync invoke if ainvoke not available
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    lambda: graph.invoke(
                        initial_state,
                        {
                            "configurable": {"orchestrator": subagent_orchestrator},
                            "recursion_limit": 50,
                        },
                    )
                )
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


@tool(tags=["planning"])
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

    # HR-12 fix: add timeout to prevent subagent from hanging forever
    _DELEGATION_TIMEOUT_SECONDS = 300.0  # 5 minutes max per subagent

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            delegate_task,
            role,
            subtask_description,
            working_dir,
        )
        try:
            return future.result(timeout=_DELEGATION_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return (
                f"Error: Delegation to '{role}' subagent timed out after "
                f"{_DELEGATION_TIMEOUT_SECONDS} seconds. The subagent may be "
                f"hanging or the task is too complex."
            )
