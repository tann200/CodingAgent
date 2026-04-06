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
from contextvars import ContextVar
from typing import Dict, Any, Optional, cast
from pathlib import Path

from src.tools._tool import tool

# SPAWN-W1: ContextVar that carries the parent orchestrator reference into tool calls.
# Set by Orchestrator.execute_tool() before dispatching; cleared automatically on exit.
# Tools that spawn subagents (delegate_task) read this to access the full pipeline.
_PARENT_ORCHESTRATOR_VAR: ContextVar[Any] = ContextVar(
    "_parent_orchestrator", default=None
)

# HR-5: Process-local delegation depth counter (not forgeable by subprocesses).
# ContextVar is the authoritative source for in-process depth checks.
# Subprocess inheritance is not supported — subprocesses start at depth 0.
_DELEGATION_DEPTH_VAR: ContextVar[int] = ContextVar("_delegation_depth", default=0)
_MAX_DELEGATION_DEPTH = 3

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

    def get_role_config(role: str) -> Optional[Dict[str, Any]]:
        return {}

    def is_tool_allowed_for_role(tool_name: str, role: str) -> bool:
        # QUAL-1: Fail closed — deny all tools when role_config is unavailable
        # rather than silently allowing everything.  This prevents privilege
        # escalation if the import fails due to a misconfiguration or partial install.
        logger.warning(
            "is_tool_allowed_for_role: role_config unavailable — denying tool '%s' for role '%s'",
            tool_name,
            role,
        )
        return False


logger = logging.getLogger(__name__)


class SubagentOrchestrator:
    """
    Minimal orchestrator-like object for subagent role enforcement.

    This provides:
    - current_role for tool restriction
    - Basic tool registry
    - No execution (subagent handles its own execution)
    """

    def __init__(
        self,
        role: str,
        working_dir: str,
        allowed_tools: Optional[set] = None,
        denied_tools: Optional[set] = None,
    ):
        self.current_role = normalize_role(role)
        self.working_dir = Path(working_dir)
        self.tool_registry = None  # Will be set by subagent
        self.cancel_event = None
        self.adapter = None  # perception_node requires orchestrator.adapter (may be None for subagents)
        # SPAWN-W2: per-agent tool allowlist / denylist from AgentDefinition
        self._allowed_tools: Optional[set] = (
            set(allowed_tools) if allowed_tools is not None else None
        )
        self._denied_tools: set = set(denied_tools) if denied_tools else set()

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if tool is allowed for this role + any active allowlist."""
        # SPAWN-W2: allowlist enforcement — reject if outside AgentDefinition.allowed_tools
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            return False
        if tool_name in self._denied_tools:
            return False
        return is_tool_allowed_for_role(tool_name, self.current_role)

    def get_denied_tools(self) -> list:
        """Get list of denied tools for this role."""
        config = get_role_config(self.current_role)
        base_denied: list = []
        if config:
            base_denied = config.get("denied_tools", [])
        return list(set(base_denied) | self._denied_tools)


@tool(side_effects=["execute"], tags=["planning"])
def delegate_task(
    role: str,
    subtask_description: str,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[list] = None,
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
        allowed_tools: Optional explicit list of tool names the subagent may use.
                       When provided, any tool not in this list is rejected.
                       When omitted, the AgentDefinition from the registry for
                       the given role is consulted (SPAWN-W2).

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

    if not subtask_description or not subtask_description.strip():
        return "Error: subtask_description must not be empty."

    workdir = working_dir or "."
    workdir_path = Path(workdir).resolve()

    # HR-5 fix: Check delegation depth to prevent unbounded recursive spawning.
    # Use the process-local ContextVar as the authoritative source — it cannot
    # be forged by subprocesses, unlike os.environ.
    import os

    depth = _DELEGATION_DEPTH_VAR.get()
    if depth >= _MAX_DELEGATION_DEPTH:
        return (
            f"Error: Maximum delegation depth ({_MAX_DELEGATION_DEPTH}) exceeded. "
            f"Refusing to spawn additional subagent to prevent infinite recursion."
        )

    logger.info(
        f"delegate_task: spawning {role} subagent for: {subtask_description[:100]}..."
    )

    try:
        # 1. Resolve the appropriate graph based on the role
        if GraphFactory is None:
            return "Error: GraphFactory is not available (src.core not importable)"
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
        if get_agent_brain_manager is None:
            return "Error: AgentBrainManager is not available (src.core not importable)"
        brain = get_agent_brain_manager()
        system_prompt = brain.compile_system_prompt(canonical_role)

        # SPAWN-W2: Resolve allowed_tools from explicit param or AgentDefinition registry.
        _effective_allowed: Optional[set] = None
        _effective_denied: set = set()
        if allowed_tools is not None:
            _effective_allowed = set(allowed_tools)
        else:
            try:
                from src.core.orchestration.agent_types import get_agent_registry

                _agent_def = get_agent_registry().get(canonical_role)
                if _agent_def is not None:
                    _effective_allowed = (
                        _agent_def.allowed_tools
                    )  # may be None (no restriction)
                    _effective_denied = _agent_def.denied_tools or set()
            except Exception:
                pass

        # CP-1: Structural recursion prevention — subagents must never be able
        # to spawn further subagents via delegate_task (or its async variant).
        # The depth-env-var check is belt-and-suspenders; this denylist ensures
        # the tool is structurally absent from every subagent's toolset.
        _effective_denied = _effective_denied | {"delegate_task", "delegate_task_async"}
        if _effective_allowed is not None:
            # Also strip from allowlist if it was explicitly included
            _effective_allowed = _effective_allowed - {
                "delegate_task",
                "delegate_task_async",
            }

        # Create role-aware orchestrator for tool restriction enforcement
        subagent_orchestrator = SubagentOrchestrator(
            role=role,
            working_dir=str(workdir_path),
            allowed_tools=_effective_allowed,
            denied_tools=_effective_denied,
        )

        # SPAWN-W1: Generate a child session ID and track parent reference.
        import uuid as _uuid

        child_session_id = str(_uuid.uuid4())[:8]
        parent_orchestrator = _PARENT_ORCHESTRATOR_VAR.get(None)
        parent_session_id = None
        if parent_orchestrator is not None:
            parent_session_id = getattr(parent_orchestrator, "_current_task_id", None)

        initial_state = {
            "task": subtask_description,
            "session_id": child_session_id,
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
            # SPAWN-W1: Track delegation hierarchy
            "parent_session_id": parent_session_id,
            "delegation_depth": depth + 1,
        }

        # SPAWN-W1: Use the full compiled LangGraph pipeline when a parent orchestrator
        # is available (real execution context), otherwise fall back to GraphFactory mini-graph.
        # The parent orchestrator is passed as-is so the child has full tool access.
        _use_full_pipeline = parent_orchestrator is not None
        if _use_full_pipeline:
            try:
                from src.core.orchestration.graph.builder import _get_compiled_graph

                graph = _get_compiled_graph()
                # Set active_agent on parent orchestrator to enforce allowed_tools
                if _effective_allowed is not None or _effective_denied:
                    from src.core.orchestration.agent_types import AgentDefinition

                    _child_agent = AgentDefinition(
                        id=f"delegated_{canonical_role}",
                        name=f"Delegated {canonical_role}",
                        description="Auto-created delegated agent context",
                        allowed_tools=_effective_allowed,
                        denied_tools=_effective_denied,
                    )
                    parent_orchestrator.active_agent = _child_agent
                _active_orchestrator = parent_orchestrator
            except Exception as _e:
                logger.warning(
                    "SPAWN-W1: Failed to use full pipeline: %s; falling back", _e
                )
                _use_full_pipeline = False
                _active_orchestrator = subagent_orchestrator
        else:
            _active_orchestrator = subagent_orchestrator

        # 3. Execute the subagent synchronously (blocking until done).
        # Always run in a dedicated thread so we never conflict with an existing event loop.
        # The lambda ensures the coroutine is created inside the worker thread, not on the
        # calling thread (which would be unsafe when passed across threads).
        import concurrent.futures

        def _run_subagent():
            from src.core.orchestration.graph.state import AgentState as _AgentState

            return asyncio.run(
                graph.ainvoke(
                    cast(_AgentState, initial_state),
                    {
                        "configurable": {"orchestrator": _active_orchestrator},
                        "recursion_limit": 50,
                    },
                )
            )

        # CP-2: Write manifest JSON *before* spawning the subagent thread so the
        # parent's context directory always contains a record of the spawned work,
        # even if the subagent crashes or is cancelled.
        import json as _json
        import time as _time

        _manifest_dir = workdir_path / ".agent-context" / "subagent_manifests"
        _manifest: dict = {}  # initialised here so later update blocks are always bound
        _manifest_path: Optional[Path] = None
        try:
            _manifest_dir.mkdir(parents=True, exist_ok=True)
            _manifest = {
                "child_session_id": child_session_id,
                "parent_session_id": parent_session_id,
                "role": canonical_role,
                "task": subtask_description,
                "working_dir": str(workdir_path),
                "spawned_at": _time.time(),
                "status": "running",
            }
            _manifest_path = _manifest_dir / f"subagent_{child_session_id}.json"
            _manifest_path.write_text(
                _json.dumps(_manifest, indent=2), encoding="utf-8"
            )
        except Exception as _me:
            logger.debug("delegate_task: manifest write failed: %s", _me)
            _manifest_path = None

        try:
            # Propagate incremented depth into the child thread's context so that
            # any further in-process delegate_task calls see the correct depth.
            import contextvars as _cv

            _child_ctx = _cv.copy_context()
            _child_ctx.run(_DELEGATION_DEPTH_VAR.set, depth + 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_child_ctx.run, _run_subagent)
                final_state = future.result(timeout=300)

            # SPAWN-W1: Reset active_agent after child run
            if _use_full_pipeline and parent_orchestrator is not None:
                parent_orchestrator.active_agent = None

            # CP-2: Update manifest to completed
            if _manifest_path is not None:
                try:
                    _manifest["status"] = "completed"
                    _manifest["completed_at"] = _time.time()
                    _manifest_path.write_text(
                        _json.dumps(_manifest, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass

        except Exception as _subagent_err:
            if _use_full_pipeline and parent_orchestrator is not None:
                parent_orchestrator.active_agent = None

            # CP-2: Update manifest to failed
            if _manifest_path is not None:
                try:
                    _manifest["status"] = "failed"
                    _manifest["error"] = str(_subagent_err)
                    _manifest["failed_at"] = _time.time()
                    _manifest_path.write_text(
                        _json.dumps(_manifest, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass

            raise _subagent_err

        # SPAWN-W3: Persist child session in SessionStore so it's queryable later.
        if parent_session_id is not None:
            try:
                from src.core.memory.session_store import SessionStore

                _store = SessionStore(workdir=str(workdir_path))
                _store.register_child_session(
                    parent_session_id=parent_session_id,
                    child_session_id=child_session_id,
                    role=canonical_role,
                    task=subtask_description,
                )
            except Exception as _se:
                logger.debug(
                    "delegate_task: session store registration failed: %s", _se
                )

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

            # SPAWN-W3: Append child_session_id to result so callers can reference it.
            result_parts.append(f"**child_session_id:** {child_session_id}")
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
