"""permission_gateway.py — ARCH-1: Extracted permission-checking logic.

Owns the 5-gate pre-execution permission check that was previously inlined in
``Orchestrator.execute_tool()``.  ``Orchestrator`` can delegate to
``PermissionGateway.check()`` as a thin delegator.

Gates (in order):
  1. Plan-mode write gate — tool blocked until plan is approved
  2. Explore-mode guard — only analyst read-only tools permitted
  3. PermissionLevel gate — DANGER/PROMPT tools require explicit user approval
  4. Active permission-mode enforcement — block tools above active mode threshold
  5. User-approval gate — prompt (or skip if autonomous)

Interface::

    gw = PermissionGateway(orchestrator)
    result = gw.check(name, args)
    if result.blocked:
        return result.rejection

The gateway is deliberately stateless beyond the orchestrator reference it holds,
so it can be constructed cheaply and replaced in tests via dependency injection.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# CODE_QUALITY_AUDIT #7 fix: promote all deferred imports (previously inside
# individual _gate* methods) to module level.  These were on the hot path for
# every tool call; repeated re-imports add unnecessary overhead.
# Each import is wrapped in a try/except fallback so the gateway degrades
# gracefully when optional modules are absent (e.g. in minimal test environments).
try:
    from src.core.orchestration.plan_mode import PlanMode as _PlanMode
except Exception:
    _PlanMode = None  # type: ignore[assignment]

try:
    from src.core.orchestration.role_config import (
        is_tool_allowed_for_role as _is_tool_allowed_for_role,
    )
except Exception:
    _is_tool_allowed_for_role = None  # type: ignore[assignment]

try:
    from src.core.orchestration.tool_constants import (
        PERMISSION_REQUIRED_TOOLS as _PERMISSION_REQUIRED_TOOLS,
    )
except Exception:
    _PERMISSION_REQUIRED_TOOLS = set()  # type: ignore[assignment]

try:
    from src.tools.tools_config import (
        get_tool_permission as _get_tool_permission,
        PermissionLevel as _PermissionLevel,
        is_autonomous as _is_autonomous,
        get_active_permission_mode as _get_active_permission_mode,
    )
except Exception:
    _get_tool_permission = None  # type: ignore[assignment]
    _PermissionLevel = None  # type: ignore[assignment]
    _is_autonomous = None  # type: ignore[assignment]
    _get_active_permission_mode = None  # type: ignore[assignment]

try:
    from src.core.orchestration.approval_gate import (
        register_tool_gate as _register_tool_gate,
        _tool_denied as _tool_denied_map,
    )
except Exception:
    _register_tool_gate = None  # type: ignore[assignment]
    _tool_denied_map = None  # type: ignore[assignment]

try:
    from src.core.orchestration.permission_policy import (
        get_permission_policy as _get_permission_policy,
        PermissionPolicy as _PermissionPolicy,
        Behavior as _Behavior,
    )
except Exception:
    _get_permission_policy = None  # type: ignore[assignment]
    _PermissionPolicy = None  # type: ignore[assignment]
    _Behavior = None  # type: ignore[assignment]

try:
    from src.tools.permission_context import get_permission_context as _get_permission_context
except Exception:
    _get_permission_context = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Ordering of permission levels from least to most permissive.
# Used by _gate4_permission_mode to compare tool vs. active-mode ranks.
_PERM_ORDER: dict[str, int] = {
    "read_only": 0,
    "workspace_write": 1,
    "danger": 2,
    "prompt": 3,
    "allow": 4,
}

# Tools that are auto-approved when every path/workdir arg is inside the
# project working directory.  ask_user is always auto-approved regardless
# of path because it produces no filesystem side-effects.
_WORKDIR_SAFE_TOOLS: frozenset[str] = frozenset(
    {"bash", "run_tests", "delete_file", "run_bash", "ask_user"}
)


def _path_inside(path_str: str, workdir: "Path") -> bool:  # type: ignore[name-defined]
    """Return True when *path_str* resolves to a location inside *workdir*."""
    from pathlib import Path as _Path

    try:
        resolved = _Path(path_str).resolve()
        wd = _Path(workdir).resolve()
        return resolved == wd or str(resolved).startswith(str(wd) + "/")
    except Exception:
        return False


def _is_workdir_confined(name: str, args: "Dict[str, Any]", workdir: "Any") -> bool:  # type: ignore[name-defined]
    """Return True when *name* with *args* operates only within *workdir*.

    ask_user is always considered confined (no filesystem side-effects).
    For bash / run_tests the ``workdir`` arg is checked.
    For delete_file the ``path`` arg is checked.
    """
    if name == "ask_user":
        return True
    if workdir is None:
        return False
    wd_str = str(workdir)
    if name in ("bash", "run_tests", "run_bash"):
        cmd_workdir = args.get("workdir", wd_str)
        return _path_inside(str(cmd_workdir), workdir)
    if name == "delete_file":
        path_arg = args.get("path", "")
        if not path_arg:
            return False
        return _path_inside(str(path_arg), workdir)
    return False


@dataclass
class PermissionResult:
    """Outcome of the permission check."""

    allowed: bool
    """True when the tool call may proceed."""

    gate: int = 0
    """Which gate blocked the call (1–5).  0 when allowed."""

    reason: str = ""
    """Human-readable reason for denial."""

    rejection: Optional[Dict[str, Any]] = field(default=None)
    """Ready-made rejection dict to return from execute_tool when blocked."""

    @property
    def blocked(self) -> bool:
        return not self.allowed


class PermissionGateway:
    """Thin facade for the 5-gate permission check.

    Parameters
    ----------
    orchestrator:
        The ``Orchestrator`` instance that owns this gateway.  The gateway reads
        ``plan_mode``, ``explore_mode``, ``_plan_mode_approved``, ``event_bus``,
        etc. from it via ``getattr`` so there is no hard circular import.
    """

    def __init__(self, orchestrator: Any) -> None:
        self._orch = orchestrator

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, name: str, args: Dict[str, Any]) -> PermissionResult:
        """Run all 5 gates for *name* / *args*.  Returns on first denial."""
        result = self._gate1_plan_mode(name)
        if result.blocked:
            return result

        result = self._gate2_explore_mode(name)
        if result.blocked:
            return result

        # TASK-4: Gate 2b — PermissionPolicy rule evaluation (DENY/ASK/ALLOW)
        result = self._gate2b_policy_rules(name, args)
        if result.blocked:
            return result

        needs_gate, result = self._gate3_permission_level(name, args)
        if result.blocked:
            return result

        result = self._gate4_permission_mode(name)
        if result.blocked:
            return result

        if needs_gate:
            result = self._gate5_user_approval(name, args)
            if result.blocked:
                return result

        return PermissionResult(allowed=True)

    # ------------------------------------------------------------------
    # Individual gates
    # ------------------------------------------------------------------

    def _gate1_plan_mode(self, name: str) -> PermissionResult:
        """Gate 1: Block write tools when the plan hasn't been approved yet."""
        try:
            _pm = getattr(self._orch, "plan_mode", None)
            if (
                _pm
                and getattr(_pm, "enabled", False)
                and _PlanMode is not None
                and name in _PlanMode.BLOCKED_TOOLS
            ):
                _approved = getattr(self._orch, "_plan_mode_approved", None)
                if _approved is not True:
                    return PermissionResult(
                        allowed=False,
                        gate=1,
                        reason="plan not approved",
                        rejection={
                            "ok": False,
                            "error": (
                                f"Tool '{name}' is blocked: the current plan has not been "
                                "approved yet. Await user approval before making file changes."
                            ),
                        },
                    )
        except Exception:
            pass
        return PermissionResult(allowed=True)

    def _gate2_explore_mode(self, name: str) -> PermissionResult:
        """Gate 2: Explore mode — only analyst read-only tools permitted."""
        if getattr(self._orch, "explore_mode", False):
            try:
                if (
                    _is_tool_allowed_for_role is not None
                    and not _is_tool_allowed_for_role(name, "analyst")
                ):
                    return PermissionResult(
                        allowed=False,
                        gate=2,
                        reason="explore mode active",
                        rejection={
                            "ok": False,
                            "error": (
                                f"Explore mode is active: tool '{name}' is not permitted. "
                                "Only read-only exploration tools (read_file, glob, grep, "
                                "find_symbol, bash, etc.) are allowed in explore mode."
                            ),
                        },
                    )
            except Exception:
                pass
        return PermissionResult(allowed=True)

    def _gate2b_policy_rules(self, name: str, args: Dict[str, Any]) -> PermissionResult:
        """Gate 2b: Evaluate user/project PermissionPolicy rules (TASK-4).

        Loads the user-level singleton policy (``~/.coding_agent/permissions.json``)
        and, when a project working directory is set, merges it with the project-level
        policy (``.agent-context/permissions.json``).  Project rules are appended
        last so they win under last-matching-wins semantics.

        DENY  → return blocked PermissionResult immediately.
        ASK   → delegate to ``_gate5_user_approval()`` (reuses TUI event flow).
        ALLOW → return allowed, continue to Gate 3.

        Exceptions are caught and silently ignored so a broken or absent policy
        file never prevents a tool from running.
        """
        if _get_permission_policy is None or _Behavior is None:
            return PermissionResult(allowed=True)

        try:
            policy = _get_permission_policy()

            # Merge with project-level permissions.json when present.
            _wd = getattr(self._orch, "working_dir", None)
            if _wd is not None and _PermissionPolicy is not None:
                try:
                    from pathlib import Path as _Path
                    _proj_path = _Path(_wd) / ".agent-context" / "permissions.json"
                    if _proj_path.exists():
                        _proj_policy = _PermissionPolicy.load(_proj_path)
                        if len(_proj_policy) > 0:
                            # Project rules appended last so they take precedence.
                            merged_rules = list(policy) + list(_proj_policy)
                            policy = _PermissionPolicy(
                                rules=merged_rules,
                                default_behavior=policy._default,
                            )
                except Exception:
                    pass

            # Gather CLI context (may be None if not configured).
            cli_ctx = None
            if _get_permission_context is not None:
                try:
                    cli_ctx = _get_permission_context()
                except Exception:
                    pass

            behavior = policy.combined_check(name, cli_context=cli_ctx)

            if behavior == _Behavior.DENY:
                return PermissionResult(
                    allowed=False,
                    gate=2,
                    reason=f"PermissionPolicy denied tool '{name}'",
                    rejection={
                        "ok": False,
                        "error": (
                            f"Tool '{name}' is blocked by a permission policy rule. "
                            "Edit ~/.coding_agent/permissions.json or "
                            ".agent-context/permissions.json to change this."
                        ),
                    },
                )

            if behavior == _Behavior.ASK:
                # Reuse Gate 5's interactive TUI-event flow.
                return self._gate5_user_approval(name, args)

        except Exception:
            pass  # policy failures must never block tool execution

        return PermissionResult(allowed=True)

    def _gate3_permission_level(
        self, name: str, args: "Dict[str, Any] | None" = None
    ) -> tuple[bool, PermissionResult]:
        """Gate 3: PermissionLevel check.  Returns (needs_gate, result).

        Tools in _WORKDIR_SAFE_TOOLS are auto-approved when every path/workdir
        argument they receive is inside the project working directory (or when
        they have no filesystem side-effects, like ask_user).
        """
        if args is None:
            args = {}

        # Fast-path: if the tool is workdir-confined, skip the approval gate.
        if name in _WORKDIR_SAFE_TOOLS:
            _orch_wd = getattr(self._orch, "working_dir", None)
            if _is_workdir_confined(name, args, _orch_wd):
                return False, PermissionResult(allowed=True)

        needs_gate = name in _PERMISSION_REQUIRED_TOOLS
        if not needs_gate:
            try:
                if _get_tool_permission is not None and _PermissionLevel is not None:
                    _perm = _get_tool_permission(name)
                    if _perm in (_PermissionLevel.DANGER, _PermissionLevel.PROMPT):
                        needs_gate = True
            except Exception:
                pass
        return needs_gate, PermissionResult(allowed=True)

    def _gate4_permission_mode(self, name: str) -> PermissionResult:
        """Gate 4: Active permission-mode threshold enforcement."""
        try:
            if (
                _get_active_permission_mode is not None
                and _get_tool_permission is not None
            ):
                _active_mode = _get_active_permission_mode()
                if _active_mode is not None:
                    _tool_perm = _get_tool_permission(name)
                    _active_rank = _PERM_ORDER.get(_active_mode.value, 99)
                    _tool_rank = _PERM_ORDER.get(_tool_perm.value, 99)
                    if _tool_rank > _active_rank:
                        return PermissionResult(
                            allowed=False,
                            gate=4,
                            reason=f"permission mode {_active_mode.value} blocks {_tool_perm.value}",
                            rejection={
                                "ok": False,
                                "error": (
                                    f"Tool '{name}' requires '{_tool_perm.value}' permission "
                                    f"but active permission mode is '{_active_mode.value}'."
                                ),
                            },
                        )
        except Exception:
            pass
        return PermissionResult(allowed=True)

    def _gate5_user_approval(self, name: str, args: Dict[str, Any]) -> PermissionResult:
        """Gate 5: Interactive user-approval prompt (skipped in autonomous mode)."""
        _autonomous = False
        try:
            if _is_autonomous is not None:
                _autonomous = _is_autonomous()
        except Exception:
            pass

        if _autonomous:
            return PermissionResult(allowed=True)

        try:
            if _register_tool_gate is not None and _tool_denied_map is not None:
                _t5_id = f"{uuid.uuid4().hex[:8]}"
                _t5_ev = _register_tool_gate(_t5_id)
                # SPAWN-W5: Fire spawn event for delegate_task
                if name == "delegate_task":
                    try:
                        self._orch.event_bus.publish(
                            "spawn.permission_required",
                            {
                                "tool": name,
                                "role": args.get("role", ""),
                                "task": str(args.get("subtask_description", ""))[:200],
                                "tool_id": _t5_id,
                            },
                        )
                    except Exception:
                        pass
                _orch_bus = getattr(self._orch, "event_bus", None)
                if _orch_bus:
                    _orch_bus.publish(
                        "tool.permission_required",
                        {
                            "tool_id": _t5_id,
                            "tool": name,
                            "args": {
                                k: str(v)[:200]
                                for k, v in args.items()
                                if k != "content"
                            },
                        },
                    )
                # Wait for approval — mirrors orchestrator.execute_tool gate logic exactly.
                # AsyncGate.wait() is safe from any thread (uses run_coroutine_threadsafe
                # internally when a loop is available, threading.Event otherwise).
                # Never skip this wait: doing so would silently grant approval.
                granted = _t5_ev.wait(timeout=120.0)
                if not granted or _t5_id in _tool_denied_map:
                    _tool_denied_map.discard(_t5_id)
                    return PermissionResult(
                        allowed=False,
                        gate=5,
                        reason="user denied approval",
                        rejection={
                            "ok": False,
                            "error": f"Tool '{name}' was denied by the user.",
                        },
                    )
        except Exception:
            pass  # gate failures must never block tool execution
        return PermissionResult(allowed=True)
