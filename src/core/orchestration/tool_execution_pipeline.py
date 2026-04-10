"""tool_execution_pipeline.py — Phase C extraction.

Contains the full implementation of Orchestrator.execute_tool as a
module-level function ``execute_tool_impl(orch, tool_call)``.

The ``Orchestrator.execute_tool`` method is a thin 2-line delegate that calls
this function, keeping the public API unchanged while reducing orchestrator.py
by ~920 lines.

All ``self.*`` accesses are preserved verbatim — they are simply accessed via
the ``orch`` parameter instead of ``self``, so every existing test that patches
orchestrator attributes/methods continues to work without change.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Re-import constants used inside execute_tool body.
# These names are also bound in orchestrator.py for backward-compat patching.
from src.core.orchestration.tool_constants import (  # noqa: E402
    WRITE_TOOLS_REQUIRING_READ,
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    _write_permission_audit,
)

# pydantic ValidationError: safe alias in case pydantic is absent
try:
    import importlib as _il

    _pyd = _il.import_module("pydantic")
    ValidationError = getattr(_pyd, "ValidationError")
except Exception:

    class ValidationError(Exception):  # type: ignore[no-redef]
        """Fallback ValidationError when pydantic is not installed."""

        pass


# tool_contracts: optional dependency
try:
    from src.core.orchestration.tool_contracts import get_tool_contract, ToolContract  # type: ignore[attr-defined]
except ImportError:

    def get_tool_contract(name: str) -> Any:  # type: ignore[misc]
        return None

    class ToolContract:  # type: ignore[no-redef]
        @staticmethod
        def model_validate(obj: Any) -> Any:
            return obj


try:
    from src.core.orchestration.approval_gate import register_tool_gate, _tool_denied
except Exception:

    def register_tool_gate(tool_id: str) -> Any:  # type: ignore[misc]
        return None

    _tool_denied: set = set()

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


def execute_tool_impl(orch: Any, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Full implementation of Orchestrator.execute_tool.

    Parameters
    ----------
    orch:
        The ``Orchestrator`` instance (``self`` in the original method).
    tool_call:
        The tool-call dict as passed to ``Orchestrator.execute_tool``.
    """
    name_raw = tool_call.get("name")
    if not isinstance(name_raw, str):
        return {"ok": False, "error": "Tool name must be a string."}
    # Normalise tool name aliases so the LLM can use short forms
    try:
        from src.tools.tools_config import TOOL_ALIASES

        name = TOOL_ALIASES.get(name_raw, name_raw)
    except Exception:
        name = name_raw
    args = dict(tool_call.get("arguments", {}))

    # F4: Strip LLM-injected user_approved to prevent WorkspaceGuard bypass.
    # user_approved is enforced at the orchestrator / UI level, not via LLM arguments.
    args.pop("user_approved", None)

    # UX-3: Dry-run mode — intercept write/destructive tools and return a
    # preview dict instead of executing.  The intercepted call is also
    # appended to orch._dry_run_log so run_agent_once() can surface the
    # full list in the result under "dry_run_intercepted".
    if getattr(orch, "_dry_run", False) and name in DRY_RUN_BLOCKED_TOOLS:
        entry = {"status": "dry_run", "would_call": name, "args": args}
        try:
            if not hasattr(orch, "_dry_run_log"):
                orch._dry_run_log = getattr(orch, "_dry_run_log", [])  # type: ignore[attr-defined]
            orch._dry_run_log.append(entry)
        except Exception:
            pass
        return entry

    # GAP 2: Generate unique tool call ID for ACP compliance
    tool_call_id = f"call_{uuid.uuid4().hex[:8]}"

    # Hard Rule Enforcement: Read before Edit for write tools
    path_arg = args.get("path") or args.get("file_path")
    if path_arg and name in WRITE_TOOLS_REQUIRING_READ:
        try:
            target = Path(orch.working_dir or ".") / path_arg
            resolved_path = str(target.resolve())
            # Only enforce read-before-write on pre-existing files.
            # Brand-new files have no prior content to protect.
            if target.exists() and resolved_path not in orch._session_read_files:
                # P0-B fix: always inject a "history" entry so the LLM receives
                # the correction signal.  Previously this returned a bare dict with
                # no "history" key, creating a silent micro-loop where the LLM
                # retried the write indefinitely without ever seeing an error message.
                err_msg = (
                    f"Security/Logic violation: You must read '{path_arg}' "
                    f"before writing to it. Use read_file first to inspect "
                    f"the current content."
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

    # GAP-S2: Workspace scope guard — block writes to files outside the plan's
    # affected_files set.  Only active when:
    #   (a) the tool is a write-family tool, AND
    #   (b) _affected_files is non-empty (i.e. a plan has been produced), AND
    #   (c) the target path is NOT in the allowed set.
    # When _affected_files is empty/None the guard is inactive (pre-plan fast-path).
    if path_arg and name in WRITE_TOOLS_REQUIRING_READ:
        try:
            _af = getattr(orch, "_affected_files", None) or []
            if _af:
                # Normalise a path to a workdir-relative form for comparison.
                # Strategy: strip the workdir prefix first (preserving the leading
                # slash while comparing), then strip any remaining leading slashes.
                _workdir = str(orch.working_dir or ".").rstrip("/\\")

                def _norm(p: str) -> str:
                    p = str(p)
                    # Strip workdir prefix (absolute or relative match).
                    if p.startswith(_workdir + "/") or p.startswith(_workdir + "\\"):
                        p = p[len(_workdir) + 1 :]
                    elif p.lstrip("/\\").startswith(_workdir.lstrip("/\\") + "/"):
                        # Handle mismatched leading-slash forms
                        _stripped = p.lstrip("/\\")
                        _wd_rel = _workdir.lstrip("/\\")
                        p = _stripped[len(_wd_rel) + 1 :]
                    # Strip any remaining leading separators.
                    return p.lstrip("/\\")

                _target_norm = _norm(path_arg)
                # Build a set of normalised allowed paths for O(1) lookup.
                _af_set = {_norm(str(_f)) for _f in _af}
                if _target_norm not in _af_set:
                    # Publish scope violation event for observability.
                    try:
                        from src.core.orchestration.event_bus import get_event_bus

                        get_event_bus().publish(
                            "scope.violation",
                            {
                                "tool": name,
                                "path": path_arg,
                                "allowed": list(_af_set),
                            },
                        )
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "error": (
                            f"File '{path_arg}' is outside the task scope. "
                            f"The current plan authorises writes to: {sorted(_af_set)}. "
                            "Use ask_user to confirm expanding the scope."
                        ),
                    }
        except Exception:
            pass  # scope guard must never block on internal errors

    # P4-4: Block write tools when plan mode is active (plan not yet approved).
    # _plan_mode_approved is set by execution_node from AgentState before each call.
    try:
        from src.core.orchestration.plan_mode import PlanMode

        _pm = getattr(orch, "plan_mode", None)
        if _pm and getattr(_pm, "enabled", False) and name in PlanMode.BLOCKED_TOOLS:
            _approved = getattr(orch, "_plan_mode_approved", None)
            if _approved is not True:
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' is blocked: the current plan has not been "
                        "approved yet. Await user approval before making file changes."
                    ),
                }
    except Exception:
        pass  # never block on import/logic errors

    # Explore Mode guard: when active, only analyst read-only tools are permitted.
    # Rejects any write/exec/delegation tool with a clear error message.
    if getattr(orch, "explore_mode", False):
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
            pass  # never block on import errors

    # TASK-01: PermissionLevel-driven gate.
    # DANGER and PROMPT tools require explicit user approval unless the agent
    # is running in autonomous mode (--autonomous / is_autonomous()).
    # The legacy PERMISSION_REQUIRED_TOOLS set is kept for backward compat
    # but the gate now also fires for any tool classified as DANGER/PROMPT.
    #
    # Workdir-confinement bypass: bash, run_tests, delete_file, and ask_user
    # are auto-approved when the operation is confined to the project working
    # directory (or has no filesystem side-effects like ask_user).
    _WORKDIR_BYPASS_TOOLS = {
        "bash",
        "run_tests",
        "delete_file",
        "run_bash",
        "ask_user",
    }
    _workdir_confined = False
    if name in _WORKDIR_BYPASS_TOOLS:
        try:
            from src.core.orchestration.permission_gateway import (
                _is_workdir_confined,
            )

            _workdir_confined = _is_workdir_confined(name, args, orch.working_dir)
        except Exception:
            pass

    _needs_gate = (not _workdir_confined) and (name in PERMISSION_REQUIRED_TOOLS)
    if not _needs_gate and not _workdir_confined:
        try:
            from src.tools.tools_config import get_tool_permission, PermissionLevel

            _perm = get_tool_permission(name)
            if _perm in (PermissionLevel.DANGER, PermissionLevel.PROMPT):
                _needs_gate = True
        except Exception:
            pass  # never block on import errors

    # TASK-20: Active permission mode check.
    # When --permission-mode is set, block any tool whose required level is
    # more permissive than the active mode.  Ordering (least → most permissive):
    #   READ_ONLY < WORKSPACE_WRITE < DANGER < PROMPT < ALLOW
    _PERM_ORDER = {
        "read_only": 0,
        "workspace_write": 1,
        "danger": 2,
        "prompt": 3,
        "allow": 4,
    }
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
            # Block if the tool requires more privilege than the active mode allows.
            # READ_ONLY mode (rank=0) only allows rank-0 (READ_ONLY) tools.
            if _tool_rank > _active_rank:
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' requires '{_tool_perm.value}' permission "
                        f"but active permission mode is '{_active_mode.value}'."
                    ),
                }
    except Exception:
        pass  # never block on import errors

    if _needs_gate:
        # Skip gate entirely when running autonomously
        _autonomous = False
        try:
            from src.tools.tools_config import is_autonomous

            _autonomous = is_autonomous()
        except Exception:
            pass

        if not _autonomous:
            try:
                # TUI-04: use module-level gate registry so each tool call gets its
                # own tool_id and the TUI can target the correct approval request.
                _t4_id = f"{uuid.uuid4().hex[:8]}"
                _t4_ev = register_tool_gate(_t4_id)
                # SPAWN-W5: For delegate_task, also fire the specialized spawn event
                # so the TUI can show a spawn-specific confirmation dialog.
                if name == "delegate_task":
                    try:
                        orch.event_bus.publish(
                            "spawn.permission_required",
                            {
                                "tool": name,
                                "role": args.get("role", ""),
                                "task": str(args.get("subtask_description", ""))[:200],
                                "tool_id": _t4_id,
                            },
                        )
                    except Exception:
                        pass
                orch.event_bus.publish(
                    "tool.permission_required",
                    {"tool": name, "args": args, "tool_id": _t4_id},
                )
                # PERF-VOL23-2: _t4_ev is a threading.Event; .wait(120) blocks the
                # calling thread for up to 2 minutes.  When execute_tool_impl runs
                # inside a ThreadPoolExecutor worker this ties up that worker slot for
                # the full timeout window.  Only affects non-autonomous mode (approval
                # required).  Ensure ThreadPoolExecutor max_workers > 1 if multiple
                # concurrent tool approvals are possible, to avoid pool exhaustion.
                granted = _t4_ev.wait(timeout=120.0)
                if not granted or _t4_id in _tool_denied:
                    _tool_denied.discard(_t4_id)
                    return {
                        "ok": False,
                        "error": f"Tool '{name}' was denied by the user.",
                    }
            except Exception as _perm_exc:
                guilogger.warning(f"Permission gate error for '{name}': {_perm_exc}")

    # SPAWN-W2: allowed_tools enforcement in delegated context.
    # When an active_agent is set (delegated execution), reject tools that are
    # outside the AgentDefinition.allowed_tools allowlist or inside denied_tools.
    try:
        _active_agent_spawn = getattr(orch, "active_agent", None)
        if (
            _active_agent_spawn is not None
            and not _active_agent_spawn.is_tool_permitted(name)
        ):
            _write_permission_audit(
                orch.working_dir, name, args, "deny", "spawn_allowed_tools"
            )
            return {
                "ok": False,
                "error": (
                    f"Tool '{name}' is not permitted for the active delegated agent "
                    f"(allowed_tools restriction). Check the agent's allowed_tools list."
                ),
            }
    except Exception:
        pass

    # PERM-W4: Per-agent permission override.
    # When the orchestrator has an active_agent with permission_rules, merge them
    # with the global policy and apply the combined result.
    try:
        _active_agent = getattr(orch, "active_agent", None)
        if _active_agent is not None:
            _global_policy = getattr(orch, "permission_policy", None)
            _merged_policy = _active_agent.get_merged_policy(_global_policy)
            if _merged_policy is not None and _merged_policy.is_denied(name):
                _write_permission_audit(
                    orch.working_dir, name, args, "deny", "agent_permission_rules"
                )
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' is denied by the active agent's permission rules."
                    ),
                }
    except Exception:
        pass  # never block on import/logic errors

    # PERM-W5: Permission audit log — record allow decision for tools that
    # passed all gates.  Deny decisions are recorded above and in the gate block.
    try:
        _write_permission_audit(
            orch.working_dir, name, args, "allow", "passed_all_gates"
        )
    except Exception:
        pass

    # Publish tool.execute.start AFTER all gate checks so the TUI is only
    # notified about tools that have actually been approved and will run.
    # (GAP 2: ACP sessionUpdate schema)
    try:
        orch.event_bus.publish(
            "tool.execute.start",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "title": name,
                "status": "in_progress",
                "rawInput": args,
                "workdir": str(orch.working_dir),
            },
        )
    except Exception:
        pass

    tool = orch.tool_registry.get(name)
    if not tool:
        return {"ok": False, "error": f"Tool '{name}' not found."}

    try:
        import inspect

        sig = inspect.signature(tool["fn"])
        if "workdir" in sig.parameters:
            args["workdir"] = Path(orch.working_dir or ".")

        # SPAWN-W1: Expose this orchestrator as the parent context for tools that
        # spawn subagents (delegate_task).  Uses a ContextVar so the reference is
        # scoped to this call stack and doesn't leak across threads.
        try:
            from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

            _orch_token = _PARENT_ORCHESTRATOR_VAR.set(orch)
        except Exception:
            _orch_token = None

        # Role enforcement: if orchestrator has current_role, restrict certain tools
        current_role = getattr(orch, "current_role", None)
        if current_role:
            from src.core.orchestration.role_config import is_tool_allowed_for_role

            if not is_tool_allowed_for_role(name, current_role):
                try:
                    if _orch_token is not None:
                        from src.tools.subagent_tools import (
                            _PARENT_ORCHESTRATOR_VAR,
                        )

                        _PARENT_ORCHESTRATOR_VAR.reset(_orch_token)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": f"Tool '{name}' is not permitted for role '{current_role}'",
                }

        # Sandbox validation for write operations — C2 fix: validate the NEW content
        # being written, not the pre-existing file.  The previous approach copied the
        # workspace to a temp dir and ran ast.parse on the OLD file, which always passed.
        # Now we extract the new content from the tool args and parse it directly.
        # HR-2/MC-3 fix: extend to JS/TS using `node --check` (syntax only, no eval).
        if "write" in tool.get("side_effects", []) and path_arg:
            try:
                new_content: str | None = args.get("content")
                if new_content and isinstance(new_content, str):
                    if path_arg.endswith(".py"):
                        import ast as _ast

                        try:
                            _ast.parse(new_content)
                        except SyntaxError as _syn:
                            return {
                                "ok": False,
                                "error": f"Sandbox validation error: new content has a syntax error at line {_syn.lineno}: {_syn.msg}",
                            }
                    elif path_arg.endswith(
                        (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
                    ):
                        # HR-2/MC-3: use Node.js --check flag for JS/TS syntax validation.
                        # node --check parses without executing — safe and fast (~50 ms).
                        # Falls back silently if Node is not installed.
                        import subprocess as _sp
                        import tempfile as _tf

                        try:
                            _node = _sp.run(
                                ["node", "--version"],
                                capture_output=True,
                                timeout=3,
                            )
                            if _node.returncode == 0:
                                # MED-4 fix: initialize _tmp_path before the try block
                                # so the finally cleanup never raises NameError.
                                _tmp_path = None
                                with _tf.NamedTemporaryFile(
                                    suffix=".js",
                                    mode="w",
                                    delete=False,
                                    encoding="utf-8",
                                ) as _tmp:
                                    _tmp.write(new_content)
                                    _tmp_path = _tmp.name
                                try:
                                    _check = _sp.run(
                                        ["node", "--check", _tmp_path],
                                        capture_output=True,
                                        text=True,
                                        timeout=10,
                                    )
                                    if _check.returncode != 0:
                                        _err = (
                                            _check.stderr
                                            or _check.stdout
                                            or "syntax error"
                                        ).strip()
                                        return {
                                            "ok": False,
                                            "error": f"Sandbox validation error: JS/TS syntax error: {_err[:300]}",
                                        }
                                finally:
                                    try:
                                        import os as _os

                                        if _tmp_path:
                                            _os.unlink(_tmp_path)
                                    except Exception:
                                        pass
                        except (FileNotFoundError, _sp.TimeoutExpired):
                            pass  # node not installed or timed out — allow write
            except Exception as e:
                # Fail-closed — do not allow writes if validation itself crashes.
                guilogger.error(f"Sandbox validation failed (fail-closed): {e}")
                return {
                    "ok": False,
                    "error": f"Sandbox validation aborted: {str(e)}. "
                    f"Write operation blocked for safety.",
                }

        # Step B: Snapshot files before write operations for automated rollback.
        # If a step-level transaction is active, accumulate into it (multi-file
        # atomicity). Otherwise create an individual per-write snapshot.
        if "write" in tool.get("side_effects", []) and path_arg:
            try:
                if orch._step_snapshot_id:
                    # Append to the step-level atomic snapshot
                    orch.rollback_manager.append_to_snapshot(
                        orch._step_snapshot_id, path_arg
                    )
                    orch._current_snapshot_id = orch._step_snapshot_id
                    guilogger.debug(
                        f"File added to step snapshot {orch._step_snapshot_id}: {path_arg}"
                    )
                else:
                    # No active transaction — fall back to per-write snapshot
                    orch._current_snapshot_id = orch.rollback_manager.snapshot_files(
                        [path_arg], snapshot_id=None
                    )
                    guilogger.debug(
                        f"Individual snapshot created before write: {path_arg}"
                    )
            except Exception as snap_err:
                guilogger.warning(f"Snapshot failed (non-blocking): {snap_err}")

        # Track files read for read-before-edit enforcement
        if name == "read_file" and path_arg:
            try:
                resolved = str((Path(orch.working_dir or ".") / path_arg).resolve())
                orch._session_read_files.add(resolved)
            except Exception:
                pass

        # T9: Tool Timeout Protection — C1 fix: use ThreadPoolExecutor so timeouts
        # work in any thread (SIGALRM only worked in the main thread, which meant all
        # timeouts were silently disabled when the agent ran from the TUI daemon thread).
        timeout_seconds = orch._get_tool_timeout(name)

        # Expose orchestrator to batch tool via thread-local so it can dispatch
        # sub-calls back through execute_tool. Only set when actually calling batch.
        if name == "batch":
            try:
                from src.tools.batch_tools import set_batch_orchestrator

                set_batch_orchestrator(orch)
            except Exception:
                pass

        # Pre-tool hooks: user-configured shell scripts that can deny the call.
        try:
            _hook_runner = getattr(orch, "_tool_hook_runner", None)
            if _hook_runner is None:
                from src.core.orchestration.tool_hooks import ToolHookRunner

                _hook_runner = ToolHookRunner(working_dir=orch.working_dir)
                orch._tool_hook_runner = _hook_runner
            _hook_result = _hook_runner.run_pre(name, args)
            if not _hook_result.allowed:
                return {
                    "ok": False,
                    "error": f"Pre-tool hook denied '{name}': {_hook_result.reason}",
                }
        except Exception:
            pass  # never block on hook infrastructure errors

        try:
            import concurrent.futures as _cf

            if timeout_seconds > 0:
                # HR-3 fix: reuse the long-lived _tool_executor instead of
                # creating a new ThreadPoolExecutor per call (~5 ms savings each).
                _tex = getattr(orch, "_tool_executor", None)
                if _tex is None:
                    _tex = _cf.ThreadPoolExecutor(max_workers=2)
                    orch._tool_executor = _tex
                _future = _tex.submit(tool["fn"], **args)
                try:
                    res = _future.result(timeout=timeout_seconds)
                except _cf.TimeoutError:
                    _future.cancel()
                    raise TimeoutError(
                        f"Tool '{name}' timed out after {timeout_seconds} seconds"
                    )
            else:
                res = tool["fn"](**args)
        except TimeoutError:
            guilogger.warning(f"Tool '{name}' timed out after {timeout_seconds}s")
            # SPAWN-W1: Reset ContextVar on early return paths too.
            try:
                if _orch_token is not None:
                    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                    _PARENT_ORCHESTRATOR_VAR.reset(_orch_token)
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"Tool execution timed out after {timeout_seconds} seconds. "
                f"Consider breaking down the task into smaller steps.",
            }

        # SPAWN-W1: Reset the parent orchestrator ContextVar after the tool returns.
        try:
            if _orch_token is not None:
                from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                _PARENT_ORCHESTRATOR_VAR.reset(_orch_token)
        except Exception:
            pass

        # Normalize the result to a dict contract
        res = orch._normalize_tool_result(res)

        # Tool output truncation: prevent individual tool results from consuming
        # excessive context window tokens. Cap string values at 8000 chars and
        # inject a truncation notice so the LLM knows content was cut.
        # Mirrors opencode's Truncate.output() strategy.
        _TOOL_OUTPUT_MAX_CHARS = 8000
        if isinstance(res, dict):
            # Copy once before any mutation — avoids repeated copies inside the loop.
            if any(
                isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS
                for _v in res.values()
            ):
                res = dict(res)
                for _k, _v in list(res.items()):
                    if isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS:
                        res[_k] = (
                            _v[:_TOOL_OUTPUT_MAX_CHARS]
                            + f"\n... [truncated: {len(_v) - _TOOL_OUTPUT_MAX_CHARS} chars omitted]"
                        )
            # Also truncate nested result.content (common for read_file)
            _inner = res.get("result")
            if isinstance(_inner, dict) and any(
                isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS
                for _v in _inner.values()
            ):
                _inner = dict(_inner)
                res["result"] = _inner
                for _k, _v in list(_inner.items()):
                    if isinstance(_v, str) and len(_v) > _TOOL_OUTPUT_MAX_CHARS:
                        _inner[_k] = (
                            _v[:_TOOL_OUTPUT_MAX_CHARS]
                            + f"\n... [truncated: {len(_v) - _TOOL_OUTPUT_MAX_CHARS} chars omitted]"
                        )

        # (O4: set_role handler removed — role_tools not registered; runtime role
        # changes via tool calls are disallowed)

        # ORCH-W4: plan_enter / plan_exit mode transitions.
        # If the tool signals an agent_mode change, update the orchestrator attribute so
        # perception_node picks it up on the next system-prompt rebuild.
        if (
            name in ("plan_enter", "plan_exit")
            and isinstance(res, dict)
            and res.get("ok")
        ):
            _new_mode = res.get("agent_mode", "execution")
            setattr(orch, "_agent_mode", _new_mode)
            try:
                orch.event_bus.publish(
                    "agent.mode_changed",
                    {"mode": _new_mode, "tool": name},
                )
            except Exception:
                pass

            # ORCH-W4 plan_exit handoff: when the LLM submits plan steps via
            # plan_exit(steps=[...]), stash them on the orchestrator so
            # execution_node can propagate them into state["current_plan"] and
            # set state["plan_mode_approved"] = True.
            if name == "plan_exit" and res.get("steps"):
                setattr(orch, "_committed_plan_steps", res["steps"])
                setattr(orch, "_plan_mode_approved", True)
                try:
                    orch.event_bus.publish(
                        "agent.plan_committed",
                        {"step_count": len(res["steps"]), "tool": name},
                    )
                except Exception:
                    pass

        # Post-tool hooks: fire-and-forget, exit code ignored.
        try:
            _hook_runner = getattr(orch, "_tool_hook_runner", None)
            if _hook_runner is not None:
                _hook_runner.run_post(name, args, res)
        except Exception:
            pass

        # Validate tool contract if registered. If validation fails, return an error to the caller.
        try:
            schema = get_tool_contract(name)
            if schema and isinstance(res, dict):
                # schema is a pydantic model class; validate either full res or res.get('result')
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
                # Validate general ToolContract wrapper for additional safety
                try:
                    ToolContract.model_validate(
                        {"tool": name, "args": args, "result": res}
                    )
                except ValidationError as ve:
                    return {
                        "ok": False,
                        "error": f"Tool result failed contract validation: {ve}",
                    }

            # Telemetry: record invocation counts and latency (best-effort)
            try:
                import time as _time

                # Telemetry data for usage tracking
                # (call_info reserved for future telemetry use)
                _ts = _time.time()

                # F17: Accumulate in memory — flushed to usage.json at end of run_agent_once()
                orch._usage_buffer[name] = orch._usage_buffer.get(name, 0) + 1
                orch.cost_tracker.record_tool_call(name)

                # Telemetry: publish event for tool invocation (GAP 2: ACP schema)
                try:
                    orch.event_bus.publish(
                        "tool.invoked",
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": tool_call_id,
                            "title": name,
                            "status": "invoked",
                            "timestamp": _ts,
                            "workdir": str(orch.working_dir),
                        },
                    )
                except Exception:
                    pass

                # Trace appending moved to workflow_nodes.py execution_node to avoid duplicates
                # orch._append_execution_trace(call_info)
            except Exception:
                pass
        except Exception:
            # If pydantic is missing or other issues occur, continue without strict validation
            pass

        # Track state for session rules
        if res.get("status") == "ok" or res.get("ok") is True:
            if name in ["read_file", "fs.read"]:
                try:
                    resolved_path = str(
                        (Path(orch.working_dir or ".") / (path_arg or "")).resolve()  # type: ignore[operator]
                    )
                    orch._session_read_files.add(resolved_path)
                except Exception:
                    pass
            elif "write" in tool.get("side_effects", []):
                try:
                    resolved_path = str(
                        (Path(orch.working_dir or ".") / (path_arg or "")).resolve()  # type: ignore[operator]
                    )
                    orch._session_modified_files.add(resolved_path)
                    # Publish file.modified event for UI dashboard
                    try:
                        orch.event_bus.publish(
                            "file.modified",
                            {
                                "path": resolved_path,
                                "tool": name,
                                "workdir": str(orch.working_dir),
                            },
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
            elif name == "delete_file" and path_arg:
                try:
                    resolved_path = str(
                        (Path(orch.working_dir or ".") / path_arg).resolve()
                    )
                    # Publish file.deleted event for UI dashboard
                    try:
                        orch.event_bus.publish(
                            "file.deleted",
                            {
                                "path": resolved_path,
                                "workdir": str(orch.working_dir),
                            },
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

        # Append execution trace for loop detection and auditing
        try:
            entry = {
                "tool": name,
                "args": orch._normalize_args(args),
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "result_ok": bool(res.get("status") == "ok" or res.get("ok") is True),
            }
            try:
                orch._append_execution_trace(entry)
            except Exception:
                pass
        except Exception:
            pass

        # Publish tool.execute.finish event (GAP 2: ACP sessionUpdate schema)
        try:
            formatted = _format_tool_result(res, name)
            orch.event_bus.publish(
                "tool.execute.finish",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "title": name,
                    "status": "completed",
                    "content": [{"type": "text", "text": formatted}],
                    "rawOutput": res,
                    "workdir": str(orch.working_dir),
                },
            )
        except Exception:
            pass

        # Phase 4: Publish token budget status for UI dashboard
        try:
            if hasattr(orch, "token_monitor"):
                budget = orch.token_monitor.get_budget(
                    session_id=getattr(orch, "_current_task_id", "default")
                )
                orch.event_bus.publish(
                    "token.budget.update",
                    {
                        "used_tokens": budget.used_tokens,
                        "max_tokens": budget.max_tokens,
                        "usage_ratio": budget.usage_ratio,
                        "session_id": getattr(orch, "_current_task_id", "default"),
                    },
                )
                # TUI-09: also publish canonical token.budget event used by TUI sidebar
                _used = budget.used_tokens
                _limit = budget.max_tokens or 32_768
                _pct = min(100, int(_used / _limit * 100)) if _limit else 0
                orch.event_bus.publish(
                    "token.budget",
                    {
                        "used": _used,
                        "limit": _limit,
                        "percent": _pct,
                        "warning": _pct >= 80,
                    },
                )
        except Exception:
            pass

        # Phase 3: Publish preview mode events for UI dashboard
        try:
            if hasattr(orch, "preview_service") and hasattr(
                orch, "_pending_preview_id"
            ):
                if orch._pending_preview_id:
                    orch.event_bus.publish(
                        "preview.pending",
                        {"preview_id": orch._pending_preview_id},
                    )
        except Exception:
            pass

        # Step B: Log tool call to SessionStore
        try:
            _safe_args = {
                k: str(v) if isinstance(v, Path) else v for k, v in args.items()
            }
            orch.session_store.add_tool_call(
                session_id=getattr(orch, "_current_task_id", "unknown"),
                tool_name=name,
                args=_safe_args,
                result=res,
                success=True,
            )
        except Exception:
            pass  # non-fatal

        # GAP 1: Sync session state after tool execution
        orch._sync_session_state()

        return {"ok": True, "result": res}
    except Exception as e:
        # Publish tool.execute.error event for UI dashboard (GAP 2: ACP schema)
        try:
            orch.event_bus.publish(
                "tool.execute.error",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "title": name,
                    "status": "failed",
                    "content": [{"type": "text", "text": str(e)}],
                    "error": str(e),
                    "workdir": str(orch.working_dir),
                },
            )
        except Exception:
            pass

        # Log failed tool call to SessionStore
        try:
            _safe_args = {
                k: str(v) if isinstance(v, Path) else v for k, v in args.items()
            }
            orch.session_store.add_tool_call(
                session_id=getattr(orch, "_current_task_id", "unknown"),
                tool_name=name,
                args=_safe_args,
                result={"error": str(e)},
                success=False,
            )
        except Exception:
            pass  # non-fatal

        # GAP 1: Sync session state after tool error
        orch._sync_session_state()

        # SPAWN-W1: Reset ContextVar on exception path to prevent leak.
        try:
            _tok = locals().get("_orch_token")
            if _tok is not None:
                from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                _PARENT_ORCHESTRATOR_VAR.reset(_tok)
        except Exception:
            pass

        return {"ok": False, "error": str(e)}
