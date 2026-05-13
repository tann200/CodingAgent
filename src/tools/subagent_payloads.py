"""Pure helpers for subagent role normalization and persisted payloads."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Optional, Tuple


DELEGATION_DENYLIST = {"delegate_task", "delegate_task_async"}


def canonicalize_subagent_role(role: str) -> str:
    """Map legacy role aliases to canonical role names."""
    return {
        "researcher": "analyst",
        "coder": "operational",
        "planner": "strategic",
        "reviewer": "reviewer",
        "analyst": "analyst",
        "operational": "operational",
        "strategic": "strategic",
        "debugger": "debugger",
        "general": "general",
        "generalist": "general",
        "scout": "scout",
        "tester": "tester",
    }.get(role, role)


def compute_effective_tool_policy(
    *,
    explicit_allowed_tools: Optional[Iterable[str]],
    registry_allowed_tools: Optional[Iterable[str]],
    registry_denied_tools: Optional[Iterable[str]],
) -> Tuple[Optional[set[str]], set[str]]:
    """Resolve the effective allow/deny sets for a delegated subagent."""
    allowed: Optional[set[str]] = None
    denied = set(registry_denied_tools or [])

    if explicit_allowed_tools is not None:
        allowed = set(explicit_allowed_tools)
    elif registry_allowed_tools is not None:
        allowed = set(registry_allowed_tools)

    denied |= DELEGATION_DENYLIST
    if allowed is not None:
        allowed -= DELEGATION_DENYLIST

    return allowed, denied


def build_subagent_manifest(
    *,
    child_session_id: str,
    parent_session_id: Optional[str],
    canonical_role: str,
    task: str,
    working_dir: str,
    spawned_at: float,
) -> Dict[str, Any]:
    """Build the initial persisted manifest payload for a delegated subagent."""
    return {
        "child_session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "role": canonical_role,
        "task": task,
        "working_dir": working_dir,
        "spawned_at": spawned_at,
        "status": "running",
    }


def build_subagent_session_payload(
    *,
    child_session_id: str,
    parent_session_id: Optional[str],
    task_name: str,
    canonical_role: str,
    working_dir: str,
    timestamp: float,
    messages: list[dict],
    ok: bool,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the persisted session payload shown by the TUI session list."""
    payload: Dict[str, Any] = {
        "version": 1,
        "session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "timestamp": timestamp,
        "task_name": task_name[:80],
        "role": canonical_role,
        "message_count": len(messages),
        "messages": messages,
        "working_dir": working_dir,
        "ok": ok,
    }
    if error is not None:
        payload["error"] = error
    return payload


def extract_child_session_messages(final_state: Any) -> list[dict[str, Any]]:
    """Extract persisted child-session messages from final graph state."""
    if not isinstance(final_state, dict):
        return []

    history = final_state.get("history") or []
    if history:
        return list(history)

    messages = []
    for msg in final_state.get("messages") or []:
        if hasattr(msg, "type") and hasattr(msg, "content"):
            messages.append(
                {
                    "role": getattr(msg, "type", "unknown"),
                    "content": str(msg.content),
                }
            )
    return messages


def build_child_session_file_path(sessions_dir: str, child_session_id: str) -> str:
    """Build the persisted child-session JSON path."""
    from pathlib import Path

    return str(Path(sessions_dir) / f"session_{child_session_id}.json")


def select_dispatch_result_content(final_state: Any) -> str:
    """Pick the best summary content for DispatchResultEvent publication."""
    if not isinstance(final_state, dict):
        return ""

    work_summary = final_state.get("work_summary")
    if work_summary:
        return str(work_summary)

    history = final_state.get("history") or []
    for msg in reversed(history):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            if content:
                return str(content)

    return ""


def build_subagent_roles_payload() -> Dict[str, Any]:
    """Build the response payload returned by ``list_subagent_roles``."""
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
        "scout": {
            "description": "Rapid codebase exploration and file discovery",
            "best_for": "Finding relevant files, analyzing structures, discovering dependencies",
            "aliases": [],
        },
        "tester": {
            "description": "Test creation and execution",
            "best_for": "Writing tests, running test suites, reporting coverage",
            "aliases": [],
        },
    }
    return {
        "status": "ok",
        "available_roles": roles,
        "note": "Pass the canonical role name (e.g. 'analyst') or any alias to delegate_task.",
    }


def build_graph_state_base(
    *,
    task: str,
    session_id: str,
    working_dir: str,
    system_prompt: str,
    history: Optional[list[dict]] = None,
    verified_reads: Optional[list[Any]] = None,
    errors: Optional[list[Any]] = None,
    current_plan: Optional[list[Any]] = None,
    current_step: int = 0,
    parent_session_id: Optional[str] = None,
    delegation_depth: int = 0,
    override_model: Optional[str] = None,
    current_role: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the shared base graph state for top-level and delegated runs."""
    return {
        "task": task,
        "session_id": session_id,
        "working_dir": working_dir,
        "history": list(history or []),
        "system_prompt": system_prompt,
        "rounds": 0,
        "errors": list(errors or []),
        "verified_reads": list(verified_reads or []),
        "next_action": None,
        "current_plan": list(current_plan or []),
        "current_step": current_step,
        "last_result": None,
        "plan_validation": None,
        "verification_result": None,
        "evaluation_result": None,
        "delegations": [],
        "delegation_results": None,
        "parent_session_id": parent_session_id,
        "delegation_depth": delegation_depth,
        "override_model": override_model,
        "current_role": current_role,
    }


def build_subagent_initial_state(
    *,
    subtask_description: str,
    child_session_id: str,
    working_dir: str,
    system_prompt: str,
    current_role: str,
    parent_session_id: Optional[str],
    delegation_depth: int,
    override_model: Optional[str],
    resumed_state: Optional[dict] = None,
) -> Dict[str, Any]:
    """Build the initial state payload passed into a delegated graph."""
    initial_state: Dict[str, Any] = build_graph_state_base(
        task=subtask_description,
        session_id=child_session_id,
        working_dir=working_dir,
        system_prompt=system_prompt,
        parent_session_id=parent_session_id,
        delegation_depth=delegation_depth,
        override_model=override_model,
        current_role=current_role,
    )

    if resumed_state:
        preserve = (
            "history",
            "current_plan",
            "current_step",
            "verified_reads",
            "files_read",
            # NOTE: plan_validation, verification_result, evaluation_result, and
            # plan_mode_approved are intentionally excluded (FAULT-08): these are
            # workflow-phase signals that must not bleed from parent into child subagent.
            "affected_files",
            "model_tier",
        )
        for key in preserve:
            if key in resumed_state and resumed_state[key] is not None:
                initial_state[key] = deepcopy(resumed_state[key])
        initial_state["task"] = subtask_description
        initial_state["session_id"] = child_session_id
        initial_state["delegation_depth"] = delegation_depth

    return initial_state


def build_delegate_result_text(
    *,
    role: str,
    child_session_id: str,
    final_state: Any,
) -> str:
    """Build the user-facing summary string returned by ``delegate_task``."""
    if not isinstance(final_state, dict):
        return f"Subagent [{role}] finished with unexpected result type: {type(final_state)}"

    last_result = final_state.get("last_result", {})
    history = final_state.get("history", [])
    task = final_state.get("task", "")
    errors = final_state.get("errors", [])

    if errors:
        error_summary = "\n".join(errors[:5])
        return f"Subagent [{role}] completed with errors:\n{error_summary}"

    summary = "Subagent completed execution."
    for msg in reversed(history):
        if isinstance(msg, dict):
            role_val = msg.get("role")
            content = msg.get("content")
            if role_val == "assistant" and content:
                summary = content
                break

    result_parts = [
        f"## Subagent [{role}] Execution Complete",
        "",
        f"**Task:** {task[:200]}..." if len(task) > 200 else f"**Task:** {task}",
        "",
        f"**Summary:** {summary[:500]}..."
        if len(summary) > 500
        else f"**Summary:** {summary}",
    ]

    if last_result:
        if isinstance(last_result, dict):
            status = last_result.get("status", "unknown")
            result_parts.append(f"**Status:** {status}")
            if last_result.get("file"):
                result_parts.append(f"**File:** {last_result.get('file')}")
            error = last_result.get("error")
            if error:
                result_parts.append(f"**Error:** {error}")
        else:
            result_parts.append(f"**Result:** {str(last_result)[:200]}")

    result_parts.append(f"**child_session_id:** {child_session_id}")
    return "\n".join(result_parts)
