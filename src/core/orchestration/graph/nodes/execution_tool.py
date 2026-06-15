"""execution_tool.py — Tool dispatch, tracking, preview, and read-then-write helpers.

Extracted from execution_helpers.py (P3-4) for improved modularity.
"""
from __future__ import annotations


from src.core.messaging.event_types import PlanProgress, StepFinish, StepStart
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


def emit_plan_progress_and_sync_todo(
    *,
    orchestrator: Any,
    state: Mapping[str, Any],
    current_step: int,
    execution_ok: bool,
    plan_progress_event: Optional[Dict[str, Any]],
    manage_todo_fn: Any,
) -> None:
    """Best-effort side effects for plan progress events and TODO completion sync."""
    if not plan_progress_event:
        return

    progress_payload = plan_progress_event["plan_progress"]
    try:
        if hasattr(orchestrator, "event_bus"):
            orchestrator.event_bus.publish_typed(PlanProgress(plan_progress=progress_payload))
    except Exception:
        pass

    if execution_ok:
        try:
            if manage_todo_fn is not None:
                manage_todo_fn(
                    action="check",
                    workdir=str(state.get("working_dir", ".")),
                    step_id=current_step,
                )
        except Exception:
            pass


def emit_execution_step_start(
    *,
    orchestrator: Any,
    state: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    tool_name: str,
    now_monotonic: float,
) -> Dict[str, Any]:
    """Emit the step.start event and return step metadata used by step.finish."""
    step_num = (state.get("current_step") or 0) + 1
    total_steps = len(current_plan) if current_plan else 1
    description = (
        current_plan[current_step].get("description", "")
        if current_plan and current_step < len(current_plan)
        else ""
    )
    try:
        if hasattr(orchestrator, "event_bus"):
            orchestrator.event_bus.publish_typed(StepStart(step=step_num, total=total_steps, tool=tool_name, description=description, session_id=state.get("session_id", "")))
    except Exception:
        pass
    return {
        "step_start_ts": now_monotonic,
        "step_num": step_num,
        "total_steps": total_steps,
    }


def emit_execution_step_finish(
    *,
    orchestrator: Any,
    state: Mapping[str, Any],
    tool_name: str,
    result: Mapping[str, Any],
    step_num: int,
    total_steps: int,
    step_start_ts: float | None,
    now_monotonic: float,
) -> None:
    """Emit the step.finish event after tool execution completes."""
    try:
        elapsed_ms = (
            int((now_monotonic - step_start_ts) * 1000)
            if step_start_ts is not None
            else None
        )
        if hasattr(orchestrator, "event_bus"):
            _ok_flag = result.get("ok")
            step_ok = (_ok_flag is True) or (_ok_flag is None and result.get("status") == "ok")
            orchestrator.event_bus.publish_typed(StepFinish(step=step_num, total=total_steps, tool=tool_name, ok=step_ok, elapsed_ms=elapsed_ms, tool_call_count=int(state.get("tool_call_count") or 0) + 1, session_id=state.get("session_id", "")))
    except Exception:
        pass


def maybe_build_preview_result(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    tool_name: str,
    args: Mapping[str, Any],
    modifying_tools: Sequence[str],
    logger: Any,
    path_cls: Any,
) -> Optional[Dict[str, Any]]:
    """Return preview-mode early result for modifying tools when preview is enabled."""
    if not state.get("preview_mode_enabled", False) or tool_name not in modifying_tools:
        return None

    try:
        preview_service = getattr(orchestrator, "preview_service", None)
        if not preview_service:
            return None

        old_content = None
        new_content = None
        file_path = args.get("path") or args.get("file_path")

        if file_path:
            try:
                file_full_path = path_cls(state["working_dir"]) / file_path
                if file_full_path.exists():
                    old_content = file_full_path.read_text()
            except Exception:
                pass

        if tool_name == "write_file":
            new_content = args.get("content", "")
        elif tool_name in ("edit_file", "edit_by_line_range"):
            new_content = args.get("new_string") or args.get("content", "")

        preview = preview_service.generate_preview(
            tool_name=tool_name,
            args=args,
            old_content=old_content,
            new_content=new_content,
        )

        logger.info(
            "Preview mode: generated preview %s for %s",
            preview.preview_id,
            tool_name,
        )
        return {
            "pending_preview_id": preview.preview_id,
            "awaiting_user_input": True,
            "preview_confirmed": False,
        }
    except Exception as exc:
        logger.warning("Preview mode error: %s, proceeding with execution", exc)
        return None


def handle_read_then_write_success(
    *,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    tool_name: str,
    path_arg: Optional[str],
    working_dir: str,
    truncate_tool_output: Callable[[dict], dict],
    tool_last_used: Mapping[str, Any],
    files_read: Mapping[str, Any],
    build_read_then_write_result_fn: Callable[..., Optional[Dict[str, Any]]],
    logger: Any,
) -> Dict[str, Any]:
    """Handle the successful read-then-write branch and normalize its outputs."""
    verified_update: List[str] = []
    files_read_update = dict(files_read)
    early_result = None

    try:
        read_then_write_result = build_read_then_write_result_fn(
            state=state,
            result=result,
            tool_name=tool_name,
            path_arg=path_arg,
            working_dir=working_dir,
            truncate_tool_output=truncate_tool_output,
            tool_last_used=tool_last_used,
            files_read=files_read,
        )
        if read_then_write_result:
            verified_update = list(read_then_write_result.get("verified_reads") or [])
            files_read_update = dict(
                read_then_write_result.get("files_read") or files_read_update
            )
            if read_then_write_result.get("history"):
                logger.info(
                    "execution_node: read succeeded, task implies modification. Injecting write context via messages: %s",
                    path_arg,
                )
                early_result = read_then_write_result
    except Exception as exc:
        logger.error("execution_node: read-then-write enforcement error: %s", exc)

    return {
        "verified_update": verified_update,
        "files_read_update": files_read_update,
        "early_result": early_result,
    }


def schedule_async_post_tool_hook(
    *,
    orchestrator: Any,
    tool_name: str,
    args: Mapping[str, Any],
    result: Mapping[str, Any],
    ensure_future_fn: Callable[..., Any],
    logger: Any,
) -> None:
    """Fire-and-forget async post-tool hook scheduling with exception logging."""
    try:
        async_hook_runner = getattr(orchestrator, "_tool_hook_runner", None)
        if async_hook_runner is not None and hasattr(
            async_hook_runner, "async_run_post"
        ):
            hook_task = ensure_future_fn(
                async_hook_runner.async_run_post(tool_name, args, result)
            )

            def _log_hook_exc(task: Any) -> None:
                if not task.cancelled() and task.exception() is not None:
                    logger.warning("async_run_post failed: %s", task.exception())

            hook_task.add_done_callback(_log_hook_exc)
    except Exception:
        pass


async def dispatch_execution_tool(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    action: Mapping[str, Any],
    tool_name: str,
    args: Mapping[str, Any],
    get_lock_manager_fn: Callable[[Any], Any],
    execute_with_locks_fn: Callable[..., Any],
    dispatch_tool_fn: Callable[..., Any],
) -> Any:
    """Dispatch tool execution through the lock manager when PRSW execution is active."""
    lock_manager = get_lock_manager_fn(orchestrator)
    exec_model_tier = state.get("model_tier") or ""
    if lock_manager and (state.get("execution_waves") or state.get("plan_dag")):
        agent_id = state.get("session_id") or "main"
        return await execute_with_locks_fn(
            tool_name,
            dict(args),
            lock_manager,
            orchestrator,
            agent_id,
            model_tier=exec_model_tier,
        )
    return await dispatch_tool_fn(orchestrator, dict(action), exec_model_tier)


def update_tool_tracking(
    *,
    state: Mapping[str, Any],
    tool_name: str,
    path_arg: Optional[str],
    max_entries: int = 100,
) -> Tuple[Dict[str, Any], Dict[str, Any], int]:
    tool_last_used = dict(state.get("tool_last_used") or {})
    files_read = dict(state.get("files_read") or {})
    current_count = int(state.get("tool_call_count") or 0)

    cooldown_key = f"{tool_name}:{path_arg or ''}"
    tool_last_used[cooldown_key] = current_count
    if len(tool_last_used) > max_entries:
        sorted_entries = sorted(tool_last_used.items(), key=lambda item: item[1])
        tool_last_used = dict(sorted_entries[-max_entries:])

    return tool_last_used, files_read, current_count
