"""Opt-in subagent delegation: read-only ``scout`` & isolated ``worktree`` modes (TASK-33).

CodingAgent follows a single-agent + progressive-disclosure skills architecture.
``delegate_task`` is strictly opt-in (excluded from ``compact-flat``) and supports:
  - ``mode="scout"``: strictly read-only codebase exploration (lethal-trifecta checked).
  - ``mode="worktree"``: isolated temporary git worktree execution for parallel edits.
"""

from __future__ import annotations

import asyncio, concurrent.futures, contextvars as _cv, logging, os, time as _time, uuid as _uuid  # noqa: E401
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from src.core.paths import get_sessions_dir
from src.tools._tool import PermissionKind, tool
from src.tools.subagent_payloads import (
    SCOUT_READONLY_TOOLS, build_delegate_result_text, build_subagent_budget_capsule,
    build_subagent_initial_state, build_subagent_manifest, build_subagent_roles_payload,
    canonicalize_subagent_role, capture_worktree_diff_summary, compute_effective_tool_policy,
    is_git_repo, is_valid_task_id, persist_child_session_file,
    publish_delegation_event, validate_parallel_subtasks,
)

logger = logging.getLogger(__name__)

_PARENT_ORCHESTRATOR_VAR: ContextVar[Any] = ContextVar("_parent_orchestrator", default=None)
_DELEGATION_DEPTH_VAR: ContextVar[int] = ContextVar("_delegation_depth", default=0)
_MAX_DELEGATION_DEPTH: int = 3
_PARALLEL_MAX_SUBTASKS: int = 6
_DELEGATION_TIMEOUT_SECONDS: float = 300.0
_VALID_MODES: frozenset[str] = frozenset({"scout", "worktree"})
_SCOUT_ROLES: frozenset[str] = frozenset({"scout", "analyst", "explore", "researcher", "planner", "strategic"})

DispatchEvent: Any = None
DispatchResultEvent: Any = None
try:
    from src.core.orchestration.event_bus import (
        DispatchEvent,  # type: ignore[assignment]
        DispatchResultEvent,  # type: ignore[assignment]
    )
except ImportError:
    pass

_get_agent_brain_manager: Any = None
try:
    from src.core.orchestration.agent_brain import get_agent_brain_manager as _get_agent_brain_manager  # type: ignore[assignment]
except ImportError:
    pass

try:
    from src.core.orchestration.role_config import (
        CANONICAL_ROLES, ROLE_ALIASES,
        get_default_model_for_role, is_tool_allowed_for_role, normalize_role,
    )
except ImportError:
    CANONICAL_ROLES = ["scout", "analyst", "operational", "strategic", "reviewer", "debugger"]
    ROLE_ALIASES = {"planner": "strategic", "coder": "operational", "researcher": "analyst"}
    normalize_role = lambda role: role  # noqa: E731
    get_default_model_for_role = lambda role: None  # noqa: E731
    is_tool_allowed_for_role = lambda tool_name, role: False  # noqa: E731


def _atomic_write_json(target: Path, obj: dict, logger: Any = None) -> bool:
    """Deferred alias for ``src.core.io_utils.atomic_write_json`` (monkeypatchable)."""
    from src.core.io_utils import atomic_write_json as _central

    return _central(target, obj, logger=logger)


def _get_agent_context_dir(workdir_path: Path) -> Path:
    try:
        from src.tools.tools_config import agent_context_path as _acp

        resolved = _acp(Path(workdir_path))
        if resolved:
            return resolved
    except Exception:
        pass
    return Path(workdir_path) / ".codingAgent"


def get_agent_brain_manager() -> Any:
    if _get_agent_brain_manager is None:
        raise RuntimeError("AgentBrainManager is not available (src.core not importable)")
    return _get_agent_brain_manager()


def _resolve_subagent_graph(*, orchestrator: Any = None, model: Optional[str] = None) -> Any:
    from src.core.orchestration.graph.builder import get_compiled_graph_for_orchestrator

    return get_compiled_graph_for_orchestrator(orchestrator=orchestrator, model=model)


def _build_valid_roles() -> set[str]:
    return set(CANONICAL_ROLES) | set(ROLE_ALIASES.keys())


class _MinimalToolRegistry:
    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {}

    def get(self, name: str) -> Optional[Any]:
        return self.tools.get(name)

    def list(self) -> List[str]:
        return list(self.tools.keys())

    def get_openai_functions(self) -> List[Dict[str, Any]]:
        return []


class SubagentOrchestrator:
    """Minimal orchestrator shim for isolated subagent role enforcement."""

    def __init__(
        self, role: str, working_dir: str,
        allowed_tools: Optional[set[str]] = None, denied_tools: Optional[set[str]] = None,
    ) -> None:
        self.current_role = normalize_role(role)
        self.working_dir = Path(working_dir)
        self.tool_registry = _MinimalToolRegistry()
        self.cancel_event = None
        self.adapter = None
        self._allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        self._denied_tools = set(denied_tools) if denied_tools else set()

    def is_tool_allowed(self, tool_name: str) -> bool:
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            return False
        return tool_name not in self._denied_tools and is_tool_allowed_for_role(tool_name, self.current_role)


def _is_compact_policy(parent_orch: Any) -> bool:
    if parent_orch is None or os.environ.get("CODINGAGENT_ALLOW_COMPACT_DELEGATION") == "1":
        return False
    pol = getattr(parent_orch, "_runtime_policy", None) or getattr(parent_orch, "runtime_policy", None)
    if pol is None:
        return False
    tier = str(pol.get("policy_tier") if isinstance(pol, dict) else getattr(pol, "policy_tier", "")).lower()
    toolset = str(pol.get("toolset_name") if isinstance(pol, dict) else getattr(pol, "toolset_name", "")).lower()
    return tier in ("nano", "compact") or toolset == "compact-flat"


def _publish_event(parent_orch: Any, kind: str, **kw: Any) -> None:
    publish_delegation_event(parent_orch, kind, dispatch_event_cls=DispatchEvent, dispatch_result_cls=DispatchResultEvent, **kw)


def _persist_child_session(
    final_state: Any, workdir_path: Path, cid: str, pid: Optional[str],
    role: str, task: str, ok: bool, error: Optional[str] = None,
) -> None:
    persist_child_session_file(
        final_state=final_state, workdir_path=workdir_path, sessions_dir=get_sessions_dir(),
        writer=_atomic_write_json, logger=logger, cid=cid, pid=pid, role=role,
        task=task, ok=ok, timestamp=_time.time(), error=error,
    )


def _resolve_tool_policy(
    canonical_role: str, effective_mode: str, allowed_tools: Optional[list[str]],
) -> tuple[Optional[set[str]], set[str]]:
    reg_allowed: Optional[set[str]] = set(SCOUT_READONLY_TOOLS) if effective_mode == "scout" else None
    reg_denied: set[str] = set()
    if effective_mode != "scout" and allowed_tools is None:
        try:
            from src.core.orchestration.agent_types import get_agent_registry

            adef = get_agent_registry().get(canonical_role)
            if adef is not None:
                reg_allowed, reg_denied = adef.allowed_tools, adef.denied_tools or set()
        except Exception:
            pass
    eff_allowed, eff_denied = compute_effective_tool_policy(
        explicit_allowed_tools=allowed_tools, registry_allowed_tools=reg_allowed, registry_denied_tools=reg_denied,
    )
    if effective_mode == "scout":
        from src.core.orchestration.pipeline.permissions import assert_lethal_trifecta_isolated

        assert_lethal_trifecta_isolated(eff_allowed or SCOUT_READONLY_TOOLS, role="scout")
    return eff_allowed, eff_denied


@tool(side_effects=["execute"], tags=["planning"], permission_kind=PermissionKind.DELEGATE)
def delegate_task(
    role: str = "scout", subtask_description: str = "", working_dir: Optional[str] = None,
    allowed_tools: Optional[list] = None, model: Optional[str] = None,
    task_id: Optional[str] = None, mode: Optional[str] = None,
) -> str:
    """Spawn an opt-in isolated subagent in ``scout`` (read-only) or ``worktree`` (isolated git branch) mode."""
    if mode is not None and mode not in _VALID_MODES:
        return f"Error: Invalid delegation mode '{mode}'. Expected one of: scout, worktree."
    if role not in _build_valid_roles():
        return f"Error: Invalid role '{role}'. Valid roles: {sorted(_build_valid_roles())}"
    if not subtask_description or not subtask_description.strip():
        return "Error: subtask_description must not be empty."

    parent_orch = _PARENT_ORCHESTRATOR_VAR.get(None)
    if _is_compact_policy(parent_orch):
        return "Error: delegate_task is disabled on compact/nano tiers (compact-flat). Work directly or call load_skill(name)."

    parent_wd_raw = getattr(parent_orch, "working_dir", None)
    workdir_path = Path(working_dir or (str(parent_wd_raw) if parent_wd_raw else ".")).resolve()
    if parent_orch is not None and isinstance(parent_wd_raw, (str, Path)) and parent_wd_raw:
        parent_wd = Path(parent_wd_raw).resolve()
        if not workdir_path.is_relative_to(parent_wd):
            return f"Error: working_dir '{working_dir}' is outside workspace '{parent_wd}'"

    depth = _DELEGATION_DEPTH_VAR.get()
    if depth >= _MAX_DELEGATION_DEPTH:
        return f"Error: Maximum delegation depth ({_MAX_DELEGATION_DEPTH}) exceeded."
    if task_id is not None and not is_valid_task_id(str(task_id)):
        return f"Error: Invalid task_id '{task_id}'. Must match ^[A-Za-z0-9._-]{{1,64}}$ without path separators."

    canonical_role = canonicalize_subagent_role(role)
    effective_mode = mode or ("scout" if canonical_role in _SCOUT_ROLES else "worktree")
    child_session_id = task_id or str(_uuid.uuid4())[:8]
    parent_session_id = getattr(parent_orch, "_current_task_id", None) if parent_orch is not None else None

    try:
        eff_allowed, eff_denied = _resolve_tool_policy(canonical_role, effective_mode, allowed_tools)
    except PermissionError as exc:
        return f"Error: {exc}"

    wt_mgr: Any = None
    wt_task_id: Optional[str] = None
    exec_workdir, agent_token, workdir_token = workdir_path, None, None
    manifest: dict[str, Any] = {}
    manifest_path: Optional[Path] = None

    try:
        if effective_mode == "worktree" and (mode == "worktree" or is_git_repo(workdir_path)):
            from src.core.orchestration.git_worktree_manager import GitWorktreeManager
            from src.core.orchestration.pipeline.permissions import _ACTIVE_WORKDIR_CTX
            wt_mgr, wt_task_id = GitWorktreeManager(workspace=workdir_path), f"wt_{child_session_id}"
            exec_workdir = asyncio.run(wt_mgr.create(task_id=wt_task_id))
            workdir_token = _ACTIVE_WORKDIR_CTX.set(exec_workdir)

        prompt_role = "scout" if effective_mode == "scout" else canonical_role
        base_system_prompt = get_agent_brain_manager().compile_system_prompt(prompt_role)
        budget_capsule = build_subagent_budget_capsule(
            role=prompt_role, mode=effective_mode, max_steps=20, tools_required=(effective_mode == "scout"),
        )
        system_prompt = f"{base_system_prompt}\n\n{budget_capsule}" if base_system_prompt else budget_capsule
        override_model = model if model is not None else get_default_model_for_role(canonical_role)
        sub_orch = SubagentOrchestrator(
            role="scout" if effective_mode == "scout" else role,
            working_dir=str(exec_workdir), allowed_tools=eff_allowed, denied_tools=eff_denied,
        )
        resumed_state: Optional[dict] = None
        if task_id:
            try:
                from src.core.memory.session_store import SessionStore as _SS
                resumed_state = _SS(workdir=str(workdir_path)).load_session_state(task_id)
            except Exception:
                resumed_state = None

        initial_state = build_subagent_initial_state(
            subtask_description=subtask_description, child_session_id=child_session_id,
            working_dir=str(exec_workdir), system_prompt=system_prompt, current_role=sub_orch.current_role,
            parent_session_id=parent_session_id, delegation_depth=depth + 1,
            override_model=override_model, resumed_state=resumed_state,
        )

        active_orch: Any = sub_orch
        graph = None
        if parent_orch is not None:
            try:
                graph = _resolve_subagent_graph(orchestrator=parent_orch, model=override_model)
                if eff_allowed is not None or eff_denied:
                    from src.core.orchestration.agent_types import AgentDefinition
                    from src.core.orchestration.tool_execution_pipeline import _ACTIVE_AGENT_CTX

                    agent_token = _ACTIVE_AGENT_CTX.set(
                        AgentDefinition(id=f"delegated_{canonical_role}", name=f"Delegated {canonical_role}",
                                        description="Delegated subagent context", allowed_tools=eff_allowed, denied_tools=eff_denied)
                    )
                active_orch = parent_orch
            except Exception as exc:
                logger.warning("delegate_task: full pipeline fallback: %s", exc)
                graph = None
        if graph is None:
            graph = _resolve_subagent_graph(orchestrator=sub_orch, model=override_model)
            if graph is None:
                return f"Error: Could not create graph for role '{role}'"

        try:
            mdir = _get_agent_context_dir(workdir_path) / "subagent_manifests"
            mdir.mkdir(parents=True, exist_ok=True)
            manifest = build_subagent_manifest(
                child_session_id=child_session_id, parent_session_id=parent_session_id,
                canonical_role=canonical_role, task=subtask_description,
                working_dir=str(exec_workdir), spawned_at=_time.time(),
            )
            manifest["mode"] = effective_mode
            manifest_path = mdir / f"subagent_{child_session_id}.json"
            if not _atomic_write_json(manifest_path, manifest, logger=logger):
                manifest_path = None
        except Exception:
            manifest_path = None

        _publish_event(parent_orch, "start", cid=child_session_id, pid=parent_session_id, role=canonical_role, task=subtask_description)

        def _run() -> Any:
            from src.core.orchestration.graph.state import AgentState as _AgentState

            return asyncio.run(graph.ainvoke(cast(_AgentState, initial_state), {"configurable": {"orchestrator": active_orch}, "recursion_limit": 50}))

        depth_tok = _DELEGATION_DEPTH_VAR.set(depth + 1)
        try:
            child_ctx = _cv.copy_context()
        finally:
            _DELEGATION_DEPTH_VAR.reset(depth_tok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            final_state = pool.submit(child_ctx.run, _run).result(timeout=_DELEGATION_TIMEOUT_SECONDS)

        if manifest_path is not None:
            manifest.update({"status": "completed", "completed_at": _time.time()})
            _atomic_write_json(manifest_path, manifest, logger=logger)
        _persist_child_session(final_state, workdir_path, child_session_id, parent_session_id, canonical_role, subtask_description, ok=True)
        _publish_event(parent_orch, "finish", cid=child_session_id, pid=parent_session_id, role=canonical_role, final_state=final_state, ok=True)
        result_text = build_delegate_result_text(role=role, child_session_id=child_session_id, final_state=final_state)
        if wt_mgr is not None and wt_task_id is not None:
            wt_summary = capture_worktree_diff_summary(exec_workdir)
            if wt_summary:
                result_text = f"{result_text}\n\n{wt_summary}"
        return result_text
    except Exception as exc:
        if manifest_path is not None:
            manifest.update({"status": "failed", "error": str(exc), "failed_at": _time.time()})
            _atomic_write_json(manifest_path, manifest, logger=logger)
        _persist_child_session(None, workdir_path, child_session_id, parent_session_id, canonical_role, subtask_description, ok=False, error=str(exc))
        _publish_event(parent_orch, "finish", cid=child_session_id, pid=parent_session_id, role=canonical_role, final_state=None, ok=False)
        logger.error("delegate_task: subagent failed: %s", exc)
        return f"Subagent [{role}] failed during execution: {exc}"
    finally:
        for _tok, _ctx_name in ((workdir_token, "_ACTIVE_WORKDIR_CTX"), (agent_token, "_ACTIVE_AGENT_CTX")):
            if _tok is not None:
                try:
                    import src.core.orchestration.pipeline.permissions as _perms
                    getattr(_perms, _ctx_name).reset(_tok)
                except Exception:
                    pass
        if wt_mgr is not None and wt_task_id is not None:
            try:
                asyncio.run(wt_mgr.remove(wt_task_id))
            except Exception:
                pass


@tool(tags=["planning"], permission_kind=PermissionKind.NONE)
def list_subagent_roles() -> Dict[str, Any]:
    """List available delegation modes (``scout`` / ``worktree``) and legacy role aliases."""
    return build_subagent_roles_payload()


async def delegate_task_async(
    role: str, subtask_description: str, working_dir: Optional[str] = None,
    allowed_tools: Optional[list] = None, model: Optional[str] = None,
) -> str:
    ctx = _cv.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(ctx.run, delegate_task, role, subtask_description, working_dir, allowed_tools, model).result(timeout=_DELEGATION_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return f"Error: Delegation to '{role}' subagent timed out after {_DELEGATION_TIMEOUT_SECONDS} seconds."


@tool(side_effects=["execute"], tags=["planning", "subagent"], permission_kind=PermissionKind.DELEGATE)
def delegate_tasks_parallel(
    subtasks: List[Dict[str, Any]], working_dir: Optional[str] = None, model: Optional[str] = None,
) -> Dict[str, Any]:
    """Run up to 6 independent subtasks concurrently in isolated subagents."""
    parent_orch = _PARENT_ORCHESTRATOR_VAR.get(None)
    parent_wd = Path(parent_orch.working_dir).resolve() if (parent_orch is not None and getattr(parent_orch, "working_dir", None)) else None
    validated, err = validate_parallel_subtasks(subtasks, _PARALLEL_MAX_SUBTASKS, working_dir, model, parent_wd)
    if err is not None or validated is None:
        return {"ok": False, "error": err or "Invalid subtasks.", "results": [], "succeeded": 0, "failed": 0}

    def _run_one(task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            out = _cv.copy_context().run(delegate_task, task["role"], task["subtask_description"], task["working_dir"], task["allowed_tools"], task["model"])
            return {"role": task["role"], "subtask_description": task["subtask_description"], "ok": not (isinstance(out, str) and out.startswith("Error:")), "output": out}
        except Exception as exc:
            return {"role": task["role"], "subtask_description": task["subtask_description"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(validated), _PARALLEL_MAX_SUBTASKS)) as pool:
        futs = {pool.submit(_cv.copy_context().run, _run_one, t): t for t in validated}
        for fut in concurrent.futures.as_completed(futs, timeout=_DELEGATION_TIMEOUT_SECONDS + 10):
            try:
                results.append(fut.result(timeout=1))
            except Exception as exc:
                t = futs[fut]
                results.append({"role": t["role"], "subtask_description": t["subtask_description"], "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    succeeded = sum(1 for r in results if r.get("ok"))
    return {"ok": succeeded == len(results), "results": results, "succeeded": succeeded, "failed": len(results) - succeeded}
