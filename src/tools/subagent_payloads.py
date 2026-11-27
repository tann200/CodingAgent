"""Pure helpers for subagent role/mode normalization, events, and persisted payloads (TASK-33)."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DELEGATION_DENYLIST = {
    "delegate",
    "delegate_task",
    "delegate_task_async",
    "delegate_tasks_parallel",
}

SCOUT_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file", "read_tool_output", "list_files", "glob", "grep",
        "search_code", "find_symbol", "find_references",
        "git_status", "git_diff", "git_log", "load_skill", "list_skills",
    }
)

_ROLE_CANONICAL_MAP: Dict[str, str] = {
    "researcher": "analyst", "coder": "operational", "planner": "strategic",
    "reviewer": "reviewer", "analyst": "analyst", "operational": "operational",
    "strategic": "strategic", "debugger": "debugger", "general": "general",
    "generalist": "general", "scout": "scout", "tester": "tester",
}


def canonicalize_subagent_role(role: str) -> str:
    return _ROLE_CANONICAL_MAP.get(role, role)


def compute_effective_tool_policy(
    *,
    explicit_allowed_tools: Optional[Iterable[str]],
    registry_allowed_tools: Optional[Iterable[str]],
    registry_denied_tools: Optional[Iterable[str]],
) -> Tuple[Optional[set[str]], set[str]]:
    allowed: Optional[set[str]] = None
    denied = set(registry_denied_tools or []) | DELEGATION_DENYLIST
    if explicit_allowed_tools is not None:
        allowed = set(explicit_allowed_tools) - DELEGATION_DENYLIST
    elif registry_allowed_tools is not None:
        allowed = set(registry_allowed_tools) - DELEGATION_DENYLIST
    return allowed, denied


def build_subagent_manifest(
    *, child_session_id: str, parent_session_id: Optional[str],
    canonical_role: str, task: str, working_dir: str, spawned_at: float,
) -> Dict[str, Any]:
    return {
        "child_session_id": child_session_id, "parent_session_id": parent_session_id,
        "role": canonical_role, "task": task, "working_dir": working_dir,
        "spawned_at": spawned_at, "status": "running",
    }


def build_subagent_session_payload(
    *, child_session_id: str, parent_session_id: Optional[str], task_name: str,
    canonical_role: str, working_dir: str, timestamp: float, messages: list[dict],
    ok: bool, error: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "version": 1, "session_id": child_session_id, "parent_session_id": parent_session_id,
        "timestamp": timestamp, "task_name": task_name[:80], "role": canonical_role,
        "message_count": len(messages), "messages": messages, "working_dir": working_dir, "ok": ok,
    }
    if error is not None:
        payload["error"] = error
    return payload


def extract_child_session_messages(final_state: Any) -> list[dict[str, Any]]:
    if not isinstance(final_state, dict):
        return []
    history = final_state.get("history") or []
    if history:
        return list(history)
    return [
        {"role": getattr(msg, "type", "unknown"), "content": str(msg.content)}
        for msg in (final_state.get("messages") or [])
        if hasattr(msg, "type") and hasattr(msg, "content")
    ]


def build_child_session_file_path(sessions_dir: str, child_session_id: str) -> str:
    return str(Path(sessions_dir) / f"session_{child_session_id}.json")


def select_dispatch_result_content(final_state: Any) -> str:
    if not isinstance(final_state, dict):
        return ""
    if final_state.get("work_summary"):
        return str(final_state["work_summary"])
    for msg in reversed(final_state.get("history") or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""


def build_subagent_roles_payload() -> Dict[str, Any]:
    roles = {
        "scout": {"description": "Read-only codebase exploration (lethal-trifecta isolated)", "best_for": "Fast file/symbol discovery", "aliases": ["analyst", "researcher"]},
        "analyst": {"description": "Deep research and repository analysis (read-only scout)", "best_for": "Exploring codebase and architecture", "aliases": ["researcher"]},
        "operational": {"description": "Code implementation and isolated worktree execution", "best_for": "Implementing features in isolated worktrees", "aliases": ["coder"]},
        "strategic": {"description": "Task decomposition and planning", "best_for": "Breaking down complex tasks", "aliases": ["planner"]},
        "reviewer": {"description": "Code review and verification", "best_for": "Reviewing patches and running checks", "aliases": []},
        "debugger": {"description": "Root-cause analysis and bug fixing", "best_for": "Diagnosing failures and minimal fixes", "aliases": []},
        "tester": {"description": "Test creation and execution", "best_for": "Writing and running tests", "aliases": []},
    }
    return {
        "status": "ok",
        "modes": {
            "scout": "Strictly read-only exploration (lethal-trifecta checked; excluded from compact-flat).",
            "worktree": "Isolated temporary git worktree execution for parallel edits.",
        },
        "available_roles": roles,
        "note": "Prefer load_skill(name) inside the single agent loop; use delegate_task(mode='scout'|'worktree') only when isolation is needed.",
    }


def build_graph_state_base(
    *, task: str, session_id: str, working_dir: str, system_prompt: str,
    history: Optional[list[dict]] = None, verified_reads: Optional[list[Any]] = None,
    errors: Optional[list[Any]] = None, current_plan: Optional[list[Any]] = None,
    current_step: int = 0, parent_session_id: Optional[str] = None,
    delegation_depth: int = 0, override_model: Optional[str] = None, current_role: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "task": task, "session_id": session_id, "working_dir": working_dir,
        "history": list(history or []), "system_prompt": system_prompt,
        "rounds": 0, "errors": list(errors or []), "verified_reads": list(verified_reads or []),
        "next_action": None, "current_plan": list(current_plan or []), "current_step": current_step,
        "last_result": None, "plan_validation": None, "verification_result": None,
        "evaluation_result": None, "delegations": [], "delegation_results": None,
        "parent_session_id": parent_session_id, "delegation_depth": delegation_depth,
        "override_model": override_model, "current_role": current_role,
    }


def build_subagent_budget_capsule(
    *,
    role: str,
    mode: str,
    max_steps: int = 20,
    tools_required: bool = False,
) -> str:
    """Return a concise budget & grounding capsule appended to delegated subagent prompts."""
    lines = [
        f"<subagent_budget role=\"{role}\" mode=\"{mode}\" max_steps=\"{max_steps}\">",
        f"- You have a bounded budget of ~{max_steps} turns. Focus directly on the assigned subtask.",
        "- Before exhausting your budget, synthesize your verified findings and explicitly list any uncovered gaps.",
    ]
    if tools_required:
        lines.append(
            "- Tool evidence is mandatory: inspect the workspace with read/search tools before answering; do not guess from memory."
        )
    lines.append("</subagent_budget>")
    return "\n".join(lines)


def build_subagent_initial_state(
    *, subtask_description: str, child_session_id: str, working_dir: str, system_prompt: str,
    current_role: str, parent_session_id: Optional[str], delegation_depth: int,
    override_model: Optional[str], resumed_state: Optional[dict] = None,
) -> Dict[str, Any]:
    state = build_graph_state_base(
        task=subtask_description, session_id=child_session_id, working_dir=working_dir,
        system_prompt=system_prompt, parent_session_id=parent_session_id,
        delegation_depth=delegation_depth, override_model=override_model, current_role=current_role,
    )
    if resumed_state:
        for key in ("history", "current_plan", "current_step", "verified_reads", "files_read", "affected_files", "model_tier"):
            if resumed_state.get(key) is not None:
                state[key] = deepcopy(resumed_state[key])
        state.update({"task": subtask_description, "session_id": child_session_id, "delegation_depth": delegation_depth})
    return state


def build_delegate_result_text(*, role: str, child_session_id: str, final_state: Any) -> str:
    if not isinstance(final_state, dict):
        return f"Subagent [{role}] finished with unexpected result type: {type(final_state)}"
    errors = final_state.get("errors", [])
    if errors:
        return f"Subagent [{role}] completed with errors:\n" + "\n".join(str(e) for e in errors[:5])
    task = str(final_state.get("task", ""))
    summary = select_dispatch_result_content(final_state) or "Subagent completed execution."
    parts = [
        f"## Subagent [{role}] Execution Complete", "",
        f"**Task:** {task[:200]}..." if len(task) > 200 else f"**Task:** {task}", "",
        f"**Summary:** {summary[:500]}..." if len(summary) > 500 else f"**Summary:** {summary}",
    ]
    last_result = final_state.get("last_result")
    if isinstance(last_result, dict) and last_result:
        parts.append(f"**Status:** {last_result.get('status', 'unknown')}")
        if last_result.get("file"):
            parts.append(f"**File:** {last_result['file']}")
        if last_result.get("error"):
            parts.append(f"**Error:** {last_result['error']}")
    parts.append(f"**child_session_id:** {child_session_id}")
    return "\n".join(parts)


def validate_parallel_subtasks(
    subtasks: Any, max_subtasks: int, working_dir: Optional[str], model: Optional[str], parent_wd: Optional[Path],
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(subtasks, list) or not subtasks:
        return None, "subtasks must be a non-empty list of dicts."
    if len(subtasks) > max_subtasks:
        return None, f"Too many subtasks: {len(subtasks)} requested, max is {max_subtasks}."
    validated: List[Dict[str, Any]] = []
    for i, task in enumerate(subtasks):
        if not isinstance(task, dict):
            return None, f"subtask[{i}] must be a dict, got {type(task).__name__}."
        role, desc = task.get("role"), task.get("subtask_description")
        if not role or not isinstance(role, str):
            return None, f"subtask[{i}] missing required string key 'role'."
        if not desc or not isinstance(desc, str):
            return None, f"subtask[{i}] missing required string key 'subtask_description'."
        twd = task.get("working_dir") or working_dir
        if parent_wd is not None and twd and not Path(twd).resolve().is_relative_to(parent_wd):
            return None, f"subtask[{i}] working_dir '{twd}' is outside workspace."
        validated.append({"role": role, "subtask_description": desc, "working_dir": twd, "allowed_tools": task.get("allowed_tools"), "model": task.get("model") or model})
    return validated, None


def publish_delegation_event(
    parent_orch: Any, kind: str, *, dispatch_event_cls: Any, dispatch_result_cls: Any, **kw: Any,
) -> None:
    try:
        pbus = getattr(parent_orch, "event_bus", None) if parent_orch is not None else None
        if pbus is None:
            return
        if kind == "start":
            pbus.publish("delegation.start", {"child_session_id": kw["cid"], "parent_session_id": kw["pid"], "role": kw["role"], "task": kw["task"][:120]})
            if dispatch_event_cls is not None and hasattr(pbus, "publish_dispatch"):
                pbus.publish_dispatch(dispatch_event_cls(session_id=kw["cid"], agent_id=kw["role"], task=kw["task"], parent_session_id=kw["pid"]))
        else:
            fs = kw.get("final_state")
            cost = float(fs.get("session_cost_usd") or 0.0) if isinstance(fs, dict) else 0.0
            pbus.publish("delegation.finish", {"child_session_id": kw["cid"], "role": kw["role"], "ok": kw["ok"], "cost_usd": cost if kw["ok"] else None})
            if kw["ok"] and dispatch_result_cls is not None and hasattr(pbus, "publish_dispatch_result"):
                pbus.publish_dispatch_result(dispatch_result_cls(session_id=kw["cid"], content=select_dispatch_result_content(fs), parent_session_id=kw["pid"]))
            if kw["ok"] and cost > 0:
                pbus.publish("usage.subagent_cost", {"child_session_id": kw["cid"], "role": kw["role"], "cost_usd": cost})
    except Exception:
        pass


def persist_child_session_file(
    *, final_state: Any, workdir_path: Path, sessions_dir: Path, writer: Any, logger: Any,
    cid: str, pid: Optional[str], role: str, task: str, ok: bool, timestamp: float, error: Optional[str] = None,
) -> None:
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = build_subagent_session_payload(
            child_session_id=cid, parent_session_id=pid, task_name=task,
            canonical_role=role, working_dir=str(workdir_path), timestamp=timestamp,
            messages=extract_child_session_messages(final_state) if ok else [], ok=ok, error=error,
        )
        writer(Path(build_child_session_file_path(str(sessions_dir), cid)), payload, logger=logger)
    except Exception:
        pass
