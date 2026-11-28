"""Tool execution pipeline — Stage 2: Permissions & policy guards.

Owns plan-mode, explore-mode, permission-mode, workdir confinement,
per-agent permission overrides, and interactive/autonomous approval gates.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Collection, Dict, Optional

from src.core.messaging.event_types import (
    SpawnPermissionRequired,
    ToolPermissionRequired,
)
from src.core.orchestration.tool_constants import (
    PERM_ORDER as _PERM_ORDER,
    PERMISSION_REQUIRED_TOOLS,
    _write_permission_audit,
)

logger = logging.getLogger("src.core.orchestration.tool_execution_pipeline")

try:
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
except Exception:
    guilogger = logger  # type: ignore[assignment]

try:
    from src.core.orchestration.approval_gate import (
        discard_tool_denied,
        is_tool_denied,
        register_tool_gate,
    )
except Exception:

    def register_tool_gate(tool_id: str) -> Any:  # type: ignore[misc]
        return None

    def is_tool_denied(tool_id: str) -> bool:  # type: ignore[misc]
        return False

    def discard_tool_denied(tool_id: str) -> None:  # type: ignore[misc]
        pass


APPROVAL_TIMEOUT_SECONDS: float = 120.0

_ACTIVE_AGENT_CTX: ContextVar[Optional[Any]] = ContextVar(
    "_ACTIVE_AGENT_CTX", default=None
)


def _check_plan_mode_guard(orch: Any, name: str) -> Optional[Dict[str, Any]]:
    """Block write tools when plan mode is active and not yet approved."""
    try:
        from src.core.orchestration.plan_mode import PlanMode

        plan_mode = getattr(orch, "plan_mode", None)
        if (
            plan_mode
            and getattr(plan_mode, "enabled", False)
            and name in PlanMode.BLOCKED_TOOLS
        ):
            if getattr(orch, "_plan_mode_approved", None) is not True:
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' is blocked: the current plan has not been "
                        "approved yet. Await user approval before making file changes."
                    ),
                }
    except Exception as exc:
        logger.error("plan mode security check failed for %r: %s", name, exc)
        return {
            "ok": False,
            "error": f"Security check (plan-mode) failed closed: {exc}",
        }
    return None


def _check_explore_mode_guard(orch: Any, name: str) -> Optional[Dict[str, Any]]:
    """Block non-read-only tools when explore mode is active."""
    if not getattr(orch, "explore_mode", False):
        return None

    try:
        from src.core.orchestration.role_config import is_tool_allowed_for_role

        if not is_tool_allowed_for_role(name, "analyst"):
            return {
                "ok": False,
                "error": (
                    f"Explore mode is active: tool '{name}' is not permitted. "
                    "Only read-only exploration tools (read_file, glob, grep, "
                    "find_symbol, bash, etc.) are allowed in explore mode."
                ),
            }
    except Exception as exc:
        logger.error("explore mode security check failed for %r: %s", name, exc)
        return {
            "ok": False,
            "error": f"Security check (explore-mode) failed closed: {exc}",
        }
    return None


def _check_permission_mode_guard(orch: Any, name: str) -> Optional[Dict[str, Any]]:
    """Block tools that exceed the active permission mode."""
    try:
        from src.tools.tools_config import (
            get_active_permission_mode,
            get_tool_permission,
        )

        active_mode = get_active_permission_mode()
        if active_mode is not None:
            tool_perm = get_tool_permission(name)
            active_val = getattr(active_mode, "value", str(active_mode))
            tool_val = getattr(tool_perm, "value", str(tool_perm))
            if _PERM_ORDER.get(tool_val, 4) > _PERM_ORDER.get(active_val, 4):
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' requires '{tool_val}' permission "
                        f"but active permission mode is '{active_val}'."
                    ),
                }
    except Exception as exc:
        logger.error("permission mode security check failed for %r: %s", name, exc)
        return {
            "ok": False,
            "error": f"Security check (permission-mode) failed closed: {exc}",
        }
    return None


def _check_workdir_confinement(
    orch: Any,
    name: str,
    args: dict,
    *,
    permission_required_tools: Optional[Collection[str]] = None,
) -> bool:
    """Return True when the tool call requires explicit approval."""
    if name == "ask_user":
        return False

    req_tools = (
        PERMISSION_REQUIRED_TOOLS
        if permission_required_tools is None
        else permission_required_tools
    )
    needs_gate = name in req_tools
    try:
        from src.tools.tools_config import PermissionLevel, get_tool_permission

        if get_tool_permission(name) in (PermissionLevel.DANGER, PermissionLevel.PROMPT):
            needs_gate = True
    except Exception:
        pass

    workdir = getattr(orch, "working_dir", None)
    if workdir is None:
        return needs_gate

    try:
        workdir_path = Path(workdir).resolve()
    except Exception:
        return needs_gate

    def _inside(path_value: Any) -> bool:
        try:
            resolved = Path(path_value).resolve()
            return resolved == workdir_path or str(resolved).startswith(
                str(workdir_path) + "/"
            )
        except Exception:
            return False

    for wd_key in ("workdir", "working_dir"):
        if wd_key in args and args[wd_key] and not _inside(args[wd_key]):
            return True

    if name == "delete_file":
        return True

    return needs_gate


def _check_agent_permission_override(
    orch: Any,
    name: str,
    args: dict,
) -> Optional[Dict[str, Any]]:
    """Apply global + per-agent permission rules; returns error dict if denied."""
    try:
        active_agent = _ACTIVE_AGENT_CTX.get()
        if active_agent is None:
            active_agent = getattr(orch, "active_agent", None)
        global_policy = getattr(orch, "permission_policy", None)
        if global_policy is None:
            try:
                from src.core.orchestration.permission_policy import (
                    get_permission_policy,
                )

                global_policy = get_permission_policy()
            except Exception as exc:
                guilogger.warning(
                    "tool_execution_pipeline: permission policy unavailable "
                    "(continuing without global rules): %s",
                    exc,
                )
                global_policy = None
        merged_policy = (
            active_agent.get_merged_policy(global_policy)
            if active_agent is not None
            else global_policy
        )
        if merged_policy is not None and merged_policy.is_denied(name):
            _write_permission_audit(
                orch.working_dir, name, args, "deny", "agent_permission_rules"
            )
            return {
                "ok": False,
                "error": (
                    f"Tool '{name}' is denied by permission rules "
                    "(permissions.json / agent overrides)."
                ),
            }
    except Exception as exc:
        guilogger.error(
            "tool_execution_pipeline: _check_agent_permission_override failed (fail-closed): %s",
            exc,
        )
        return {
            "ok": False,
            "error": f"Tool '{name}' denied by agent permission check error.",
        }
    return None


def _run_permission_gate(
    orch: Any,
    name: str,
    args: dict,
    tool_call_id: str,
    needs_gate: bool,
) -> Optional[Dict[str, Any]]:
    """Run the approval gate when needed, reconciling with PermissionPolicy."""
    matched_rule = None
    try:
        from src.core.orchestration.permission_policy import (
            Behavior,
            get_permission_policy,
        )

        matched_rule = get_permission_policy().matching_rule(name)
    except Exception as pol_exc:
        guilogger.error(f"PermissionPolicy evaluation failed closed for '{name}': {pol_exc}")
        return {
            "ok": False,
            "error": f"Permission policy check failed closed for '{name}'.",
        }

    if matched_rule is not None and matched_rule.behavior == Behavior.DENY:
        return {
            "ok": False,
            "error": f"Tool '{name}' is denied by a permissions.json rule.",
        }

    if matched_rule is not None and matched_rule.behavior == Behavior.ASK:
        needs_gate = True

    if not needs_gate:
        return None

    autonomous = False
    try:
        from src.tools.tools_config import is_autonomous

        autonomous = is_autonomous()
    except Exception:
        pass

    if autonomous:
        try:
            from src.tools.tools_config import autonomous_approval_allowed

            if autonomous_approval_allowed(name):
                return None
        except Exception:
            pass
        return {
            "ok": False,
            "error": (
                f"Tool '{name}' requires approval and is not in the autonomous "
                "approval allowlist. Add it via set_autonomous_approve() or the "
                "CODINGAGENT_AUTONOMOUS_APPROVE env var to auto-approve it."
            ),
        }

    if (
        matched_rule is not None
        and matched_rule.behavior == Behavior.ALLOW
        and name != "delete_file"
    ):
        return None

    _pipe_mod = sys.modules.get("src.core.orchestration.tool_execution_pipeline")
    _reg_gate = getattr(_pipe_mod, "register_tool_gate", register_tool_gate)
    _is_denied = getattr(_pipe_mod, "is_tool_denied", is_tool_denied)
    _discard_denied = getattr(_pipe_mod, "discard_tool_denied", discard_tool_denied)
    _timeout = getattr(_pipe_mod, "APPROVAL_TIMEOUT_SECONDS", APPROVAL_TIMEOUT_SECONDS)

    gate_event = None
    try:
        gate_event = _reg_gate(tool_call_id)
        if name == "delegate_task":
            orch.event_bus.publish_typed(
                SpawnPermissionRequired(
                    tool=name,
                    role=args.get("role", ""),
                    task=str(args.get("subtask_description", ""))[:200],
                    tool_id=tool_call_id,
                )
            )
        else:
            orch.event_bus.publish_typed(
                ToolPermissionRequired(
                    tool=name,
                    args={k: str(v)[:200] for k, v in args.items() if k != "content"},
                    tool_id=tool_call_id,
                )
            )
    except Exception as perm_exc:
        guilogger.error(f"Permission gate error for '{name}' (fail-closed): {perm_exc}")
        return {"ok": False, "error": f"Permission gate unavailable for '{name}'."}

    granted = False
    try:
        if gate_event is not None and hasattr(gate_event, "wait"):
            granted = bool(gate_event.wait(timeout=_timeout))
        else:
            guilogger.error(f"Permission gate event unavailable for '{name}' (fail-closed)")
            granted = False
    except Exception:
        granted = False

    denied = True
    try:
        denied = bool(_is_denied(tool_call_id))
    except Exception:
        denied = True

    try:
        _discard_denied(tool_call_id)
    except Exception:
        pass

    if not granted or denied:
        return {"ok": False, "error": f"Tool '{name}' was denied by the user."}
    return None


_LETHAL_TRIFECTA_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "multiedit",
        "apply_patch",
        "delete_file",
        "rename_file",
        "create_directory",
        "bash",
        "run_tests",
        "git_commit",
        "delegate_task",
    }
)
_READONLY_INGESTION_ROLES: frozenset[str] = frozenset(
    {"scout", "analyst", "explore", "researcher", "planner", "strategic"}
)


def assert_lethal_trifecta_isolated(
    tool_names: Collection[str],
    *,
    role: str = "",
    explore_mode: bool = False,
) -> None:
    """Fail closed if a read-only / untrusted-ingestion role admits any mutating tool (TASK-31)."""
    norm_role = (role or "").strip().lower()
    if not explore_mode and norm_role not in _READONLY_INGESTION_ROLES:
        return
    admitted_mutating = sorted(set(tool_names) & _LETHAL_TRIFECTA_MUTATING_TOOLS)
    if admitted_mutating:
        raise PermissionError(
            f"Lethal-trifecta isolation violation: read-only role '{norm_role or 'explore'}' "
            f"cannot admit mutating tools {admitted_mutating}."
        )
