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
from typing import Any, Dict, Optional, TYPE_CHECKING

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

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
        PERM_ORDER as _PERM_ORDER,
    )
except Exception:
    _PERMISSION_REQUIRED_TOOLS = set()  # type: ignore[assignment]
    _PERM_ORDER = {"read_only": 0, "workspace_write": 1, "danger": 2, "prompt": 3, "allow": 4}  # type: ignore[assignment]

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
    from src.tools.permission_context import (
        get_permission_context as _get_permission_context,
    )
except Exception:
    _get_permission_context = None  # type: ignore[assignment]

try:
    from src.tools._tool import permission_kind_to_table_kind as _perm_kind_to_table_kind
except Exception:
    _perm_kind_to_table_kind = None  # type: ignore[assignment]

# F-83: single logger; removed duplicate `logger = logging.getLogger(__name__)` at old line 104.

# ---------------------------------------------------------------------------
# Tool-kind and primary-argument helpers (used by _gate2c_permission_table)
# ---------------------------------------------------------------------------

#: Maps tool name → PermissionKind string for PermissionTable lookups.
_TOOL_KIND_MAP: dict[str, str] = {
    # Writes
    "write_file": "write",
    "edit_file": "edit",
    "edit_by_line_range": "edit",
    "apply_patch": "edit",
    "delete_file": "write",
    "rename_file": "write",
    # Reads
    "read_file": "read",
    "list_dir": "read",
    "glob_tool": "glob",
    "grep_tool": "grep",
    # Shell
    "bash": "bash",
    "run_tests": "bash",
    "run_bash": "bash",
    # Network
    "read_web_page": "webfetch",
    "web_search": "websearch",
    # Delegation
    "delegate_task": "delegate_task",
}


def _tool_kind_for_name(tool_name: str) -> str:
    """Return the PermissionKind string for *tool_name* (fallback: tool_name)."""
    return _TOOL_KIND_MAP.get(tool_name, tool_name)


def _tool_kind_for_name_with_registry(orch: Any, tool_name: str) -> str:
    """Resolve the permission-table kind via registry metadata when available."""
    try:
        registry = getattr(orch, "tool_registry", None)
        if registry is not None and hasattr(registry, "get_permission_kind"):
            permission_kind = registry.get_permission_kind(tool_name)
            if _perm_kind_to_table_kind is not None:
                mapped = _perm_kind_to_table_kind(permission_kind)
                if mapped and mapped != "none":
                    return mapped
    except Exception:
        pass
    return _tool_kind_for_name(tool_name)


#: Keys to try when extracting the primary argument, in priority order.
_PRIMARY_ARG_KEYS: list[str] = [
    "path",
    "file_path",
    "src_path",
    "target",
    "filename",
    "url",
    "uri",
    "command",
    "cmd",
    "bash_command",
    "query",
    "search_query",
    "subtask_description",
    "description",
    "pattern",
    "glob",
]


def _primary_arg_for_tool(tool_name: str, args: dict) -> str:
    """Return the primary string argument of the tool call for pattern matching."""
    for key in _PRIMARY_ARG_KEYS:
        val = args.get(key)
        if val is not None:
            s = str(val)
            return s[:256]  # cap length for pattern matching
    return ""


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
        _gate5_handled = False
        result = self._gate2b_policy_rules(name, args)
        if result.blocked:
            return result
        if result.gate == 5:
            _gate5_handled = True

        # Gate 2d — External directory: prompt user when a file tool targets a
        # path outside the workspace root.  Mirrors opencode's
        # assertExternalDirectoryEffect / external_directory permission.
        result = self._gate2d_external_directory(name, args)
        if result.blocked:
            return result
        if result.gate == 5:
            _gate5_handled = True

        needs_gate, result = self._gate3_permission_level(name, args)
        if result.blocked:
            return result

        result = self._gate4_permission_mode(name)
        if result.blocked:
            return result

        # TASK-PERM-3: Gate 2c — SQLite PermissionTable "allow always" / "deny always" rules.
        # Runs before gate5 so persisted "allow always" rules skip the interactive prompt.
        if needs_gate and not _gate5_handled:
            table_result = self._gate2c_permission_table(name, args)
            if table_result is not None:
                return table_result
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

        Loads the user-level singleton policy (uses ``src.core.paths.get_permissions_path()``
        to locate the user-level permissions.json)
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

                    try:
                        from src.tools.tools_config import (
                            agent_context_path as _agent_context_path,
                        )

                        _proj_path = (
                            _agent_context_path(_Path(_wd)) / "permissions.json"
                        )
                    except Exception:
                        _proj_path = _Path(_wd) / ".codingAgent" / "permissions.json"
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
                            "Edit the permissions file at src.core.paths.get_permissions_path() or "
                            ".agent-context/permissions.json to change this."
                        ),
                    },
                )

            if behavior == _Behavior.ASK:
                # Reuse Gate 5's interactive TUI-event flow.
                result = self._gate5_user_approval(name, args)
                if result.allowed:
                    return PermissionResult(allowed=True, gate=5)
                return result

        except Exception as _e:
            _logger.warning(
                "Gate 2c permission policy check failed (fail-open): %s", _e
            )  # policy failures must never block tool execution

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

    def _gate2c_permission_table(
        self, name: str, args: Dict[str, Any]
    ) -> Optional[PermissionResult]:
        """TASK-PERM-3: Check SQLite PermissionTable for stored "allow/deny always" rules.

        Returns:
          - ``PermissionResult(allowed=True)`` when a matching "allow" rule is found.
          - ``PermissionResult(allowed=False, ...)`` when a matching "deny" rule is found.
          - ``None`` when no rule matches (gate5 user-approval should proceed as normal).

        The primary argument is extracted from *args* using common key names
        (``path``, ``url``, ``command``, etc.) for pattern matching.
        """
        try:
            from src.core.orchestration.permission_table import get_permission_table

            # Resolve the permission-table kind from declared PermissionKind metadata
            # when available; fall back to the legacy name-based mapping for older tools.
            kind_str = _tool_kind_for_name_with_registry(self._orch, name)
            primary_arg = _primary_arg_for_tool(name, args)
            tbl = get_permission_table()
            action = tbl.check(kind_str, primary_arg)

            if action == "allow":
                _logger.debug(
                    "permission_table: pre-approved %s %r via allow rule",
                    name,
                    primary_arg,
                )
                return PermissionResult(allowed=True)

            if action == "deny":
                _logger.debug(
                    "permission_table: blocked %s %r via deny rule", name, primary_arg
                )
                return PermissionResult(
                    allowed=False,
                    gate=3,
                    reason="permission_table deny rule",
                    rejection={
                        "ok": False,
                        "error": (
                            f"Tool '{name}' is blocked by a stored permission rule. "
                            "Remove the rule from .agent-context/permission_rules.db to allow it."
                        ),
                    },
                )
        except Exception:
            pass  # table failures must never block tool execution

        return None  # no matching rule — proceed to gate5

    # File-touching tool names whose path args should be checked for external-directory access.
    _FILE_TOOLS: frozenset = frozenset(
        {
            "read_file",
            "read_file_chunk",
            "read_file_bytes",
            "write_file",
            "edit_file",
            "edit_file_atomic",
            "multiedit",
            "delete_file",
            "rename_file",
            "list_dir",
            "glob_files",
        }
    )

    def _gate2d_external_directory(
        self, name: str, args: Dict[str, Any]
    ) -> PermissionResult:
        """Gate 2d: Prompt for user approval when a file tool targets a path
        outside the current workspace root.

        Mirrors opencode's assertExternalDirectoryEffect / external_directory
        permission.  Internal tools that don't touch the filesystem are skipped.
        If the path is inside the workspace the gate passes immediately (no
        prompt).  If outside, Gate 5 (interactive approval) is triggered so the
        user can allow once, always, or reject.

        Non-fatal: any exception causes the gate to pass so a mis-configured
        orchestrator never silently blocks legitimate workspace-internal calls.
        """
        try:
            if name not in self._FILE_TOOLS:
                return PermissionResult(allowed=True)

            workdir = getattr(self._orch, "working_dir", None)
            if not workdir:
                return PermissionResult(allowed=True)

            from pathlib import Path as _Path

            wd = _Path(workdir).resolve()

            # Collect all path-like args; any one escaping workspace triggers the gate.
            _path_keys = (
                "path",
                "src_path",
                "dst_path",
                "new_path",
                "old_path",
                "src",
                "dst",
            )
            paths_to_check = [str(args[k]) for k in _path_keys if k in args and args[k]]

            for raw in paths_to_check:
                try:
                    resolved = _Path(raw).resolve()
                    resolved.relative_to(wd)  # raises ValueError if outside
                except ValueError:
                    # Path escapes workspace — require user approval via Gate 5.
                    _logger.info(
                        "gate2d: tool %r targets external path %r (workspace: %s) — prompting",
                        name,
                        raw,
                        wd,
                    )
                    result = self._gate5_user_approval(
                        name,
                        {**args, "_external_path_context": raw},
                    )
                    if result.allowed:
                        return PermissionResult(allowed=True, gate=5)
                    return result
                except Exception:
                    pass  # unresolvable paths — let the tool itself report the error
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
                    except Exception as e:
                        _logger.warning(
                            "failed to publish spawn.permission_required, falling through: %s",
                            e,
                        )
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
                else:
                    _logger.warning(
                        "orchestrator event_bus is None, cannot publish tool.permission_required for %s",
                        name,
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
        except Exception as e:
            _logger.warning(
                "gate 5 (user approval) failed, denying tool %s as safety fallback: %s",
                name,
                e,
            )
            return PermissionResult(
                allowed=False,
                gate=5,
                reason=f"gate failure: {e}",
                rejection={
                    "ok": False,
                    "error": f"Tool '{name}' blocked due to gate failure: {e}",
                },
            )
        return PermissionResult(allowed=True)
