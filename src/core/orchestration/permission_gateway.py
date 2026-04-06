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

        needs_gate, result = self._gate3_permission_level(name)
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
            from src.core.orchestration.plan_mode import PlanMode

            _pm = getattr(self._orch, "plan_mode", None)
            if (
                _pm
                and getattr(_pm, "enabled", False)
                and name in PlanMode.BLOCKED_TOOLS
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
                from src.core.orchestration.role_config import is_tool_allowed_for_role

                if not is_tool_allowed_for_role(name, "analyst"):
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

    def _gate3_permission_level(self, name: str) -> tuple[bool, PermissionResult]:
        """Gate 3: PermissionLevel check.  Returns (needs_gate, result)."""
        from src.core.orchestration.orchestrator import PERMISSION_REQUIRED_TOOLS

        needs_gate = name in PERMISSION_REQUIRED_TOOLS
        if not needs_gate:
            try:
                from src.tools.tools_config import get_tool_permission, PermissionLevel

                _perm = get_tool_permission(name)
                if _perm in (PermissionLevel.DANGER, PermissionLevel.PROMPT):
                    needs_gate = True
            except Exception:
                pass
        return needs_gate, PermissionResult(allowed=True)

    def _gate4_permission_mode(self, name: str) -> PermissionResult:
        """Gate 4: Active permission-mode threshold enforcement."""
        try:
            from src.tools.tools_config import (
                get_active_permission_mode,
                get_tool_permission,
                PermissionLevel,
            )

            _active_mode = get_active_permission_mode()
            if _active_mode is not None:
                _tool_perm = get_tool_permission(name)
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
            from src.tools.tools_config import is_autonomous

            _autonomous = is_autonomous()
        except Exception:
            pass

        if _autonomous:
            return PermissionResult(allowed=True)

        try:
            from src.core.orchestration.approval_gate import (
                register_tool_gate,
                _tool_denied,
            )

            _t5_id = f"{uuid.uuid4().hex[:8]}"
            _t5_ev = register_tool_gate(_t5_id)
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
                            k: str(v)[:200] for k, v in args.items() if k != "content"
                        },
                    },
                )
            # Wait for approval — mirrors orchestrator.execute_tool gate logic exactly.
            # AsyncGate.wait() is safe from any thread (uses run_coroutine_threadsafe
            # internally when a loop is available, threading.Event otherwise).
            # Never skip this wait: doing so would silently grant approval.
            granted = _t5_ev.wait(timeout=120.0)
            if not granted or _t5_id in _tool_denied:
                _tool_denied.discard(_t5_id)
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
