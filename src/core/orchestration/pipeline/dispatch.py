"""Tool execution pipeline — Stage 3: Threaded dispatch, timeouts & cancellation."""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import contextvars
import inspect
import logging
import os
import signal
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Coroutine, Dict, Optional, cast

from src.core.messaging.event_types import ToolExecuteError

logger = logging.getLogger("src.core.orchestration.tool_execution_pipeline")

TOOL_EXECUTOR_MAX_WORKERS: int = 5

_TOOL_EXECUTOR_LOCK: threading.Lock = threading.Lock()
_CURRENT_TOOL_CANCEL_EVENT: ContextVar[Optional[threading.Event]] = ContextVar(
    "_CURRENT_TOOL_CANCEL_EVENT", default=None
)
_THREAD_CANCEL_EVENTS: Dict[int, threading.Event] = {}
_THREAD_CANCEL_LOCK: threading.Lock = threading.Lock()
_CURRENT_TOOL_PROCESS: ContextVar[Optional[Any]] = ContextVar(
    "_CURRENT_TOOL_PROCESS", default=None
)
_THREAD_TOOL_PROCESSES: Dict[int, Any] = {}
_THREAD_PROCESS_LOCK: threading.Lock = threading.Lock()


def register_current_tool_process(proc: Any) -> None:
    """Register an active subprocess for the current tool execution for timeout cleanup."""
    try:
        _CURRENT_TOOL_PROCESS.set(proc)
    except Exception:
        pass
    tid = threading.get_ident()
    with _THREAD_PROCESS_LOCK:
        _THREAD_TOOL_PROCESSES[tid] = proc


def unregister_current_tool_process(proc: Any = None) -> None:
    """Unregister the active subprocess for the current worker thread."""
    tid = threading.get_ident()
    with _THREAD_PROCESS_LOCK:
        _THREAD_TOOL_PROCESSES.pop(tid, None)


def _kill_proc_group(proc: Any) -> None:
    """Kill a process and its process group (``os.killpg``)."""
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
    return ev is not None and ev.is_set()


def _timeout_result(name: str, timeout_seconds: float) -> Dict[str, Any]:
    """Build the structured result for a tool that exceeded its timeout (P3.4)."""
    base: Dict[str, Any] = {
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


def _inject_confined_workdir(orch: Any, fn: Any, args: dict) -> tuple[Optional[Dict[str, Any]], dict]:
    """Inject and confine ``workdir`` / ``working_dir`` arguments when accepted by *fn*."""
    from src.core.orchestration.pipeline.permissions import get_effective_workdir

    effective_wd = get_effective_workdir(orch)
    if not effective_wd:
        return None, args
    try:
        sig = inspect.signature(fn)
        target_workdir = Path(effective_wd).resolve()

        def _is_confined(p: Any) -> bool:
            try:
                resolved = Path(p).resolve()
                return resolved == target_workdir or str(resolved).startswith(
                    str(target_workdir) + "/"
                )
            except Exception:
                return False

        if "workdir" in sig.parameters:
            args = dict(args)
            if args.get("workdir"):
                if not _is_confined(args["workdir"]):
                    return {
                        "ok": False,
                        "error": f"Path '{args['workdir']}' is outside working directory.",
                    }, args
                args["workdir"] = Path(args["workdir"]).resolve()
            else:
                args["workdir"] = target_workdir

        if "working_dir" in sig.parameters:
            args = dict(args)
            if args.get("working_dir"):
                if not _is_confined(args["working_dir"]):
                    return {
                        "ok": False,
                        "error": f"Path '{args['working_dir']}' is outside working directory.",
                    }, args
                args["working_dir"] = str(Path(args["working_dir"]).resolve())
            else:
                args["working_dir"] = str(target_workdir)
    except (ValueError, TypeError):
        pass
    return None, args


def _dispatch_tool_call(
    orch: Any,
    name: str,
    args: dict,
    tool: Any,
    tool_call_id: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """Run the tool callable (with timeout, MCP loop routing & cancel tracking)."""
    wd_err, args = _inject_confined_workdir(orch, tool["fn"], args)
    if wd_err is not None:
        return wd_err

    cancel_ev = threading.Event()
    cancel_token = _CURRENT_TOOL_CANCEL_EVENT.set(cancel_ev)

    def _run_tool_callable(fn: Any, kwargs: dict) -> Any:
        tid = threading.get_ident()
        with _THREAD_CANCEL_LOCK:
            _THREAD_CANCEL_EVENTS[tid] = cancel_ev
        try:
            rv = fn(**kwargs)
            if inspect.isawaitable(rv):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                # TASK-21: Dispatch MCP async tool calls onto the persistent
                # event loop that owns the MCP client transport.
                target_loop = getattr(fn, "_mcp_loop", None)
                if (
                    target_loop is not None
                    and not target_loop.is_closed()
                    and target_loop.is_running()
                    and target_loop is not loop
                ):
                    return asyncio.run_coroutine_threadsafe(
                        cast(Coroutine[Any, Any, Any], rv), target_loop
                    ).result()

                if loop is not None and loop.is_running():
                    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(
                            asyncio.run, cast(Coroutine[Any, Any, Any], rv)
                        ).result()
                return asyncio.run(cast(Coroutine[Any, Any, Any], rv))
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
                ctx = contextvars.copy_context()
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
        orch.event_bus.publish_typed(
            ToolExecuteError(
                session_update="tool_call_update",
                tool_call_id=tool_call_id,
                title=name,
                status="failed",
                content=[{"type": "text", "text": str(exc)}],
                error=str(exc),
                workdir=str(orch.working_dir),
            )
        )
    except Exception:
        pass

    try:
        safe_args = {k: str(v) if isinstance(v, Path) else v for k, v in args.items()}
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
