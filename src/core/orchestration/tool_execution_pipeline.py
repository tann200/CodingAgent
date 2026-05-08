"""tool_execution_pipeline.py — Phase C extraction.

Contains the implementation of ``Orchestrator.execute_tool`` as the module-level
function ``execute_tool_impl(orch, tool_call)``.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, cast

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_SECONDS: float = 120.0
TOOL_EXECUTOR_MAX_WORKERS: int = 4
TOOL_OUTPUT_MAX_CHARS: int = 8_000


def _truncate_result_strings(result: dict) -> dict:
    """Truncate overly large string fields in tool results."""
    if not isinstance(result, dict):
        return result

    if any(
        isinstance(v, str) and len(v) > TOOL_OUTPUT_MAX_CHARS for v in result.values()
    ):
        result = dict(result)
        for k, v in list(result.items()):
            if isinstance(v, str) and len(v) > TOOL_OUTPUT_MAX_CHARS:
                result[k] = (
                    v[:TOOL_OUTPUT_MAX_CHARS]
                    + f"\n... [truncated: {len(v) - TOOL_OUTPUT_MAX_CHARS} chars omitted]"
                )

    inner = result.get("result")
    if isinstance(inner, dict):
        if any(
            isinstance(v, str) and len(v) > TOOL_OUTPUT_MAX_CHARS
            for v in inner.values()
        ):
            inner = dict(inner)
            result["result"] = inner
            for k, v in list(inner.items()):
                if isinstance(v, str) and len(v) > TOOL_OUTPUT_MAX_CHARS:
                    inner[k] = (
                        v[:TOOL_OUTPUT_MAX_CHARS]
                        + f"\n... [truncated: {len(v) - TOOL_OUTPUT_MAX_CHARS} chars omitted]"
                    )

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
        ToolContract,
        get_tool_contract,
    )
except ImportError:

    def get_tool_contract(name: str) -> Any:  # type: ignore[misc]
        return None

    class ToolContract:  # type: ignore[no-redef]
        @staticmethod
        def model_validate(obj: Any) -> Any:
            return obj


try:
    from src.core.orchestration.approval_gate import (
        _tool_denied,
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


def _check_read_before_write(
    orch: Any,
    name: str,
    args: dict,
    workspace_guard_tools: frozenset,
) -> Optional[Dict[str, Any]]:
    """Enforce read-before-write for write tools."""
    if name not in workspace_guard_tools:
        return None

    path_arg = args.get("path") or args.get("file_path") or args.get("src_path")
    if not path_arg:
        return None

    try:
        target = Path(orch.working_dir or ".") / path_arg
        resolved_path = str(target.resolve())
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

    path_arg = args.get("path") or args.get("file_path") or args.get("src_path")
    if not path_arg:
        return None

    try:
        affected_files = getattr(orch, "_affected_files", None) or []
        if not affected_files:
            return None

        workdir = str(orch.working_dir or ".").rstrip("/\\")

        def _norm(p: str) -> str:
            p = str(p)
            if p.startswith(workdir + "/") or p.startswith(workdir + "\\"):
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

    if name in ("bash", "run_tests", "run_bash"):
        if _inside(args.get("workdir", workdir_path)):
            return False
        return True

    if name == "delete_file":
        path_arg = args.get("path") or args.get("file_path")
        if path_arg and _inside(workdir_path / str(path_arg)):
            return False
        return True

    return needs_gate


def execute_tool_impl(orch: Any, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Module-level implementation of ``Orchestrator.execute_tool``."""
    name_raw = tool_call.get("name")
    if not isinstance(name_raw, str):
        return {"ok": False, "error": "Tool name must be a string."}

    name = name_raw
    args = tool_call.get("arguments") or {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "Tool arguments must be a mapping."}

    if getattr(orch, "_dry_run", False) and name in DRY_RUN_BLOCKED_TOOLS:
        entry = {"tool": name, "args": args}
        try:
            orch._dry_run_log.append(entry)
        except Exception:
            pass
        return {"status": "dry_run", "would_call": name, "args": args}

    path_arg = args.get("path") or args.get("file_path") or args.get("src_path")
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

    try:
        preflight = orch.preflight_check(tool_call)
    except Exception:
        preflight = {"ok": True}
    if isinstance(preflight, dict) and not preflight.get("ok", True):
        if preflight.get("error") == "tool_not_found":
            return {
                "ok": False,
                "error": f"Tool '{name}' not found.",
                "details": preflight.get("message"),
                "suggestions": preflight.get("suggestions", []),
            }
        return {
            "ok": False,
            "error": preflight.get("message")
            or preflight.get("error")
            or "preflight_failed",
        }

    name = tool_call.get("name", name)
    tool = orch.tool_registry.get(name)
    if not tool:
        return {"ok": False, "error": f"Tool '{name}' not found."}

    try:
        active_agent = getattr(orch, "active_agent", None)
        if active_agent is not None and not active_agent.is_tool_permitted(name):
            _write_permission_audit(
                orch.working_dir, name, args, "deny", "spawn_allowed_tools"
            )
            return {
                "ok": False,
                "error": (
                    f"Tool '{name}' is not permitted for the active delegated agent "
                    "(allowed_tools restriction). Check the agent's allowed_tools list."
                ),
            }
    except Exception:
        pass

    agent_override = _check_agent_permission_override(orch, name, args)
    if agent_override is not None:
        return agent_override

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
        orch.event_bus.publish(
            "tool.execute.start",
            {
                "tool": name,
                "args": {k: str(v)[:200] for k, v in args.items() if k != "content"},
                "tool_call_id": tool_call_id,
            },
        )
    except Exception:
        pass

    try:
        contract = get_tool_contract(name)
        if contract is not None:
            try:
                validated = contract.model_validate(args)
            except ValidationError as ve:
                return {
                    "ok": False,
                    "error": f"Tool call failed contract validation: {ve}",
                }
            if hasattr(validated, "model_dump"):
                args = validated.model_dump()
            elif isinstance(validated, dict):
                args = validated
            else:
                try:
                    args = dict(validated)
                except Exception:
                    pass
    except Exception:
        pass

    path_arg = args.get("path") or args.get("file_path") or args.get("src_path")

    try:
        if "write" in tool.get("side_effects", []) and path_arg:
            new_content = args.get("content")
            if new_content and isinstance(new_content, str):
                if str(path_arg).endswith(".py"):
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
                elif str(path_arg).endswith(
                    (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
                ):
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
                                            "Sandbox validation error: JS/TS syntax "
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
    except Exception as e:
        guilogger.error(f"Sandbox validation failed (fail-closed): {e}")
        return {
            "ok": False,
            "error": f"Sandbox validation aborted: {str(e)}. Write operation blocked for safety.",
        }

    if "write" in tool.get("side_effects", []) and path_arg:
        try:
            if getattr(orch, "_step_snapshot_id", None):
                orch.rollback_manager.append_to_snapshot(
                    orch._step_snapshot_id, path_arg
                )
                orch._current_snapshot_id = orch._step_snapshot_id
            else:
                orch._current_snapshot_id = orch.rollback_manager.snapshot_files(
                    [path_arg], snapshot_id=None
                )
        except Exception as snap_err:
            guilogger.warning(f"Snapshot failed (non-blocking): {snap_err}")

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

    orch_token = None
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        orch_token = _PARENT_ORCHESTRATOR_VAR.set(orch)
    except Exception:
        orch_token = None

    timeout_seconds = orch._get_tool_timeout(name)

    try:
        import concurrent.futures as _cf
        import contextvars as _contextvars
        import inspect as _inspect

        def _run_tool_callable(fn: Any, kwargs: dict) -> Any:
            rv = fn(**kwargs)
            if _inspect.isawaitable(rv):
                import asyncio as _asyncio
                from typing import Coroutine

                return _asyncio.run(cast(Coroutine[Any, Any, Any], rv))
            return rv

        try:
            if timeout_seconds > 0:
                tool_executor = getattr(orch, "_tool_executor", None)
                if tool_executor is None:
                    tool_executor = _cf.ThreadPoolExecutor(
                        max_workers=TOOL_EXECUTOR_MAX_WORKERS
                    )
                    orch._tool_executor = tool_executor
                try:
                    ctx = _contextvars.copy_context()
                    future = tool_executor.submit(
                        ctx.run, _run_tool_callable, tool["fn"], args
                    )
                except Exception:
                    future = tool_executor.submit(_run_tool_callable, tool["fn"], args)
                try:
                    res = future.result(timeout=timeout_seconds)
                except _cf.TimeoutError:
                    future.cancel()
                    raise TimeoutError(
                        f"Tool '{name}' timed out after {timeout_seconds} seconds"
                    )
            else:
                res = _run_tool_callable(tool["fn"], args)
        finally:
            try:
                if orch_token is not None:
                    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

                    _PARENT_ORCHESTRATOR_VAR.reset(orch_token)
            except Exception:
                pass
    except TimeoutError:
        guilogger.warning(f"Tool '{name}' timed out after {timeout_seconds}s")
        return {
            "ok": False,
            "error": (
                f"Tool execution timed out after {timeout_seconds} seconds. "
                "Consider breaking down the task into smaller steps."
            ),
        }
    except Exception as e:
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

        try:
            safe_args = {
                k: str(v) if isinstance(v, Path) else v for k, v in args.items()
            }
            orch.session_store.add_tool_call(
                session_id=getattr(orch, "_current_task_id", None),
                tool_name=name,
                args=safe_args,
                result={"error": str(e)},
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
                    summary=f"tool {name} raised {type(e).__name__}: {str(e)[:100]}",
                    context=str(e)[:400],
                    tool=name,
                )
        except Exception:
            pass

        try:
            from src.core.errors import classify_exception as _classify

            error_code = _classify(e).value
        except Exception:
            error_code = "system.unknown"

        return {"ok": False, "error": str(e), "error_code": error_code}

    res = orch._normalize_tool_result(res)
    res = _truncate_result_strings(res)

    if name in ("plan_enter", "plan_exit") and isinstance(res, dict) and res.get("ok"):
        new_mode = res.get("agent_mode", "execution")
        setattr(orch, "_agent_mode", new_mode)
        try:
            orch.event_bus.publish(
                "agent.mode_changed",
                {"mode": new_mode, "tool": name},
            )
        except Exception:
            pass
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

    try:
        hook_runner = getattr(orch, "_tool_hook_runner", None)
        if hook_runner is not None:
            hook_runner.run_post(name, args, res)
    except Exception:
        pass

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
    except Exception:
        pass

    try:
        import time as _time

        ts = _time.time()
        orch._usage_buffer[name] = orch._usage_buffer.get(name, 0) + 1
        orch.cost_tracker.record_tool_call(name)
        try:
            orch.event_bus.publish(
                "tool.invoked",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "title": name,
                    "status": "invoked",
                    "timestamp": ts,
                    "workdir": str(orch.working_dir),
                },
            )
        except Exception:
            pass
    except Exception:
        pass

    if res.get("status") == "ok" or res.get("ok") is True:
        if name in ["read_file", "fs.read"] and path_arg:
            try:
                resolved_path = str(
                    (Path(orch.working_dir or ".") / path_arg).resolve()
                )
                orch._session_read_files.add(resolved_path)
            except Exception:
                pass
        elif "write" in tool.get("side_effects", []) and path_arg:
            try:
                resolved_path = str(
                    (Path(orch.working_dir or ".") / path_arg).resolve()
                )
                orch._session_modified_files.add(resolved_path)
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

    try:
        orch._append_execution_trace(
            {
                "tool": name,
                "args": orch._normalize_args(args),
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "result_ok": bool(res.get("status") == "ok" or res.get("ok") is True),
            }
        )
    except Exception:
        pass

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
            used = budget.used_tokens
            limit = budget.max_tokens or 32_768
            pct = min(100, int(used / limit * 100)) if limit else 0
            orch.event_bus.publish(
                "token.budget",
                {
                    "used": used,
                    "limit": limit,
                    "percent": pct,
                    "warning": pct >= 80,
                },
            )
    except Exception:
        pass

    try:
        if hasattr(orch, "preview_service") and getattr(
            orch, "_pending_preview_id", None
        ):
            orch.event_bus.publish(
                "preview.pending",
                {"preview_id": orch._pending_preview_id},
            )
    except Exception:
        pass

    try:
        safe_args = {k: str(v) if isinstance(v, Path) else v for k, v in args.items()}
        orch.session_store.add_tool_call(
            session_id=getattr(orch, "_current_task_id", None),
            tool_name=name,
            args=safe_args,
            result=res,
            success=True,
        )
    except Exception:
        pass

    try:
        orch._sync_session_state()
    except Exception:
        pass

    return {"ok": True, "result": res}


def _check_agent_permission_override(
    orch: Any,
    name: str,
    args: dict,
) -> Optional[Dict[str, Any]]:
    """Apply per-agent permission rules; returns error dict if denied."""
    try:
        active_agent = getattr(orch, "active_agent", None)
        if active_agent is not None:
            global_policy = getattr(orch, "permission_policy", None)
            merged_policy = active_agent.get_merged_policy(global_policy)
            if merged_policy is not None and merged_policy.is_denied(name):
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
        pass
    return None


def _run_permission_gate(
    orch: Any,
    name: str,
    args: dict,
    tool_call_id: str,
    needs_gate: bool,
) -> Optional[Dict[str, Any]]:
    """Run the approval gate when needed."""
    if not needs_gate:
        return None

    autonomous = False
    try:
        from src.tools.tools_config import is_autonomous

        autonomous = is_autonomous()
    except Exception:
        pass

    if autonomous:
        return None

    gate_event = None
    try:
        gate_event = register_tool_gate(tool_call_id)
        if name == "delegate_task":
            try:
                orch.event_bus.publish(
                    "spawn.permission_required",
                    {
                        "tool": name,
                        "role": args.get("role", ""),
                        "task": str(args.get("subtask_description", ""))[:200],
                        "tool_id": tool_call_id,
                    },
                )
            except Exception:
                pass
        orch.event_bus.publish(
            "tool.permission_required",
            {
                "tool": name,
                "args": {k: str(v)[:200] for k, v in args.items() if k != "content"},
                "tool_id": tool_call_id,
            },
        )
    except Exception as perm_exc:
        guilogger.warning(f"Permission gate error for '{name}': {perm_exc}")
        return None

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
