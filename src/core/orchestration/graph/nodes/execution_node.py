from langchain_core.runnables import RunnableConfig
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Mapping, Dict, Any, Optional

from src.core.orchestration.graph.state import StateLike, validate_state
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.graph.nodes.execution_helpers import (
    build_no_action_result,
    build_execution_return_payload,
    build_read_then_write_result,
    build_tool_history_messages,
    compute_execution_post_tool_updates,
    compute_plan_progress_payload,
    compute_plan_step_updates,
    compute_replan_trigger,
    emit_execution_step_finish,
    emit_execution_step_start,
    emit_plan_progress_and_sync_todo,
    extract_tool_call_from_response,
    dispatch_execution_tool,
    generate_action_for_plan_step,
    handle_execution_preflight_and_role_gate,
    handle_read_then_write_success,
    increment_step_retry_count,
    log_no_action_outcome,
    log_plan_step_execution,
    log_plan_and_wave_advancement,
    log_wave_execution_start,
    maybe_build_execution_cancellation_result,
    maybe_begin_step_transaction,
    maybe_build_preview_result,
    resolve_execution_orchestrator,
    schedule_async_post_tool_hook,
    select_execution_action,
    sync_execution_state_to_orchestrator,
    sync_tool_result_to_ui,
    update_tool_tracking,
    _validate_python_syntax,
    _capture_snapshot,
)
from src.core.orchestration.loop_guards import (
    check_read_before_write,
    check_cooldown,
    check_doom_loop as _guard_doom_loop,
    MODIFYING_TOOLS,  # single source of truth (includes manage_todo — TS-5)
    RECENT_CALLS_WINDOW as _RECENT_CALLS_WINDOW,
)
from src.core.orchestration.event_bus import run_with_correlation
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    TOOL_LARGE_TEXT_FIELDS,
    TOOL_OUTPUT_MAX_BYTES,
    truncate_tool_output,
)

# F-59: hoist manage_todo import to module level (was inline inside plan-step loop).
try:
    from src.tools.todo_tools import manage_todo as _manage_todo
except ImportError:
    _manage_todo = None  # type: ignore[assignment]

from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node

# P3-T3: index refresh after file writes — non-fatal import
try:
    from src.core.indexing.repo_indexer import refresh_file_in_index as _refresh_file_in_index
except ImportError:  # pragma: no cover
    _refresh_file_in_index = None  # type: ignore[assignment]

_FILE_WRITING_TOOLS = frozenset({"write_file", "edit_file", "create_file"})


# Gap 3: Plugin hooks — lazy import so the registry is not required at import time.
try:
    from src.core.plugin.hook_registry import (
        registry as _hook_registry,
        HOOK_TOOL_RESULT as _HOOK_TOOL_RESULT,
    )

    _HAS_HOOKS = True
except Exception:
    _hook_registry = None  # type: ignore[assignment]
    _HOOK_TOOL_RESULT = "tool.result"
    _HAS_HOOKS = False


logger = logging.getLogger(__name__)

# D-11: Named constants — avoids magic numbers scattered through the node body.
_EXECUTION_MAX_PROMPT_TOKENS = 4000  # token budget passed to ContextBuilder

# OP-9: Safety-net cap on serialized tool output entering LLM history.
# Individual tools already truncate internally (bash at ~16KB, read_file at 50K chars);
# this catches edge cases (large glob results, git diffs, combined fields).
_TOOL_OUTPUT_MAX_BYTES = TOOL_OUTPUT_MAX_BYTES
# Fields that typically carry large text payloads, in descending priority for truncation.
_TOOL_LARGE_TEXT_FIELDS = TOOL_LARGE_TEXT_FIELDS


def _truncate_tool_output(res: dict) -> dict:
    """OP-9: Cap any large text fields in a tool result before it enters history."""
    return truncate_tool_output(res, marker_label="OP-9", logger=logger)


# P2-B: Tiers that benefit from offloading execute_tool to a thread pool.
# NANO/SMALL primarily run fast read tools (<100 ms); the threading overhead
# (Task creation, context switch) exceeds the savings.  MEDIUM+ may run bash
# commands, pytest, git operations that block for seconds.
_ASYNC_TOOL_TIERS = frozenset(("medium", "large", "frontier"))


async def _dispatch_tool(
    orchestrator: Any,
    action: Dict[str, Any],
    model_tier: str = "",
) -> Dict[str, Any]:
    """P2-B: Dispatch a synchronous tool call without blocking the event loop.

    For MEDIUM/LARGE/FRONTIER, delegates to ``asyncio.to_thread`` so the event
    loop remains responsive during long-running tools (bash, pytest, git).
    For NANO/SMALL, calls synchronously — the tools are fast enough that
    thread overhead is not worth it.
    """
    if model_tier.lower() in _ASYNC_TOOL_TIERS:
        # Propagate ContextVars (correlation id etc.) into the worker thread
        loop = asyncio.get_running_loop()
        return await run_with_correlation(loop, None, orchestrator.execute_tool, action)
    return orchestrator.execute_tool(action)


async def _execute_tool_with_locks(
    tool_name: str,
    args: Dict,
    lock_manager: Any,
    orchestrator: Any,
    agent_id: str = "main",
    model_tier: str = "",
) -> Dict:
    """
    Execute a tool with file locking for PRSW.
    - Read tools acquire read locks
    - Write tools acquire write locks sequentially
    """
    path_arg = args.get("path") or args.get("file_path")
    files = [path_arg] if path_arg else []

    is_write = tool_name in MODIFYING_TOOLS
    acquired = []

    try:
        for f in files:
            if is_write:
                success = await lock_manager.acquire_write_async(
                    f, agent_id, timeout=30.0
                )
                if not success:
                    return {
                        "ok": False,
                        "error": f"Failed to acquire write lock for {f}",
                    }
            else:
                await lock_manager.acquire_read_async(f, agent_id)
            acquired.append(f)

        # P2-B: offload blocking tool dispatch for MEDIUM+ tiers.
        result = await _dispatch_tool(
            orchestrator, {"name": tool_name, "arguments": args}, model_tier
        )
        # Gap 3: HOOK_TOOL_RESULT — lets plugins observe every tool call outcome.
        if _HAS_HOOKS and _hook_registry is not None:
            try:
                _hook_registry.call(
                    _HOOK_TOOL_RESULT,
                    {"tool_name": tool_name, "args": args, "result": result},
                )
            except Exception:
                pass
        return result

    except Exception as e:
        logger.error(f"_execute_tool_with_locks: error: {e}")
        return {"ok": False, "error": str(e)}

    finally:
        for f in acquired:
            try:
                if is_write:
                    await lock_manager.release_write(f, agent_id)
                else:
                    await lock_manager.release_read(f, agent_id)
            except Exception as release_err:
                logger.error(f"Failed to release lock for {f}: {release_err}")

        if is_write and lock_manager:
            lock_manager.reset_cancel()


async def execution_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """
    Execution Layer: Programmatically enforces Operational Workflows.
    Uses the 'operational' role from ContextBuilder (loaded from agent-brain).
    Dynamic skill injection: If len(relevant_files) > 2, injects 'dry' skill.
    """
    with _span_node("execution", {"step": state.get("current_step", 0)}):
        return await _execution_node_impl(state, config)


async def _execution_node_impl(state: Mapping[str, Any], config: RunnableConfig) -> Dict[str, Any]:  # noqa: C901
    # Validate state invariants at node entry (D-02: non-fatal, logs on issues)
    validate_state(state)

    # Resolve orchestrator first (needed for dynamic cancel_event lookup)
    orchestrator, orchestrator_error = resolve_execution_orchestrator(
        state=state,
        config=config,
        resolve_orchestrator_fn=_resolve_orchestrator,
        logger=logger,
    )
    if orchestrator_error is not None:
        return orchestrator_error

    # Check for cancellation - dynamically resolve from orchestrator if not in state
    cancellation_result = maybe_build_execution_cancellation_result(
        state=state,
        orchestrator=orchestrator,
        logger=logger,
    )
    if cancellation_result is not None:
        return cancellation_result

    # HR-4 fix: planned_action (set by step_controller for the current step) takes priority
    # over next_action (set by perception which may be stale from a prior round).
    # Using next_action first caused stale perception outputs to override freshly computed
    # step actions from the planner/step_controller.
    action = select_execution_action(state=state, logger=logger)

    # Begin step-level atomic transaction for multi-file rollback support.
    # All file writes during this execution will be captured in a single snapshot
    # that can be atomically rolled back by verification_node on failure.
    # Skip if no orchestrator (subagent mode)
    maybe_begin_step_transaction(orchestrator=orchestrator, logger=logger)

    # Handle multi-step plan execution with wave support
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    original_task = state.get("original_task")
    task_decomposed = state.get("task_decomposed", False)
    # Initialize content before any conditional LLM generation so it is always bound.
    content: str = ""

    # Phase A: Wave-based execution support
    execution_waves = state.get("execution_waves")
    current_wave = state.get("current_wave") or 0
    wave_advance = {}  # Initialize for all code paths

    log_wave_execution_start(
        execution_waves=execution_waves,
        current_wave=current_wave,
        logger=logger,
    )

    # If we have a plan but no action, we need to generate one for the current step
    if not action and current_plan and current_step < len(current_plan):
        try:
            from src.core.orchestration.provider_capabilities import resolve_provider_capabilities as _resolve_pc

            tool_call, current_plan, content, early_result = await generate_action_for_plan_step(
                state=state,
                orchestrator=orchestrator,
                current_plan=current_plan,
                current_step=current_step,
                original_task=original_task,
                execution_max_prompt_tokens=_EXECUTION_MAX_PROMPT_TOKENS,
                context_builder_cls=ContextBuilder,
                resolve_provider_capabilities=_resolve_pc,
                call_model_fn=call_model,
                parse_tool_block=parse_tool_block,
                extract_tool_call_from_response_fn=extract_tool_call_from_response,
                logger=logger,
            )
            if early_result is not None:
                return early_result
            if tool_call:
                action = tool_call
        except Exception as e:
            logger.error(f"Failed to generate tool for step: {e}")
            _notify_provider_limit(str(e))

    # If we have a plan and haven't finished it, check if we need to advance
    if current_plan and current_step < len(current_plan):
        log_plan_step_execution(
            current_plan=current_plan,
            current_step=current_step,
            task_decomposed=bool(task_decomposed),
            original_task=original_task,
            logger=logger,
        )

    if not action:
        no_action_result = build_no_action_result(
            content=content,
            action=action,
            state=state,
            current_plan=current_plan,
            current_step=current_step,
            original_task=original_task,
            inc_step_retry=increment_step_retry_count,
        )
        log_no_action_outcome(content=content, logger=logger, regex_module=re)
        return no_action_result

    tool_name = action["name"]
    args = action.get("arguments", {})
    path_arg = args.get("path") or args.get("file_path")

    # ORCH-02: read-before-write guard delegated to loop_guards.check_read_before_write()
    _session_read = set()
    if orchestrator and hasattr(orchestrator, "_session_read_files"):
        _session_read = orchestrator._session_read_files
    _rbw_err = check_read_before_write(
        tool_name, path_arg, state, state.get("working_dir", "."), _session_read
    )
    if _rbw_err:
        return _rbw_err

    # P2-T2: Python syntax gate — reject write_file calls that would produce
    # syntactically invalid .py files before the write reaches the filesystem.
    # Runs after the read-before-write check so RBW errors take precedence.
    if tool_name == "write_file" and path_arg:
        _py_content = args.get("content", "")
        _syntax_err = _validate_python_syntax(_py_content, path_arg)
        if _syntax_err:
            _syntax_err_payload = json.dumps(
                {"tool_execution_result": {"ok": False, "error": _syntax_err}}
            )
            return {
                "last_result": {"ok": False, "error": _syntax_err},
                "history": list(state.get("history", []))
                + [{"role": "user", "content": _syntax_err_payload}],
                "next_action": None,
            }

    # ORCH-02: cooldown guard delegated to loop_guards.check_cooldown()
    _cooldown_err = check_cooldown(tool_name, args, state)
    if _cooldown_err:
        return _cooldown_err

    # ORCH-01/PERM-02: doom-loop detection + permission gate delegated to loop_guards.
    _ev_bus = getattr(orchestrator, "event_bus", None) if orchestrator else None
    # MED-17 fix: initialise _recent_calls so it is always bound even if
    # _guard_doom_loop raises an exception before returning its second value.
    _recent_calls: list = []
    _doom_err, _recent_calls = _guard_doom_loop(tool_name, args, state, _ev_bus)
    if _doom_err:
        return {**_doom_err, "recent_tool_calls": _recent_calls}

    preflight_gate_result = handle_execution_preflight_and_role_gate(
        state={**dict(state), "_modifying_tools": tuple(MODIFYING_TOOLS)},
        config=config,
        orchestrator=orchestrator,
        action=action,
        tool_name=tool_name,
        args=args,
        logger=logger,
    )
    if preflight_gate_result is not None:
        return preflight_gate_result

    preview_result = maybe_build_preview_result(
        state=state,
        orchestrator=orchestrator,
        tool_name=tool_name,
        args=args,
        modifying_tools=tuple(MODIFYING_TOOLS),
        logger=logger,
        path_cls=Path,
    )
    if preview_result is not None:
        return preview_result

    # P4-4/GAP-S2: propagate execution-scoped enforcement state before tool execution.
    sync_execution_state_to_orchestrator(state=state, orchestrator=orchestrator)

    # Step boundary event: step.start — mirrors opencode's StepStartPart.
    # Emitted just before tool execution so TUI can show "running..." state.
    import time as _time_mod

    _step_meta = emit_execution_step_start(
        orchestrator=orchestrator,
        state=state,
        current_plan=current_plan,
        current_step=current_step,
        tool_name=tool_name,
        now_monotonic=_time_mod.monotonic(),
    )

    # P3-T5: Capture pre-write snapshot before the tool modifies the file
    _pre_write_snapshot: Optional[str] = None
    if tool_name in _FILE_WRITING_TOOLS:
        _snap_path_arg = args.get("path") or args.get("file_path") or args.get("filename")
        _snap_working_dir = (
            orchestrator.working_dir
            if orchestrator and hasattr(orchestrator, "working_dir")
            else None
        )
        if _snap_path_arg and _snap_working_dir:
            _pre_write_snapshot = _capture_snapshot(_snap_path_arg, _snap_working_dir)

    try:
        res = await dispatch_execution_tool(
            state=state,
            orchestrator=orchestrator,
            action=action,
            tool_name=tool_name,
            args=args,
            get_lock_manager_fn=lambda orch: orch.get_file_lock_manager()
            if orch and hasattr(orch, "get_file_lock_manager")
            else None,
            execute_with_locks_fn=_execute_tool_with_locks,
            dispatch_tool_fn=_dispatch_tool,
        )
    except asyncio.CancelledError:
        raise

    schedule_async_post_tool_hook(
        orchestrator=orchestrator,
        tool_name=tool_name,
        args=args,
        result=res,
        ensure_future_fn=asyncio.ensure_future,
        logger=logger,
    )

    emit_execution_step_finish(
        orchestrator=orchestrator,
        state=state,
        tool_name=tool_name,
        result=res,
        step_num=_step_meta["step_num"],
        total_steps=_step_meta["total_steps"],
        step_start_ts=_step_meta["step_start_ts"],
        now_monotonic=_time_mod.monotonic(),
    )

    # _recent_calls already has the current fingerprint appended by _guard_doom_loop
    # (it returns updated_recent in both the error and non-error paths).
    # Trim to window just in case extra entries accumulated.
    _recent_calls = _recent_calls[-_RECENT_CALLS_WINDOW:]

    # UI Sync: Forward tool result to TUI so user can see execution result
    sync_tool_result_to_ui(orchestrator=orchestrator, result=res, logger=logger, tool_name=tool_name)

    # P3-T3: Keep repo index fresh after file-writing tools
    if _refresh_file_in_index is not None and tool_name in _FILE_WRITING_TOOLS:
        _written_path = args.get("path") or args.get("file_path") or args.get("filename")
        _working_dir = (
            orchestrator.working_dir
            if orchestrator and hasattr(orchestrator, "working_dir")
            else None
        )
        if _written_path and _working_dir:
            _refresh_file_in_index(_written_path, _working_dir)

    # Successful tool execution
    verified_update = []
    plan_advance = {}
    _tool_last_used_update, _files_read_update, _current_count = update_tool_tracking(
        state=state,
        tool_name=tool_name,
        path_arg=path_arg,
    )

    # Check for multi-step plan completion
    plan_advance, wave_advance = compute_plan_step_updates(
        result=res,
        current_plan=current_plan,
        current_step=current_step,
        original_task=original_task,
        execution_waves=execution_waves,
        current_wave=current_wave,
        step_retry_counts=state.get("step_retry_counts") or {},
    )
    log_plan_and_wave_advancement(
        plan_advance=plan_advance,
        wave_advance=wave_advance,
        current_plan=current_plan,
        current_step=current_step,
        execution_waves=execution_waves,
        current_wave=current_wave,
        logger=logger,
    )

    # Check if execution was successful (handle both {"ok": True} and {"status": "ok"} formats)
    execution_ok = res.get("ok") or res.get("status") == "ok"
    if execution_ok:
        # Log failure for debugging if needed
        actual_res = res.get("result", {})
        if tool_name == "edit_file" and actual_res.get("status") != "ok":
            logging.getLogger("coding_agent").info(f"PATCH FAILED: {actual_res}")

        verified_update = []
        read_then_write_update = handle_read_then_write_success(
            state=state,
            result=res,
            tool_name=tool_name,
            path_arg=path_arg,
            working_dir=str(state.get("working_dir", ".")),
            truncate_tool_output=_truncate_tool_output,
            tool_last_used=_tool_last_used_update,
            files_read=_files_read_update,
            build_read_then_write_result_fn=build_read_then_write_result,
            logger=logger,
        )
        verified_update = list(read_then_write_update["verified_update"] or [])
        _files_read_update = dict(read_then_write_update["files_read_update"])
        if read_then_write_update["early_result"] is not None:
            return read_then_write_update["early_result"]

        # Verified reads are tracked by execute_tool and other lower-level
        # components; avoid duplicating the add here. This keeps RBW updates
        # consolidated and reduces redundant work.

    # FIX: Return ONLY the new message as "user" role so ContextBuilder doesn't filter it out.
    # The ContextBuilder filters out non-user/assistant roles, so tool results need to be "user".
    # Also, we must NOT mutate the existing history list in place - LangGraph will duplicate it!
    # OP-9: Cap the serialized result at 50 KB before it enters LLM context.
    new_messages = build_tool_history_messages(
        result=res,
        truncate_tool_output=_truncate_tool_output,
    )

    # Phase 2: Patch Size Guard - Intercept requires_split flag
    replan_triggered = compute_replan_trigger(result=res)
    if replan_triggered:
        logger.warning(
            f"execution_node: patch too large, triggering replan - {replan_triggered.get('replan_required')}"
        )

    # Publish plan.progress event for UI dashboard (GAP 2: ACP sessionUpdate schema)
    plan_progress_event = compute_plan_progress_payload(
        state=state,
        current_plan=current_plan,
        current_step=current_step,
        execution_ok=bool(execution_ok),
    )
    emit_plan_progress_and_sync_todo(
        orchestrator=orchestrator,
        state=state,
        current_step=current_step,
        execution_ok=bool(execution_ok),
        plan_progress_event=plan_progress_event,
        manage_todo_fn=_manage_todo,
    )

    post_tool_updates = compute_execution_post_tool_updates(
        state=state,
        orchestrator=orchestrator,
        tool_name=tool_name,
        result=res,
        modifying_tools=tuple(MODIFYING_TOOLS),
    )
    # Keep the consumed approval reset explicit at the node boundary so the
    # execution contract remains source-visible and unambiguous.
    if not post_tool_updates["plan_approval_consumed"] and (
        state.get("plan_mode_approved")
        and tool_name in MODIFYING_TOOLS
        and (res.get("ok") or res.get("status") == "ok")
    ):
        post_tool_updates["plan_approval_consumed"] = {"plan_mode_approved": False}

    return build_execution_return_payload(
        result=res,
        tool_name=tool_name,
        verified_reads=verified_update,
        history=new_messages,
        tool_call_count=post_tool_updates["tool_call_count"],
        tool_last_used=_tool_last_used_update,
        files_read=_files_read_update,
        recent_tool_calls=_recent_calls,
        plan_advance=plan_advance,
        wave_advance=wave_advance,
        replan_triggered=replan_triggered,
        plan_progress_event=plan_progress_event,
        plan_approval_consumed=post_tool_updates["plan_approval_consumed"],
        no_plan_fail_update=post_tool_updates["no_plan_fail_update"],
        affected_files_update=post_tool_updates["affected_files_update"],
        plan_exit_update=post_tool_updates["plan_exit_update"],
    ) | (
        # P3-T5: append pre-write snapshot path to state["snapshots"]
        {"snapshots": (list(state.get("snapshots") or [])) + [_pre_write_snapshot]}
        if _pre_write_snapshot
        else {}
    )
