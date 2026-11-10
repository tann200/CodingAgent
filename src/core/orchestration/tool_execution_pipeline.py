"""tool_execution_pipeline.py — Phase C extraction.

Contains the implementation of ``Orchestrator.execute_tool`` as the module-level
function ``execute_tool_impl(orch, tool_call)``.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import signal
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional, cast

from src.core.messaging.event_types import AgentModeChanged, AgentPlanCommitted, FileDeleted, FileModified, PreviewPending, SpawnPermissionRequired, TokenBudget, TokenBudgetUpdate, ToolExecuteError, ToolExecuteFinish, ToolExecuteStart, ToolInvoked, ToolPermissionRequired
from src.core.security.redaction import redact_data, redact_secrets
logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_SECONDS: float = 120.0
TOOL_EXECUTOR_MAX_WORKERS: int = 5
RESULT_MAX_CHARS: int = 8_000
TOOL_OUTPUT_MAX_CHARS: int = RESULT_MAX_CHARS

TIER_TOOL_OUTPUT_MAX_CHARS: Dict[str, int] = {
    "nano": 6_000,
    "compact": 12_000,
    "standard": 24_000,
    "frontier": 48_000,
}


def resolve_tool_output_max_chars(orch: Any = None) -> int:
    """Resolve maximum allowed characters for a tool output string based on runtime policy.

    Scales dynamically based on active ModelRuntimePolicy:
      - nano: 6,000 chars (~1,500 tokens)
      - compact: 12,000 chars (~3,000 tokens)
      - standard: 24,000 chars (~6,000 tokens)
      - frontier: 48,000 chars (~12,000 tokens)
    Falls back to RESULT_MAX_CHARS (8,000 chars) if orchestrator policy cannot be determined.
    """
    if orch is None:
        return RESULT_MAX_CHARS

    explicit = getattr(orch, "_tool_output_max_chars", None)
    if isinstance(explicit, int) and explicit > 0:
        return explicit

    pol = getattr(orch, "_runtime_policy", None) or getattr(orch, "runtime_policy", None)
    if pol is not None:
        if hasattr(pol, "tool_output_max_chars"):
            val = getattr(pol, "tool_output_max_chars")
            if isinstance(val, int) and val > 0:
                return val
        if isinstance(pol, dict):
            val = pol.get("tool_output_max_chars")
            if isinstance(val, int) and val > 0:
                return val
            tier = pol.get("policy_tier")
            if tier and str(tier) in TIER_TOOL_OUTPUT_MAX_CHARS:
                return TIER_TOOL_OUTPUT_MAX_CHARS[str(tier)]

    try:
        from src.core.orchestration.graph.runtime_policy import resolve_runtime_policy

        resolved = resolve_runtime_policy(orch)
        if hasattr(resolved, "tool_output_max_chars"):
            val = getattr(resolved, "tool_output_max_chars")
            if isinstance(val, int) and val > 0:
                return val
        if hasattr(resolved, "policy_tier") and str(resolved.policy_tier) in TIER_TOOL_OUTPUT_MAX_CHARS:
            return TIER_TOOL_OUTPUT_MAX_CHARS[str(resolved.policy_tier)]
    except Exception:
        pass

    return RESULT_MAX_CHARS

_TOOL_EXECUTOR_LOCK: threading.Lock = threading.Lock()
_CURRENT_TOOL_CANCEL_EVENT: ContextVar[Optional[threading.Event]] = ContextVar(
    "_CURRENT_TOOL_CANCEL_EVENT", default=None
)
_ACTIVE_AGENT_CTX: ContextVar[Optional[Any]] = ContextVar(
    "_ACTIVE_AGENT_CTX", default=None
)
_THREAD_CANCEL_EVENTS: Dict[int, threading.Event] = {}
_THREAD_CANCEL_LOCK: threading.Lock = threading.Lock()
_CURRENT_TOOL_PROCESS: ContextVar[Optional[Any]] = ContextVar(
    "_CURRENT_TOOL_PROCESS", default=None
)
_THREAD_TOOL_PROCESSES: Dict[int, Any] = {}
_THREAD_PROCESS_LOCK: threading.Lock = threading.Lock()


def register_current_tool_process(proc: Any) -> None:
    """Register an active subprocess for the current tool execution so it can be terminated on timeout/cancel."""
    try:
        _CURRENT_TOOL_PROCESS.set(proc)
    except Exception:
        pass
    tid = threading.get_ident()
    with _THREAD_PROCESS_LOCK:
        _THREAD_TOOL_PROCESSES[tid] = proc


def unregister_current_tool_process(proc: Any = None) -> None:
    """Unregister the active subprocess."""
    tid = threading.get_ident()
    with _THREAD_PROCESS_LOCK:
        _THREAD_TOOL_PROCESSES.pop(tid, None)


def _kill_proc_group(proc: Any) -> None:
    """Kill a process and its process group (os.killpg)."""
    try:
        pid = getattr(proc, "pid", None)
        if pid is not None:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    except Exception:
        pass


def is_current_tool_cancelled() -> bool:
    """Return True if the currently executing tool has been cancelled or timed out."""
    ev = _CURRENT_TOOL_CANCEL_EVENT.get()
    if ev is not None and ev.is_set():
        return True
    tid = threading.get_ident()
    with _THREAD_CANCEL_LOCK:
        ev = _THREAD_CANCEL_EVENTS.get(tid)
    if ev is not None and ev.is_set():
        return True
    return False


def _truncate_result_strings(
    result: Any,
    _depth: int = 0,
    max_chars: Optional[int] = None,
) -> Any:
    """Recursively truncate overly large string fields in tool results.

    E-03: The previous implementation only handled the top-level dict and a
    single nested ``result["result"]`` dict.  Deeper nesting and list values
    were silently passed through at full size, bypassing TOOL_OUTPUT_MAX_CHARS.

    This replacement recurses into dicts and lists up to a safety depth of 6,
    scaling dynamically with the resolved *max_chars* ceiling (defaults to
    ``TOOL_OUTPUT_MAX_CHARS``).
    """
    ceiling = max_chars if (isinstance(max_chars, int) and max_chars > 0) else TOOL_OUTPUT_MAX_CHARS
    if _depth > 6:
        return result
    if isinstance(result, str):
        if len(result) > ceiling:
            return (
                result[:ceiling]
                + f"\n... [truncated: {len(result) - ceiling} chars omitted]"
            )
        return result
    if isinstance(result, dict):
        return {k: _truncate_result_strings(v, _depth + 1, max_chars=ceiling) for k, v in result.items()}
    if isinstance(result, list):
        return [_truncate_result_strings(v, _depth + 1, max_chars=ceiling) for v in result]
    return result


from src.core.orchestration.tool_constants import (  # noqa: E402
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    WRITE_TOOLS_REQUIRING_READ,
    _write_permission_audit,
)

try:
    import importlib as _il

    _pyd = _il.import_module("pydantic")
    ValidationError = getattr(_pyd, "ValidationError")
except Exception:

    class ValidationError(Exception):  # type: ignore[no-redef]
        pass


try:
    from src.core.orchestration.tool_contracts import (  # type: ignore[attr-defined]
        ToolContract,  # type: ignore[assignment]
        get_tool_contract,
    )
except ImportError:

    def get_tool_contract(name: str) -> Any:  # type: ignore[misc]
        return None

    class ToolContract:  # type: ignore[no-redef]
        @staticmethod
        def model_validate(obj: Any) -> Any:  # type: ignore[misc]
            return obj


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


try:
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
except Exception:
    guilogger = logger  # type: ignore[assignment]


try:
    from src.core.orchestration.tool_result_formatter import (
        format_tool_result as _format_tool_result,
    )
except Exception:

    def _format_tool_result(result: Any, tool_name: Optional[str] = None) -> str:  # type: ignore[misc]
        return str(result)


def _check_read_before_write(
    orch: Any,
    name: str,
    args: dict,
    workspace_guard_tools: frozenset,
) -> Optional[Dict[str, Any]]:
    """Enforce read-before-write for write tools."""
    if name not in workspace_guard_tools:
        return None

    path_arg = (
        args.get("path")
        or args.get("file_path")
        or args.get("src_path")
        or args.get("src")
        or args.get("old_path")
        or args.get("dir_path")
        or args.get("directory")
    )
    if not path_arg:
        return None

    try:
        target = Path(orch.working_dir or ".") / path_arg
        resolved_path = str(target.resolve())
        from src.tools.guardrails import _global_read_files, _get_ctx_read_files, _global_lock
        with _global_lock:
            in_guardrails = (resolved_path in _global_read_files) or (resolved_path in _get_ctx_read_files())
        if in_guardrails:
            orch._session_read_files.add(resolved_path)

        if target.exists() and resolved_path not in orch._session_read_files:
            err_msg = (
                f"Security/Logic violation: You must read '{path_arg}' "
                "before writing to it. Use read_file first to inspect "
                "the current content."
            )
            return {
                "ok": False,
                "error": err_msg,
                "history": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "tool_execution_result": {
                                    "ok": False,
                                    "error": err_msg,
                                }
                            }
                        ),
                    }
                ],
                "last_result": {"ok": False, "error": err_msg},
                "next_action": None,
            }
    except Exception:
        pass
    return None


def _check_workspace_scope_guard(
    orch: Any,
    name: str,
    args: dict,
    workspace_guard_tools: frozenset,
) -> Optional[Dict[str, Any]]:
    """Block writes to files outside the plan's affected_files set."""
    if name not in workspace_guard_tools:
        return None

    path_arg = (
        args.get("path")
        or args.get("file_path")
        or args.get("src_path")
        or args.get("src")
        or args.get("old_path")
        or args.get("dir_path")
        or args.get("directory")
    )
    if not path_arg:
        return None

    try:
        affected_files = getattr(orch, "_affected_files", None) or []
        if not affected_files:
            return None

        workdir = str(orch.working_dir or ".").rstrip("/\\")

        def _norm(p: str) -> str:
            p = str(p).replace("\\", "/")
            # Collapse . and .. components using the workdir as the anchor
            p = str((Path(workdir) / p).resolve())
            if p.startswith(workdir + "/"):
                p = p[len(workdir) + 1 :]
            elif p.lstrip("/\\").startswith(workdir.lstrip("/\\") + "/"):
                stripped = p.lstrip("/\\")
                wd_rel = workdir.lstrip("/\\")
                p = stripped[len(wd_rel) + 1 :]
            return p.lstrip("/\\")

        target_norm = _norm(path_arg)
        affected_set = {_norm(str(item)) for item in affected_files}

        if target_norm not in affected_set:
            try:
                from src.core.orchestration.event_bus import get_event_bus

                get_event_bus().publish(
                    "scope.violation",
                    {
                        "tool": name,
                        "path": path_arg,
                        "allowed": list(affected_set),
                    },
                )
            except Exception:
                pass
            return {
                "ok": False,
                "error": (
                    f"File '{path_arg}' is outside the task scope. "
                    f"The current plan authorises writes to: {sorted(affected_set)}. "
                    "Use ask_user to confirm expanding the scope."
                ),
            }
    except Exception:
        pass
    return None


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
            approved = getattr(orch, "_plan_mode_approved", None)
            if approved is not True:
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' is blocked: the current plan has not been "
                        "approved yet. Await user approval before making file changes."
                    ),
                }
    except Exception:
        pass
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
    except Exception:
        pass
    return None


def _check_permission_mode_guard(orch: Any, name: str) -> Optional[Dict[str, Any]]:
    """Block tools that exceed the active permission mode."""
    try:
        from src.tools.tools_config import (
            PermissionLevel,
            get_active_permission_mode,
            get_tool_permission,
        )

        active_mode = get_active_permission_mode()
        if active_mode is not None:
            tool_perm = get_tool_permission(name)
            active_rank = {
                PermissionLevel.READ_ONLY: 0,
                PermissionLevel.WORKSPACE_WRITE: 1,
                PermissionLevel.DANGER: 2,
                PermissionLevel.PROMPT: 3,
                PermissionLevel.ALLOW: 4,
            }.get(active_mode, 4)
            tool_rank = {
                PermissionLevel.READ_ONLY: 0,
                PermissionLevel.WORKSPACE_WRITE: 1,
                PermissionLevel.DANGER: 2,
                PermissionLevel.PROMPT: 3,
                PermissionLevel.ALLOW: 4,
            }.get(tool_perm, 4)
            if tool_rank > active_rank:
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' requires '{tool_perm.value}' permission "
                        f"but active permission mode is '{active_mode.value}'."
                    ),
                }
    except Exception:
        pass
    return None


def _check_workdir_confinement(orch: Any, name: str, args: dict) -> bool:
    """Return True when the tool call still needs explicit approval."""
    if name == "ask_user":
        return False

    needs_gate = name in PERMISSION_REQUIRED_TOOLS
    try:
        from src.tools.tools_config import PermissionLevel, get_tool_permission

        tool_perm = get_tool_permission(name)
        if tool_perm in (PermissionLevel.DANGER, PermissionLevel.PROMPT):
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

    # SPEC-01: if workdir or working_dir is explicitly passed and points outside workspace, require approval gate
    for wd_key in ("workdir", "working_dir"):
        if wd_key in args and args[wd_key]:
            if not _inside(args[wd_key]):
                return True

    if name in ("bash", "run_tests", "run_bash"):
        if _inside(args.get("workdir", workdir_path)):
            return False
        return True

    if name == "delete_file":
        # HS-5 / SEC-8: deletion is irreversible, so it must NEVER be blindly
        # auto-approved just because the target is inside the working directory —
        # it always requires explicit approval regardless of path confinement.
        return True

    return needs_gate


def _run_preflight_and_lookup(
    orch: Any,
    name: str,
    args: dict,
    tool_call: Dict[str, Any],
    tool_call_id: str,
) -> "tuple[Dict[str, Any] | None, Any]":
    """Run preflight check, tool registry lookup, and agent allowed_tools gate.

    Returns ``(error_dict, tool)`` where *error_dict* is non-None on failure and
    *tool* is the registry entry on success.
    """
    try:
        preflight = orch.preflight_check(tool_call)
    except Exception:
        preflight = {"ok": True}
    if isinstance(preflight, dict) and not preflight.get("ok", True):
        if preflight.get("error") == "tool_not_found":
            return (
                {
                    "ok": False,
                    "error": f"Tool '{name}' not found.",
                    "details": preflight.get("message"),
                    "suggestions": preflight.get("suggestions", []),
                },
                None,
            )
        return (
            {
                "ok": False,
                "error": preflight.get("message")
                or preflight.get("error")
                or "preflight_failed",
            },
            None,
        )

    # E-05: 'name' was already validated above; re-fetching from tool_call is a
    # no-op and could silently swap the name after guard checks ran against it.
    tool = orch.tool_registry.get(name)
    if not tool:
        return {"ok": False, "error": f"Tool '{name}' not found."}, None

    try:
        active_agent = _ACTIVE_AGENT_CTX.get()
        if active_agent is None:
            active_agent = getattr(orch, "active_agent", None)
        if active_agent is not None and not active_agent.is_tool_permitted(name):
            _write_permission_audit(
                orch.working_dir, name, args, "deny", "spawn_allowed_tools"
            )
            return (
                {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' is not permitted for the active delegated agent "
                        "(allowed_tools restriction). Check the agent's allowed_tools list."
                    ),
                },
                None,
            )
    except Exception:
        pass

    # P2-3: Structural role-based tool restriction for the main orchestrator role.
    # When no active_agent is set, check the orchestrator's role_manager if
    # available.  This enforces reviewer/researcher read-only policies at the
    # graph level rather than relying solely on prompt instructions.
    try:
        role_manager = getattr(orch, "role_manager", None)
        if role_manager is not None and active_agent is None:
            current_role = role_manager.get_current_role()
            if current_role is not None:
                from src.core.orchestration.role_config import is_tool_allowed_for_role

                if not is_tool_allowed_for_role(name, current_role):
                    _write_permission_audit(
                        orch.working_dir, name, args, "deny", "role_tool_restriction"
                    )
                    return (
                        {
                            "ok": False,
                            "error": (
                                f"Tool '{name}' is not permitted for role '{current_role}'. "
                                f"Role '{current_role}' has structural tool restrictions — "
                                "check the role's allowed_tools / denied_tools config."
                            ),
                        },
                        None,
                    )
    except Exception:
        pass

    agent_override = _check_agent_permission_override(orch, name, args)
    if agent_override is not None:
        return agent_override, None

    return None, tool


def _run_sandbox_and_snapshot(
    orch: Any,
    name: str,
    args: dict,
    tool: Any,
    path_arg: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Validate file content (sandbox) and take a rollback snapshot for write tools.

    Returns an error dict if sandbox validation fails; None on success.
    """
    try:
        if "write" in tool.get("side_effects", []) and path_arg:
            new_content = args.get("content")
            # SPEC-P1.7: Compute candidate replacement text for atomic string replacement tools
            if not new_content and name == "edit_file_atomic" and path_arg:
                old_s = args.get("old_string")
                new_s = args.get("new_string")
                if isinstance(old_s, str) and isinstance(new_s, str):
                    try:
                        from pathlib import Path as _P
                        target_p = _P(path_arg)
                        if not target_p.is_absolute() and hasattr(orch, "working_dir") and orch.working_dir:
                            target_p = _P(orch.working_dir) / target_p
                        if target_p.is_file():
                            orig_text = target_p.read_text(encoding="utf-8")
                            if old_s in orig_text:
                                new_content = orig_text.replace(old_s, new_s, 1)
                    except Exception:
                        pass

            if new_content and isinstance(new_content, str):
                p_str = str(path_arg)
                if p_str.endswith(".py"):
                    import ast as _ast

                    try:
                        _ast.parse(new_content)
                    except SyntaxError as syn:
                        return {
                            "ok": False,
                            "error": (
                                "Sandbox validation error: new content has a syntax "
                                f"error at line {syn.lineno}: {syn.msg}"
                            ),
                        }
                elif p_str.endswith((".js", ".mjs", ".cjs")):
                    import subprocess as _sp
                    import tempfile as _tf

                    try:
                        node = _sp.run(
                            ["node", "--version"],
                            capture_output=True,
                            timeout=3,
                        )
                        if node.returncode == 0:
                            tmp_path = None
                            with _tf.NamedTemporaryFile(
                                suffix=".js",
                                mode="w",
                                delete=False,
                                encoding="utf-8",
                            ) as tmp:
                                tmp.write(new_content)
                                tmp_path = tmp.name
                            try:
                                check = _sp.run(
                                    ["node", "--check", tmp_path],
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                )
                                if check.returncode != 0:
                                    err = (
                                        check.stderr or check.stdout or "syntax error"
                                    ).strip()
                                    return {
                                        "ok": False,
                                        "error": (
                                            "Sandbox validation error: JS syntax "
                                            f"error: {err[:300]}"
                                        ),
                                    }
                            finally:
                                try:
                                    import os as _os

                                    if tmp_path:
                                        _os.unlink(tmp_path)
                                except Exception:
                                    pass
                    except (FileNotFoundError, _sp.TimeoutExpired):
                        pass
                elif p_str.endswith((".ts", ".tsx")):
                    # SPEC-P1.1: Validate TypeScript only if tsc is directly available on PATH;
                    # do NOT force node --check with .js suffix which fails on valid TS types/interfaces.
                    import shutil as _shutil
                    import subprocess as _sp
                    import tempfile as _tf

                    tsc_bin = _shutil.which("tsc")
                    if tsc_bin:
                        tmp_path = None
                        ext = ".tsx" if p_str.endswith(".tsx") else ".ts"
                        try:
                            with _tf.NamedTemporaryFile(
                                suffix=ext,
                                mode="w",
                                delete=False,
                                encoding="utf-8",
                            ) as tmp:
                                tmp.write(new_content)
                                tmp_path = tmp.name
                            check = _sp.run(
                                [
                                    tsc_bin,
                                    "--noEmit",
                                    "--allowJs",
                                    "--target",
                                    "es2020",
                                    tmp_path,
                                ],
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            if check.returncode != 0:
                                err = (check.stdout or check.stderr or "syntax error").strip()
                                if "error TS" in err:
                                    return {
                                        "ok": False,
                                        "error": (
                                            f"Sandbox validation error: TS syntax error: {err[:300]}"
                                        ),
                                    }
                        except Exception:
                            pass
                        finally:
                            try:
                                import os as _os

                                if tmp_path:
                                    _os.unlink(tmp_path)
                            except Exception:
                                pass

            # SPEC-P3.5: Mark successfully sandbox-validated content to avoid duplicate linting in tool body
            if new_content and isinstance(new_content, str) and path_arg:
                try:
                    from pathlib import Path as _P
                    from src.tools._lint_verify import mark_content_prevalidated

                    target_p = _P(path_arg)
                    if not target_p.is_absolute() and hasattr(orch, "working_dir") and orch.working_dir:
                        target_p = _P(orch.working_dir) / target_p
                    mark_content_prevalidated(target_p, new_content)
                except Exception:
                    pass
    except Exception as e:
        guilogger.error(f"Sandbox validation failed (fail-closed): {e}")
        return {
            "ok": False,
            "error": f"Sandbox validation aborted: {str(e)}. Write operation blocked for safety.",
        }

    from src.tools.rollback_tools import WRITE_TOOL_NAMES

    paths_to_snap: list[str] = []
    if path_arg:
        paths_to_snap.append(str(path_arg))
    for key in ("paths", "files", "target_files", "file_paths"):
        val = args.get(key)
        if isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str) and item not in paths_to_snap:
                    paths_to_snap.append(item)
                elif isinstance(item, dict) and "path" in item and str(item["path"]) not in paths_to_snap:
                    paths_to_snap.append(str(item["path"]))
    dst_arg = (
        args.get("new_path")
        or args.get("dst_path")
        or args.get("dst")
        or args.get("new_name")
    )
    if dst_arg and str(dst_arg) not in paths_to_snap:
        paths_to_snap.append(str(dst_arg))

    if ("write" in tool.get("side_effects", []) or name in WRITE_TOOL_NAMES) and paths_to_snap:
        try:
            if getattr(orch, "_step_snapshot_id", None):
                for p_snap in paths_to_snap:
                    orch.rollback_manager.append_to_snapshot(
                        orch._step_snapshot_id, p_snap
                    )
                orch._current_snapshot_id = orch._step_snapshot_id
            else:
                orch._current_snapshot_id = orch.rollback_manager.snapshot_files(
                    paths_to_snap, snapshot_id=None
                )
        except Exception as snap_err:
            guilogger.warning(f"Snapshot failed (non-blocking): {snap_err}")

    # P3.2: fingerprint the "before" state so the change log can prove what
    # this call actually altered (and attribute it).  Non-fatal.
    try:
        from src.core.orchestration.change_log import begin_change

        begin_change(orch, name, args, orch.working_dir)
    except Exception as exc:
        guilogger.debug(f"change_log: begin_change skipped (non-blocking): {exc}")

    return None


def _timeout_result(name: str, timeout_seconds: float) -> Dict[str, Any]:
    """Build the result for a tool that exceeded its timeout (P3.4).

    A timeout does **not** mean the tool failed.  ``_dispatch_tool_call`` runs
    tools on a ``ThreadPoolExecutor`` and calls ``future.cancel()``, which
    returns ``False`` for an already-running future — the tool keeps executing
    and may well complete its side effect moments later (verified by execution).
    So for a non-idempotent tool the honest answer is "outcome unknown", not
    "failed": a "failed" result invites the model to simply re-run the call,
    which is precisely how a write gets applied twice.

    The wording is chosen for the model reading it in ``history``: verify before
    re-running rather than re-run on faith.  P3.2's ``change_log`` journal is
    what makes that verification possible.
    """
    base = {
        "ok": False,
        "error": (
            f"Tool execution timed out after {timeout_seconds} seconds. "
            "Consider breaking down the task into smaller steps."
        ),
        "error_code": "tool.timeout",
    }
    try:
        from src.tools.tools_config import is_idempotent_tool

        repeatable = is_idempotent_tool(name)
    except Exception:
        # Fail closed: if idempotency cannot be established, treat the tool as
        # unsafe to re-run.
        repeatable = False

    if not repeatable:
        base["outcome_unknown"] = True
        base["error"] = (
            f"Tool execution timed out after {timeout_seconds} seconds. The tool "
            "was NOT cancelled and may still have completed its side effect, so "
            "its effect on disk/state is UNKNOWN. Do NOT re-run this call blindly "
            "— verify the current state first (e.g. read the file or check the "
            "change log), then re-run only if the effect is genuinely missing. "
            "Re-running without checking risks applying the change twice."
        )
        base["error_code"] = "tool.outcome_unknown"
    return base


def _dispatch_tool_call(
    orch: Any,
    name: str,
    args: dict,
    tool: Any,
    tool_call_id: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """Run the tool callable (with timeout) and return a raw result or error dict.

    The returned dict is the raw tool result on success, or ``{"ok": False, ...}``
    on timeout / exception.  Callers must still call ``_normalize_tool_result``.
    """
    import concurrent.futures as _cf
    import contextvars as _contextvars
    import inspect as _inspect

    # Inject and confine working directory so tool functions resolve paths correctly (SPEC-01).
    if hasattr(orch, "working_dir") and orch.working_dir:
        try:
            sig = _inspect.signature(tool["fn"])
            from pathlib import Path as _Path
            target_workdir = _Path(orch.working_dir).resolve()

            def _is_confined(p: Any) -> bool:
                try:
                    resolved = _Path(p).resolve()
                    return resolved == target_workdir or str(resolved).startswith(
                        str(target_workdir) + "/"
                    )
                except Exception:
                    return False

            if "workdir" in sig.parameters:
                args = dict(args)
                if "workdir" in args and args["workdir"]:
                    if not _is_confined(args["workdir"]):
                        return {
                            "ok": False,
                            "error": f"Path '{args['workdir']}' is outside working directory.",
                        }
                    args["workdir"] = _Path(args["workdir"]).resolve()
                else:
                    args["workdir"] = target_workdir

            if "working_dir" in sig.parameters:
                args = dict(args)
                if "working_dir" in args and args["working_dir"]:
                    if not _is_confined(args["working_dir"]):
                        return {
                            "ok": False,
                            "error": f"Path '{args['working_dir']}' is outside working directory.",
                        }
                    args["working_dir"] = str(_Path(args["working_dir"]).resolve())
                else:
                    args["working_dir"] = str(target_workdir)
        except (ValueError, TypeError):
            pass

    cancel_ev = threading.Event()
    cancel_token = _CURRENT_TOOL_CANCEL_EVENT.set(cancel_ev)

    def _run_tool_callable(fn: Any, kwargs: dict) -> Any:
        tid = threading.get_ident()
        with _THREAD_CANCEL_LOCK:
            _THREAD_CANCEL_EVENTS[tid] = cancel_ev
        try:
            rv = fn(**kwargs)
            if _inspect.isawaitable(rv):
                import asyncio as _asyncio
                from typing import Coroutine

                # Check if we're in a thread with an existing event loop
                try:
                    loop = _asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None and loop.is_running():
                    # Thread has an active running event loop; run the coroutine in a
                    # dedicated thread to avoid nested loop collision and preserve caller's loop.
                    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(_asyncio.run, cast(Coroutine[Any, Any, Any], rv)).result()
                else:
                    # No running loop - safe to use asyncio.run
                    return _asyncio.run(cast(Coroutine[Any, Any, Any], rv))
            return rv
        finally:
            with _THREAD_CANCEL_LOCK:
                _THREAD_CANCEL_EVENTS.pop(tid, None)

    orch_token = None
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        orch_token = _PARENT_ORCHESTRATOR_VAR.set(orch)
    except Exception:
        orch_token = None

    try:
        if timeout_seconds > 0:
            tool_executor = getattr(orch, "_tool_executor", None)
            if tool_executor is None:
                with _TOOL_EXECUTOR_LOCK:
                    tool_executor = getattr(orch, "_tool_executor", None)
                    if tool_executor is None:
                        tool_executor = _cf.ThreadPoolExecutor(
                            max_workers=TOOL_EXECUTOR_MAX_WORKERS
                        )
                        orch._tool_executor = tool_executor
            worker_tid_holder: list[int] = []

            def _timed_tool_worker(fn_target: Any, fn_args: dict) -> Any:
                worker_tid_holder.append(threading.get_ident())
                return _run_tool_callable(fn_target, fn_args)

            try:
                ctx = _contextvars.copy_context()
                future = tool_executor.submit(
                    ctx.run, _timed_tool_worker, tool["fn"], args
                )
            except Exception:
                future = tool_executor.submit(_timed_tool_worker, tool["fn"], args)
            try:
                res = future.result(timeout=timeout_seconds)
            except _cf.TimeoutError:
                cancel_ev.set()
                future.cancel()
                # Isolate timeout kill strictly to the timed-out worker thread so
                # sibling concurrent read-only tool subprocesses are not killed.
                with _THREAD_PROCESS_LOCK:
                    timed_out_tid = worker_tid_holder[0] if worker_tid_holder else None
                    if timed_out_tid is not None:
                        _proc = _THREAD_TOOL_PROCESSES.pop(timed_out_tid, None)
                        if _proc is not None:
                            try:
                                _kill_proc_group(_proc)
                            except Exception:
                                pass
                raise TimeoutError(
                    f"Tool '{name}' timed out after {timeout_seconds} seconds"
                )
        else:
            res = _run_tool_callable(tool["fn"], args)
    finally:
        try:
            _CURRENT_TOOL_CANCEL_EVENT.reset(cancel_token)
        except Exception:
            pass
        try:
            if orch_token is not None:
                from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                _PARENT_ORCHESTRATOR_VAR.reset(orch_token)
        except Exception:
            pass

    return res  # type: ignore[return-value]


def _handle_tool_execution_error(
    orch: Any,
    name: str,
    args: dict,
    tool_call_id: str,
    exc: Exception,
) -> Dict[str, Any]:
    """Record error telemetry/session state and return a structured error dict."""
    try:
        orch.event_bus.publish_typed(ToolExecuteError(session_update="tool_call_update", tool_call_id=tool_call_id, title=name, status="failed", content=[{"type": "text", "text": str(exc)}], error=str(exc), workdir=str(orch.working_dir)))
    except Exception:
        pass

    try:
        safe_args = {
            k: str(v) if isinstance(v, Path) else v for k, v in args.items()
        }
        orch.session_store.add_tool_call(
            session_id=getattr(orch, "_current_task_id", None),
            tool_name=name,
            args=safe_args,
            result={"error": str(exc)},
            success=False,
        )
    except Exception:
        pass

    try:
        orch._sync_session_state()
    except Exception:
        pass

    try:
        store = getattr(orch, "session_store", None)
        if store is not None and hasattr(store, "add_mistake"):
            store.add_mistake(
                session_id=getattr(orch, "_current_task_id", None) or "unknown",
                summary=f"tool {name} raised {type(exc).__name__}: {str(exc)[:100]}",
                context=str(exc)[:400],
                tool=name,
            )
    except Exception:
        pass

    try:
        from src.core.errors import classify_exception as _classify

        error_code = _classify(exc).value
    except Exception:
        error_code = "system.unknown"

    return {"ok": False, "error": str(exc), "error_code": error_code}


def _run_post_execution(
    orch: Any,
    name: str,
    args: dict,
    tool: Any,
    tool_call_id: str,
    res: Any,
    path_arg: Optional[str],
) -> Dict[str, Any]:
    """Normalise result, apply side-effects, fire events, persist to session store.

    Returns the final ``{"ok": True, "result": ...}`` dict.
    """
    res = orch._normalize_tool_result(res)
    max_chars = resolve_tool_output_max_chars(orch)
    res = _truncate_result_strings(res, max_chars=max_chars)
    res = redact_data(res)

    if name in ("plan_enter", "plan_exit") and isinstance(res, dict) and res.get("ok"):
        new_mode = res.get("agent_mode", "execution")
        setattr(orch, "_agent_mode", new_mode)
        try:
            orch.event_bus.publish_typed(AgentModeChanged(mode=new_mode, tool=name))
        except Exception:
            pass
        if name == "plan_exit" and res.get("steps"):
            setattr(orch, "_committed_plan_steps", res["steps"])
            setattr(orch, "_plan_mode_approved", True)
            try:
                orch.event_bus.publish_typed(AgentPlanCommitted(step_count=len(res["steps"]), tool=name))
            except Exception:
                pass

    try:
        hook_runner = getattr(orch, "_tool_hook_runner", None)
        if hook_runner is not None:
            hook_runner.run_post(name, args, res)
    except Exception as _hook_exc:
        # HS-6: plugin post-tool hooks failing must be observable — the tool
        # result itself is unaffected, but a silent hook failure hides broken
        # extensions and defeats the plugin surface's whole purpose.
        logger.warning(
            "tool_execution_pipeline: post-tool hook for %r failed: %s",
            name,
            _hook_exc,
            exc_info=True,
        )

    try:
        schema = get_tool_contract(name)
        if schema and isinstance(res, dict):
            try:
                schema.model_validate(res)
            except ValidationError:
                try:
                    schema.model_validate(res.get("result") or {})
                except ValidationError as ve:
                    return {
                        "ok": False,
                        "error": f"Tool result failed contract validation: {ve}",
                    }
        else:
            try:
                ToolContract.model_validate({"tool": name, "args": args, "result": res})
            except ValidationError as ve:
                return {
                    "ok": False,
                    "error": f"Tool result failed contract validation: {ve}",
                }
    except Exception as exc:
        # HS-6: contract-framework failures must be observable, not silently
        # passed out of the validation block. The tool DID execute successfully,
        # so fail-open for an infrastructure hiccup — but log it at WARNING.
        logger.warning(
            "tool_execution_pipeline: post-exec contract validation skipped for %r (framework error: %s)",
            name,
            exc,
            exc_info=True,
        )

    import contextlib as _contextlib

    _raw_lock = getattr(orch, "_state_lock", None)
    _lock_ctx = (
        _raw_lock
        if _raw_lock is not None and hasattr(_raw_lock, "__enter__")
        else _contextlib.nullcontext()
    )

    try:
        import time as _time

        ts = _time.time()
        with _lock_ctx:
            orch._usage_buffer[name] = orch._usage_buffer.get(name, 0) + 1
            orch.cost_tracker.record_tool_call(name)
        try:
            orch.event_bus.publish_typed(ToolInvoked(session_update="tool_call_update", tool_call_id=tool_call_id, title=name, status="invoked", timestamp=ts, workdir=str(orch.working_dir)))
        except Exception:
            pass
    except Exception:
        pass

    if res.get("status") == "ok" or res.get("ok") is True:
        if name in ("read_file", "fs.read", "read_file_chunk", "read_file_bytes", "files_read") and path_arg:
            try:
                resolved_path = str(
                    (Path(orch.working_dir or ".") / path_arg).resolve()
                )
                with _lock_ctx:
                    orch._session_read_files.add(resolved_path)
            except Exception:
                pass
        elif name == "batched_file_read":
            try:
                wd = Path(orch.working_dir or ".")
                batch_paths = args.get("paths") or []
                if isinstance(batch_paths, list):
                    resolved_batch = [
                        str((wd / p).resolve())
                        for p in batch_paths
                        if isinstance(p, str) and p.strip()
                    ]
                    with _lock_ctx:
                        orch._session_read_files.update(resolved_batch)
            except Exception:
                pass
        elif name == "rename_file":
            try:
                src_raw = args.get("path") or args.get("src_path") or args.get("src") or args.get("old_path")
                dst_raw = args.get("new_path") or args.get("dst_path") or args.get("dst") or args.get("new_name")
                wd = Path(orch.working_dir or ".")
                src_res = str((wd / src_raw).resolve()) if src_raw else None
                dst_res = str((wd / dst_raw).resolve()) if dst_raw else None
                with _lock_ctx:
                    if src_res:
                        orch._session_modified_files.add(src_res)
                        orch._session_read_files.add(src_res)
                    if dst_res:
                        orch._session_modified_files.add(dst_res)
                        orch._session_read_files.add(dst_res)
                        try:
                            from src.tools.guardrails import mark_file_read
                            mark_file_read(dst_res)
                        except Exception:
                            pass
                    aff = getattr(orch, "_affected_files", None)
                    if isinstance(aff, list):
                        if src_res and src_res not in aff and (src_raw not in aff):
                            aff.append(src_res)
                        if dst_res and dst_res not in aff and (dst_raw not in aff):
                            aff.append(dst_res)
                    elif isinstance(aff, set):
                        if src_res:
                            aff.add(src_res)
                        if dst_res:
                            aff.add(dst_res)
                try:
                    if src_res:
                        orch.event_bus.publish_typed(FileModified(path=src_res, tool=name, workdir=str(orch.working_dir)))
                    if dst_res:
                        orch.event_bus.publish_typed(FileModified(path=dst_res, tool=name, workdir=str(orch.working_dir)))
                except Exception:
                    pass
            except Exception:
                pass
        elif "write" in tool.get("side_effects", []) and path_arg:
            try:
                resolved_path = str(
                    (Path(orch.working_dir or ".") / path_arg).resolve()
                )
                with _lock_ctx:
                    orch._session_modified_files.add(resolved_path)
                try:
                    orch.event_bus.publish_typed(FileModified(path=resolved_path, tool=name, workdir=str(orch.working_dir)))
                except Exception:
                    pass
            except Exception:
                pass
        elif name == "delete_file" and path_arg:
            try:
                resolved_path = str(
                    (Path(orch.working_dir or ".") / path_arg).resolve()
                )
                try:
                    orch.event_bus.publish_typed(FileDeleted(path=resolved_path, workdir=str(orch.working_dir)))
                except Exception:
                    pass
            except Exception:
                pass

    # P3.2: durable, attributed repo-change audit trail.  Runs for every tool
    # call (not only the ones above) because a tool that reported failure but
    # still touched disk is exactly what the trail must surface.  Non-fatal.
    try:
        from src.core.orchestration.change_log import record_change

        record_change(orch, name, args, orch.working_dir)
    except Exception as exc:
        guilogger.debug(f"change_log: record_change skipped (non-blocking): {exc}")

    try:
        with _lock_ctx:
            orch._append_execution_trace(
                {
                    "tool": name,
                    "args": redact_data(orch._normalize_args(args)),
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "result_ok": bool(res.get("status") == "ok" or res.get("ok") is True),
                }
            )
    except Exception:
        pass

    try:
        formatted = _format_tool_result(res, name)
        orch.event_bus.publish_typed(
            ToolExecuteFinish(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                title=name,
                status="completed",
                content=[{"type": "text", "text": redact_secrets(formatted)}],
                raw_output=redact_data(res),
                workdir=str(orch.working_dir),
            )
        )
    except Exception:
        pass

    try:
        if hasattr(orch, "token_monitor"):
            budget = orch.token_monitor.get_budget(
                session_id=getattr(orch, "_current_task_id", "default")
            )
            orch.event_bus.publish_typed(TokenBudgetUpdate(used_tokens=budget.used_tokens, max_tokens=budget.max_tokens, usage_ratio=budget.usage_ratio, session_id=getattr(orch, "_current_task_id", "default")))
            used = budget.used_tokens
            limit = budget.max_tokens or 32_768
            pct = min(100, int(used / limit * 100)) if limit else 0
            orch.event_bus.publish_typed(TokenBudget(used=used, limit=limit, percent=pct, warning=pct >= 80))
    except Exception:
        pass

    try:
        if hasattr(orch, "preview_service") and getattr(
            orch, "_pending_preview_id", None
        ):
            orch.event_bus.publish_typed(PreviewPending(preview_id=orch._pending_preview_id))
    except Exception:
        pass

    try:
        safe_args = {k: str(v) if isinstance(v, Path) else v for k, v in args.items()}
        orch.session_store.add_tool_call(
            session_id=getattr(orch, "_current_task_id", None),
            tool_name=name,
            args=redact_data(safe_args),
            result=redact_data(res),
            success=True,
        )
    except Exception:
        pass

    try:
        orch._sync_session_state()
    except Exception:
        pass

    return {"ok": True, "result": res}


def execute_tool_impl(orch: Any, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Module-level implementation of ``Orchestrator.execute_tool``.

    The pipeline is:
      1. Input validation (name + args types)
      2. Dry-run short-circuit
      3. Guard checks (read-before-write, scope, plan-mode, explore, permission-mode)
      4. Preflight + tool lookup + agent allowed_tools  [_run_preflight_and_lookup]
      5. Permission gate (workdir confinement / approval wait)
      6. Contract validation on *args*
      7. Sandbox validation + rollback snapshot          [_run_sandbox_and_snapshot]
      8. Hook pre-check
      9. Tool dispatch (timeout, executor)               [_dispatch_tool_call]
     10. Post-execution (normalise, events, session)     [_run_post_execution]
    """
    name_raw = tool_call.get("name")
    if not isinstance(name_raw, str):
        return {"ok": False, "error": "Tool name must be a string."}

    name = name_raw
    args = tool_call.get("arguments") or {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "Tool arguments must be a mapping."}

    # 0. Alias normalisation — MUST happen before any permission/policy check so
    # that a deny/approval rule keyed on the canonical name (e.g. "bash") cannot
    # be evaded via an alias (e.g. "run", "shell", "cmd"). SEC-2 / HS-4.
    try:
        from src.tools.tools_config import resolve_tool_alias

        name = resolve_tool_alias(name)
    except Exception:
        pass

    if getattr(orch, "_dry_run", False) and name in DRY_RUN_BLOCKED_TOOLS:
        # Strip internal flags before returning (same as the live-execution path).
        # user_approved is an internal WorkspaceGuard bypass token; it must never
        # be surfaced to callers even in dry_run mode.
        args = dict(args)
        args.pop("user_approved", None)
        entry = {"tool": name, "args": redact_data(args)}
        try:
            orch._dry_run_log.append(entry)
        except Exception:
            pass
        return {"status": "dry_run", "would_call": name, "args": redact_data(args)}

    path_arg = (
        args.get("path")
        or args.get("file_path")
        or args.get("src_path")
        or args.get("src")
        or args.get("old_path")
        or args.get("dir_path")
        or args.get("directory")
    )
    tool_call_id = uuid.uuid4().hex[:8]

    read_guard = _check_read_before_write(orch, name, args, WRITE_TOOLS_REQUIRING_READ)
    if read_guard is not None:
        return read_guard

    scope_guard = _check_workspace_scope_guard(
        orch, name, args, WRITE_TOOLS_REQUIRING_READ
    )
    if scope_guard is not None:
        return scope_guard

    plan_guard = _check_plan_mode_guard(orch, name)
    if plan_guard is not None:
        return plan_guard

    explore_guard = _check_explore_mode_guard(orch, name)
    if explore_guard is not None:
        return explore_guard

    permission_mode_guard = _check_permission_mode_guard(orch, name)
    if permission_mode_guard is not None:
        return permission_mode_guard

    preflight_err, tool = _run_preflight_and_lookup(orch, name, args, tool_call, tool_call_id)
    if preflight_err is not None:
        return preflight_err

    try:
        _write_permission_audit(
            orch.working_dir, name, args, "allow", "passed_all_gates"
        )
    except Exception:
        pass

    needs_gate = _check_workdir_confinement(orch, name, args)
    permission_gate = _run_permission_gate(orch, name, args, tool_call_id, needs_gate)
    if permission_gate is not None:
        return permission_gate

    try:
        orch.event_bus.publish_typed(ToolExecuteStart(tool=name, args={k: str(v)[:200] for k, v in args.items() if k != "content"}, tool_call_id=tool_call_id))
    except Exception:
        pass

    try:
        contract = get_tool_contract(name)
        if contract is not None:
            try:
                contract.model_validate(args)
            except ValidationError as ve:
                return {
                    "ok": False,
                    "error": f"Tool call failed contract validation: {ve}",
                }
    except Exception as exc:
        # TW-3: contract validation must fail CLOSED. A broken/missing contract
        # that raises anything other than ValidationError used to be swallowed
        # here, silently letting the tool run unvalidated.
        guilogger.error(
            "tool_execution_pipeline: contract validation failed for tool %r (fail-closed): %s",
            name,
            exc,
        )
        return {
            "ok": False,
            "error": f"Tool call failed contract validation: {exc}",
        }

    # Strip LLM-injected user_approved (prevents WorkspaceGuard bypass)
    args.pop("user_approved", None)

    path_arg = (
        args.get("path")
        or args.get("file_path")
        or args.get("src_path")
        or args.get("src")
        or args.get("old_path")
        or args.get("dir_path")
        or args.get("directory")
    )

    sandbox_err = _run_sandbox_and_snapshot(orch, name, args, tool, path_arg)
    if sandbox_err is not None:
        return sandbox_err

    if name == "batch":
        try:
            from src.tools.batch_tools import set_batch_orchestrator

            set_batch_orchestrator(orch)
        except Exception:
            pass

    try:
        hook_runner = getattr(orch, "_tool_hook_runner", None)
        if hook_runner is None:
            from src.core.orchestration.tool_hooks import ToolHookRunner

            hook_runner = ToolHookRunner(working_dir=orch.working_dir)
            orch._tool_hook_runner = hook_runner
        hook_result = hook_runner.run_pre(name, args)
        if not hook_result.allowed:
            return {
                "ok": False,
                "error": f"Pre-tool hook denied '{name}': {hook_result.reason}",
            }
    except Exception:
        pass

    timeout_seconds = orch._get_tool_timeout(name)

    try:
        res = _dispatch_tool_call(orch, name, args, tool, tool_call_id, timeout_seconds)
    except TimeoutError:
        guilogger.warning(f"Tool '{name}' timed out after {timeout_seconds}s")
        return _timeout_result(name, timeout_seconds)
    except Exception as e:
        return _handle_tool_execution_error(orch, name, args, tool_call_id, e)

    return _run_post_execution(orch, name, args, tool, tool_call_id, res, path_arg)


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
            # The orchestrator never assigns .permission_policy, so without
            # this fallback the operator's permissions.json (TUI "Allow
            # Always" grants and hand-written DENY rules) was write-only at
            # the main gate — only the doom-loop guard ever read it.
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
        if active_agent is not None:
            # PERM-W4: agent rules append after global rules (last-match-wins).
            merged_policy = active_agent.get_merged_policy(global_policy)
        else:
            # Global DENY rules must apply even without an active agent.
            merged_policy = global_policy
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
    except Exception as e:
        guilogger.error(
            "tool_execution_pipeline: _check_agent_permission_override failed (fail-closed): %s",
            e,
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
    """Run the approval gate when needed.

    The global permissions.json policy (PermissionPolicy) reconciles the
    decision, so TUI "Allow Always" grants and hand-written rules actually
    take effect at this gate:

    - an explicit DENY rule hard-denies (both modes),
    - an explicit ASK rule forces the prompt (both modes — autonomous mode
      then fails closed unless the tool is on the HS-1 allowlist),
    - an explicit ALLOW rule skips the prompt *interactive only* (the
      "Allow Always" promise).  Autonomous mode still requires the HS-1
      ``autonomous_approval_allowed`` allowlist — permissions.json is a
      second, independent suppression channel and must not become an HS-1
      bypass — and ``delete_file`` is never suppressed (HS-5/SEC-8).
    """
    matched_rule = None
    try:
        from src.core.orchestration.permission_policy import (
            Behavior,
            get_permission_policy,
        )

        matched_rule = get_permission_policy().matching_rule(name)
    except Exception:
        matched_rule = None

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
        # HS-1: autonomous mode must NOT silently approve every gated tool —
        # it only skips the prompt for tools the operator explicitly allowed
        # (set_autonomous_approve / CODINGAGENT_AUTONOMOUS_APPROVE).  Anything
        # else is denied loudly instead of auto-approved.
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

    # Interactive mode only (autonomous handled above via HS-1): an explicit
    # ALLOW rule is the operator's persistent "don't ask me again".
    if (
        matched_rule is not None
        and matched_rule.behavior == Behavior.ALLOW
        and name != "delete_file"
    ):
        return None

    gate_event = None
    try:
        gate_event = register_tool_gate(tool_call_id)
        if name == "delegate_task":
            try:
                orch.event_bus.publish_typed(SpawnPermissionRequired(tool=name, role=args.get("role", ""), task=str(args.get("subtask_description", ""))[:200], tool_id=tool_call_id))
            except Exception:
                pass
        orch.event_bus.publish_typed(ToolPermissionRequired(tool=name, args={k: str(v)[:200] for k, v in args.items() if k != "content"}, tool_id=tool_call_id))
    except Exception as perm_exc:
        guilogger.error(f"Permission gate error for '{name}' (fail-closed): {perm_exc}")
        return {"ok": False, "error": f"Permission gate unavailable for '{name}'."}

    granted = True
    try:
        if gate_event is not None and hasattr(gate_event, "wait"):
            granted = gate_event.wait(timeout=APPROVAL_TIMEOUT_SECONDS)
    except Exception:
        granted = False

    denied = False
    try:
        denied = is_tool_denied(tool_call_id)
    except Exception:
        denied = False

    if not granted or denied:
        try:
            discard_tool_denied(tool_call_id)
        except Exception:
            pass
        return {"ok": False, "error": f"Tool '{name}' was denied by the user."}

    try:
        discard_tool_denied(tool_call_id)
    except Exception:
        pass
    return None
