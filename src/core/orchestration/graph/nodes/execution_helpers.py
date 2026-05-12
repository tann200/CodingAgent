from __future__ import annotations

import ast as _ast
import asyncio
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# P3-T6: Guard/validation helpers extracted to execution_guards.py — re-exported here
# for backwards compatibility so all existing callers continue to work unchanged.
from src.core.orchestration.graph.nodes.execution_guards import (  # noqa: F401
    _validate_python_syntax,
    _capture_snapshot,
)


def extract_tool_call_from_response(
    response: Any,
    *,
    parse_tool_block: Callable[[str], Optional[dict]],
    logger: Any = None,
) -> Tuple[str, Optional[List[Any]], Optional[dict]]:
    content = ""
    tool_calls = None

    if isinstance(response, dict):
        choices = response.get("choices")
        if choices and len(choices) > 0:
            choice_message = (
                choices[0].get("message") if isinstance(choices[0], dict) else None
            )
            if isinstance(choice_message, dict):
                content = choice_message.get("content") or ""
                tool_calls = choice_message.get("tool_calls")
        elif response.get("message"):
            message = response.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                tool_calls = message.get("tool_calls")

    tool_call = None
    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
        first_tool_call = tool_calls[0]
        if isinstance(first_tool_call, dict):
            func = first_tool_call.get("function")
            if func:
                name = func.get("name")
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if name:
                    tool_call = {"name": name, "arguments": args or {}}
                    if logger is not None:
                        try:
                            logger.info(
                                "execution_node: native function call: %s",
                                name,
                            )
                        except Exception:
                            pass
    if not tool_call:
        tool_call = parse_tool_block(content)

    return content, tool_calls, tool_call


def build_no_action_result(
    *,
    content: str,
    action: Any,
    state: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    original_task: Optional[str],
    inc_step_retry: Callable[[Mapping[str, Any], int], dict],
) -> Optional[Dict[str, Any]]:
    if action:
        return None

    content_lower = (content or "").lower()

    if content is not None and content.strip() == "" and not action:
        return {
            "last_result": {
                "ok": False,
                "error": "empty_model_response: model produced no content. "
                "The conversation history may be too long for the context window. "
                "Try breaking the task into smaller steps.",
            },
            "step_retry_counts": inc_step_retry(state, current_step),
            "history": [],
        }

    if re.search(r"status\s*:\s*complete", content_lower):
        updated_plan = [dict(step) for step in current_plan] if current_plan else []
        if updated_plan and current_step < len(updated_plan):
            updated_plan[current_step]["completed"] = True
            next_step = current_step + 1
            plan_advance: Dict[str, Any] = {
                "current_step": next_step,
                "current_plan": updated_plan,
            }
            if next_step < len(updated_plan):
                plan_advance["task"] = updated_plan[next_step].get("description", "")
            else:
                plan_advance["task"] = original_task or "Task complete"
        else:
            plan_advance = {}

        history_entry = [{"role": "assistant", "content": content}] if content else []
        return {
            "last_result": {
                "ok": True,
                "status": "ok",
                "completed_without_tool": True,
            },
            "history": history_entry,
            **plan_advance,
        }

    return {
        "last_result": {"ok": False, "error": "format_error: no tool call emitted"},
        "step_retry_counts": inc_step_retry(state, current_step),
    }


def increment_step_retry_count(state: Mapping[str, Any], current_step: int) -> dict:
    """Return step retry counts with the current step incremented.

    Keys are normalized to strings so persisted state and routing logic use one
    canonical representation. Legacy int-keyed state is folded into the same
    string key during normalization.
    """
    raw = state.get("step_retry_counts") or {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        try:
            counts[str(int(key))] = int(value)
        except (ValueError, TypeError):
            try:
                counts[str(key)] = int(value)
            except (ValueError, TypeError):
                continue
    step_key = str(current_step)
    counts[step_key] = counts.get(step_key, 0) + 1
    return counts


def resolve_execution_orchestrator(
    *,
    state: Mapping[str, Any],
    config: Any,
    resolve_orchestrator_fn: Callable[[Mapping[str, Any], Any], Any],
    logger: Any,
) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """Resolve the execution orchestrator and normalize subagent/config errors."""
    try:
        orchestrator = resolve_orchestrator_fn(state, config)
        if orchestrator is not None:
            return orchestrator, None

        config_has_orchestrator_field = False
        if config and isinstance(config, dict):
            cfg = config.get("configurable") or config
            if cfg is not None and "orchestrator" in cfg:
                config_has_orchestrator_field = True

        if config_has_orchestrator_field:
            logger.info("execution_node: subagent mode (orchestrator=None in config)")
            return None, None

        logger.error("execution_node: orchestrator is None")
        return None, {
            "last_result": None,
            "errors": ["orchestrator not found"],
        }
    except Exception as exc:
        logger.error("execution_node: failed to get orchestrator: %s", exc)
        return None, {
            "last_result": None,
            "errors": [f"config error: {exc}"],
        }


def maybe_build_execution_cancellation_result(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    logger: Any,
) -> Optional[Dict[str, Any]]:
    """Return the standard canceled payload when a cancel event is set."""
    cancel_event = state.get("cancel_event")
    if not cancel_event:
        cancel_event = getattr(orchestrator, "cancel_event", None)
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled by user")
        return {
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
            "next_action": None,
        }
    return None


def select_execution_action(*, state: Mapping[str, Any], logger: Any) -> Any:
    """Select the current execution action, preferring planned_action over next_action."""
    try:
        return state.get("planned_action") or state.get("next_action")
    except Exception as exc:
        logger.error("execution_node: failed to get next_action: %s", exc)
        return None


def maybe_begin_step_transaction(*, orchestrator: Any, logger: Any) -> None:
    """Begin a step transaction when supported, logging non-fatal failures."""
    try:
        if orchestrator and hasattr(orchestrator, "begin_step_transaction"):
            orchestrator.begin_step_transaction()
            logger.debug("execution_node: step transaction started")
    except Exception as exc:
        logger.debug(
            "execution_node: step transaction init failed (non-fatal): %s",
            exc,
        )


def log_wave_execution_start(
    *,
    execution_waves: Optional[Sequence[Any]],
    current_wave: int,
    logger: Any,
) -> None:
    """Log the current wave size when execution is operating in wave mode."""
    if execution_waves and current_wave < len(execution_waves):
        wave_steps = execution_waves[current_wave]
        logger.info(
            "Wave execution: wave %d/%d with %d steps",
            current_wave + 1,
            len(execution_waves),
            len(wave_steps),
        )


def log_plan_and_wave_advancement(
    *,
    plan_advance: Mapping[str, Any],
    wave_advance: Mapping[str, Any],
    current_plan: Optional[Sequence[Any]],
    current_step: int,
    execution_waves: Optional[Sequence[Any]],
    current_wave: int,
    logger: Any,
) -> None:
    """Log human-readable advancement for plan steps and execution waves."""
    if plan_advance:
        if current_plan and plan_advance.get("current_step", 0) < len(
            plan_advance.get("current_plan") or []
        ):
            logger.info(
                "Step %d complete, advancing to step %d",
                current_step + 1,
                plan_advance["current_step"] + 1,
            )
        else:
            logger.info("All plan steps completed")

    if wave_advance:
        if wave_advance.get("current_wave", 0) < len(execution_waves or []):
            logger.info(
                "Wave %d complete, advancing to wave %d",
                current_wave + 1,
                wave_advance["current_wave"] + 1,
            )
        else:
            logger.info("All waves completed")


def log_plan_step_execution(
    *,
    current_plan: Optional[Sequence[Mapping[str, Any]]],
    current_step: int,
    task_decomposed: bool,
    original_task: Optional[str],
    logger: Any,
) -> None:
    """Log the current plan step when executing a decomposed task."""
    if not current_plan or current_step >= len(current_plan):
        return
    if not task_decomposed or not original_task:
        return

    current_step_desc = current_plan[current_step].get("description", "")
    logger.info(
        "Plan execution: step %d/%d - %s",
        current_step + 1,
        len(current_plan),
        current_step_desc,
    )


def sync_tool_result_to_ui(
    *, orchestrator: Any, result: Mapping[str, Any], logger: Any
) -> None:
    """Best-effort append of the latest tool result into UI-visible history."""
    if not orchestrator or not hasattr(orchestrator, "msg_mgr"):
        return
    try:
        orchestrator.msg_mgr.append(
            "user", json.dumps({"tool_execution_result": result})
        )
    except Exception as exc:
        logger.debug("UI sync failed: %s", exc)


def log_no_action_outcome(*, content: str, logger: Any, regex_module: Any = re) -> None:
    """Log the reason a no-action result was returned."""
    if content is not None and content.strip() == "":
        logger.warning(
            "execution_node: model produced empty content with no tool call — "
            "context window may be full or model failed to generate output"
        )
    elif regex_module.search(r"status\s*:\s*complete", (content or "").lower()):
        logger.info(
            "execution_node: model declared STATUS: complete with no tool call — "
            "treating as successful step completion"
        )


def sync_execution_state_to_orchestrator(
    *, state: Mapping[str, Any], orchestrator: Any
) -> None:
    """Propagate execution-scoped state needed by downstream tool enforcement."""
    if not orchestrator:
        return
    orchestrator._plan_mode_approved = state.get("plan_mode_approved")
    orchestrator._affected_files = list(state.get("affected_files") or [])


async def generate_action_for_plan_step(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    current_plan: List[Dict[str, Any]],
    current_step: int,
    original_task: Optional[str],
    execution_max_prompt_tokens: int,
    context_builder_cls: Any,
    resolve_provider_capabilities: Callable[[Any], Mapping[str, Any]],
    call_model_fn: Any,
    parse_tool_block: Callable[[str], Optional[dict]],
    extract_tool_call_from_response_fn: Callable[
        ..., Tuple[str, Optional[List[Any]], Optional[dict]]
    ],
    logger: Any,
) -> Tuple[Optional[dict], List[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
    """Best-effort LLM generation of a tool call for the current plan step."""
    current_step_desc = current_plan[current_step].get("description", "")
    logger.info("No action provided, generating tool for step: %s", current_step_desc)

    if not orchestrator or not getattr(orchestrator, "tool_registry", None):
        raise RuntimeError(
            "execution_node: orchestrator or tool_registry unavailable for LLM step generation"
        )

    builder = context_builder_cls(working_dir=state.get("working_dir"))
    tools_list = [
        {"name": name, "description": meta.get("description", "")}
        for name, meta in orchestrator.tool_registry.tools.items()
    ]

    active_skills = []
    relevant_files = state.get("relevant_files") or []
    if len(relevant_files) > 2:
        active_skills.append("dry")
        logger.info("execution_node: injected DRY skill due to many relevant files")

    step_prompt = (
        f"Execute this specific step: {current_step_desc}\n\n"
        f"Working directory: {state.get('working_dir')}\n"
        f"Original task: {original_task or state.get('task')}\n\n"
        "Generate the appropriate tool call to complete this step. "
        "Respond with ONLY a JSON function call (no YAML)."
    )

    provider_capabilities = resolve_provider_capabilities(orchestrator)
    messages = builder.build_prompt(
        role_name="operational",
        active_skills=active_skills,
        task_description=step_prompt,
        tools=tools_list,
        conversation=state.get("history", []),
        max_tokens=execution_max_prompt_tokens,
        provider_capabilities=provider_capabilities,
        model_tier=state.get("model_tier"),
        model_name=provider_capabilities.get("model") or "",
    )

    cancel_event = state.get("cancel_event") or getattr(
        orchestrator, "cancel_event", None
    )
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled before LLM call")
        return (
            None,
            current_plan,
            "",
            {
                "last_result": {"ok": False, "error": "Task canceled by user"},
                "next_action": None,
                "errors": ["canceled"],
            },
        )

    functions = None
    if orchestrator and hasattr(orchestrator.tool_registry, "get_openai_functions"):
        functions = orchestrator.tool_registry.get_openai_functions()

    exec_model_override = state.get("override_model") or None
    if not exec_model_override:
        try:
            from src.core.config_loader import get_model_for_role as _gmfr

            exec_model_override = _gmfr("operational")
        except Exception:
            pass

    exec_llm_timeout: int | None = 120
    try:
        from src.core.orchestration.project_settings import (
            get_active_settings as _gas_exec,
        )

        ps_exec = _gas_exec()
        if ps_exec is not None:
            exec_llm_timeout = ps_exec.max_llm_wait_seconds or None
    except Exception:
        pass

    try:
        call_coro = call_model_fn(
            messages,
            stream=False,
            format_json=False,
            tools=functions,
            temperature=0.0,
            session_id=state.get("session_id"),
            **({"model": exec_model_override} if exec_model_override else {}),
        )
        if exec_llm_timeout:
            resp = await asyncio.wait_for(
                call_coro, timeout=exec_llm_timeout
            )
        else:
            resp = await call_coro

    except asyncio.TimeoutError:
        logger.warning(
            "execution_node: LLM call timed out after %ss",
            exec_llm_timeout,
        )
        return (
            None,
            current_plan,
            "",
            {
                "last_result": {
                    "ok": False,
                    "error": f"LLM call timed out after {exec_llm_timeout}s",
                },
                "next_action": "wait_for_user",
                "errors": [f"llm_timeout:{exec_llm_timeout}s"],
            },
        )

    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled after LLM call")
        return (
            None,
            current_plan,
            "",
            {
                "last_result": {"ok": False, "error": "Task canceled by user"},
                "next_action": None,
                "errors": ["canceled"],
            },
        )

    if isinstance(resp, dict) and resp.get("context_overflow"):
        raw_hist = list(state.get("history") or [])
        trunc_hist = raw_hist[-6:] if len(raw_hist) > 6 else raw_hist
        logger.warning(
            "execution_node: context overflow detected — truncating history %d → %d messages; errors=['context_overflow'] will route to memory_sync",
            len(raw_hist),
            len(trunc_hist),
        )
        return (
            None,
            current_plan,
            "",
            {
                "history": [],
                "_compacted_history": trunc_hist,
                "next_action": None,
                "_budget_compaction": True,
                "_should_distill": True,
                "errors": ["context_overflow"],
                "last_result": {
                    "ok": False,
                    "error": "Context window overflow in execution — history truncated, compaction triggered",
                },
            },
        )

    content, _tool_calls, tool_call = extract_tool_call_from_response_fn(
        resp,
        parse_tool_block=parse_tool_block,
        logger=logger,
    )
    updated_plan = current_plan
    if tool_call:
        logger.info("Generated tool call for step: %s", tool_call)
        updated_plan = [dict(step) for step in current_plan]
        updated_plan[current_step]["action"] = tool_call

    return tool_call, updated_plan, content, None


def handle_execution_preflight_and_role_gate(
    *,
    state: Mapping[str, Any],
    config: Any,
    orchestrator: Any,
    action: Mapping[str, Any],
    tool_name: str,
    args: Mapping[str, Any],
    logger: Any,
) -> Optional[Dict[str, Any]]:
    """Return an early payload when sandbox, role, or plan-mode gates block execution."""
    if not orchestrator:
        return {
            "last_result": {
                "ok": False,
                "error": "Orchestrator required for tool execution",
            },
            "errors": ["orchestrator not available"],
        }

    preflight = orchestrator.preflight_check(action)
    if not preflight.get("ok"):
        tool_not_found = "not found" in (preflight.get("error") or "").lower()
        prev_result = state.get("last_result") or {}
        prev_ok = prev_result.get("ok") or prev_result.get("status") == "ok"
        if tool_not_found and prev_ok and (state.get("rounds") or 0) >= 1:
            logger.info(
                "route_execution: tool %r not found but task already completed — treating as completion signal",
                action.get("name"),
            )
            synthetic_result = json.dumps(
                {
                    "tool_execution_result": {
                        "tool_name": action.get("name", "respond"),
                        "output": "Task already completed. No further action needed.",
                        "status": "ok",
                    }
                }
            )
            return {
                "last_result": {**prev_result, "_completion_detected": True},
                "history": [{"role": "user", "content": synthetic_result}],
                "next_action": None,
            }

        error_content = f"[SANDBOX VIOLATION] {preflight.get('error')}"
        try:
            orchestrator.msg_mgr.append("user", error_content)
        except Exception as exc:
            logger.error(
                "Failed to append sandbox violation to orchestrator history: %s",
                exc,
            )

        return {
            "last_result": preflight,
            "history": [{"role": "user", "content": error_content}],
            "next_action": None,
        }

    if state.get("plan_mode_enabled", False) and tool_name in state.get(
        "_modifying_tools", ()
    ):
        if not state.get("plan_mode_approved", False):
            plan_mode = getattr(orchestrator, "plan_mode", None)
            if plan_mode is None:
                from src.core.orchestration.plan_mode import PlanMode

                plan_mode = PlanMode(orchestrator)
            if plan_mode.is_blocked(tool_name):
                if not plan_mode.pending_plan:
                    plan_mode.set_pending_plan(
                        {
                            "plan": state.get("current_plan"),
                            "blocked_tool": tool_name,
                            "args": dict(args),
                        }
                    )
                blocked_msg = (
                    f"Plan Mode: tool '{tool_name}' is blocked pending plan approval. "
                    "Review and approve the proposed plan before execution continues."
                )
                logger.info("execution_node: plan mode blocked %r", tool_name)
                return {
                    "awaiting_plan_approval": True,
                    "awaiting_user_input": True,
                    "plan_mode_blocked_tool": tool_name,
                    "last_result": {"ok": False, "error": blocked_msg},
                    "history": [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "tool_execution_result": {
                                        "ok": False,
                                        "error": blocked_msg,
                                    }
                                }
                            ),
                        }
                    ],
                    "next_action": None,
                }

    current_role = None
    try:
        from src.core.orchestration.graph.nodes.node_utils import get_current_role

        current_role = get_current_role(state, config)
    except ImportError:
        pass

    if current_role:
        from src.core.orchestration.role_config import is_tool_allowed_for_role

        if not is_tool_allowed_for_role(tool_name, current_role):
            role_error = (
                f"Tool '{tool_name}' is not permitted for role '{current_role}'"
            )
            logger.warning("execution_node: %s", role_error)
            return {
                "last_result": {"ok": False, "error": role_error},
                "history": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "tool_execution_result": {
                                    "ok": False,
                                    "error": role_error,
                                }
                            }
                        ),
                    }
                ],
                "next_action": None,
            }

    return None


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
            orchestrator.event_bus.publish("plan.progress", progress_payload)
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


def compute_execution_post_tool_updates(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    tool_name: str,
    result: Mapping[str, Any],
    modifying_tools: Sequence[str],
) -> Dict[str, Any]:
    """Compute the post-tool bookkeeping updates merged into the final payload."""
    tool_call_count = int(state.get("tool_call_count") or 0) + 1
    no_plan_fail_update = compute_no_plan_fail_update(state=state, result=result)
    plan_approval_consumed = compute_plan_approval_consumed(
        state=state,
        tool_name=tool_name,
        result=result,
        modifying_tools=modifying_tools,
    )
    affected_files_update = compute_affected_files_update(
        tool_name=tool_name,
        result=result,
        state=state,
        orchestrator=orchestrator,
    )
    plan_exit_update = compute_plan_exit_update(orchestrator=orchestrator)
    return {
        "tool_call_count": tool_call_count,
        "no_plan_fail_update": no_plan_fail_update,
        "plan_approval_consumed": plan_approval_consumed,
        "affected_files_update": affected_files_update,
        "plan_exit_update": plan_exit_update,
    }


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
            orchestrator.event_bus.publish(
                "step.start",
                {
                    "step": step_num,
                    "total": total_steps,
                    "tool": tool_name,
                    "description": description,
                    "session_id": state.get("session_id"),
                },
            )
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
            step_ok = bool(result.get("ok") or result.get("status") == "ok")
            orchestrator.event_bus.publish(
                "step.finish",
                {
                    "step": step_num,
                    "total": total_steps,
                    "tool": tool_name,
                    "ok": step_ok,
                    "elapsed_ms": elapsed_ms,
                    "tool_call_count": int(state.get("tool_call_count") or 0) + 1,
                    "session_id": state.get("session_id"),
                },
            )
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


def compute_plan_step_updates(
    *,
    result: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    original_task: Optional[str],
    execution_waves: Optional[Sequence[Any]],
    current_wave: int,
    step_retry_counts: Mapping[str, Any],
    max_step_retries: int = 3,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    plan_advance: Dict[str, Any] = {}
    wave_advance: Dict[str, Any] = {}

    if not current_plan or current_step >= len(current_plan):
        return plan_advance, wave_advance

    execution_ok = bool(result.get("ok") or result.get("status") == "ok")
    if not execution_ok:
        return plan_advance, wave_advance

    updated_plan = [dict(step) for step in current_plan]
    updated_plan[current_step]["completed"] = True
    next_step = current_step + 1

    if execution_waves and current_wave < len(execution_waves):
        wave_step_ids = execution_waves[current_wave]
        step_id_str = str(current_step)
        if step_id_str in wave_step_ids or current_step in wave_step_ids:
            all_in_wave_complete = True
            for wave_step in wave_step_ids:
                wave_idx = (
                    int(wave_step.split("_")[-1])
                    if isinstance(wave_step, str) and wave_step.startswith("step_")
                    else wave_step
                )
                if isinstance(wave_idx, str):
                    try:
                        wave_idx = int(wave_idx.replace("step_", ""))
                    except (ValueError, AttributeError):
                        wave_idx = wave_step
                if isinstance(wave_idx, int) and wave_idx < len(updated_plan):
                    step_done = updated_plan[wave_idx].get("completed")
                    step_retries = int(step_retry_counts.get(str(wave_idx), 0))
                    step_retry_exhausted = step_retries >= max_step_retries
                    if not step_done and not step_retry_exhausted:
                        all_in_wave_complete = False
                        break

            if all_in_wave_complete:
                wave_advance = {"current_wave": current_wave + 1}

    if next_step < len(updated_plan):
        plan_advance = {
            "current_step": next_step,
            "current_plan": updated_plan,
            "task": updated_plan[next_step].get("description", ""),
        }
    else:
        plan_advance = {
            "current_step": next_step,
            "current_plan": updated_plan,
            "task": original_task or "Task complete",
        }

    return plan_advance, wave_advance


def compute_no_plan_fail_update(
    *, state: Mapping[str, Any], result: Mapping[str, Any]
) -> Dict[str, Any]:
    if state.get("current_plan"):
        return {}

    execution_ok = bool(result.get("ok") or result.get("status") == "ok")
    is_format_error = "format_error" in str(result.get("error", ""))
    if execution_ok:
        return {"no_plan_fail_count": 0}
    if not is_format_error:
        return {"no_plan_fail_count": int(state.get("no_plan_fail_count") or 0) + 1}
    return {}


def compute_plan_approval_consumed(
    *,
    state: Mapping[str, Any],
    tool_name: str,
    result: Mapping[str, Any],
    modifying_tools: Sequence[str],
) -> Dict[str, Any]:
    if (
        state.get("plan_mode_approved")
        and tool_name in modifying_tools
        and (result.get("ok") or result.get("status") == "ok")
    ):
        return {"plan_mode_approved": False}
    return {}


def compute_affected_files_update(
    *,
    tool_name: str,
    result: Mapping[str, Any],
    state: Mapping[str, Any],
    orchestrator: Any,
) -> Dict[str, Any]:
    affected_files_update: Dict[str, Any] = {}
    if (
        tool_name == "ask_user"
        and result.get("status") == "ok"
        and state.get("affected_files") is not None
    ):
        approval_kw = re.compile(
            r"\b(yes\s+all|approve\s+all|expand\s+scope|allow\s+all|unrestrict)\b",
            re.IGNORECASE,
        )
        file_pat = re.compile(
            r"\b([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|yaml|yml|json|md|txt|toml|cfg|ini|sh|bash|html|css|scss|sql))\b"
        )
        answer_text = str(result.get("answer") or "")
        if approval_kw.search(answer_text):
            affected_files_update = {"affected_files": []}
        else:
            new_paths = []
            seen_paths = set(state.get("affected_files") or [])
            for match in file_pat.finditer(answer_text):
                path = match.group(1)
                if path not in seen_paths and not path.startswith(".."):
                    seen_paths.add(path)
                    new_paths.append(path)
            if new_paths:
                expanded = list(state.get("affected_files") or []) + new_paths
                affected_files_update = {"affected_files": expanded}
                if orchestrator:
                    orchestrator._affected_files = expanded

    return affected_files_update


def build_read_then_write_result(
    *,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    tool_name: str,
    path_arg: Optional[str],
    working_dir: str,
    truncate_tool_output: Callable[[dict], dict],
    tool_last_used: Mapping[str, Any],
    files_read: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    actual_result = result.get("result", {})
    if (
        tool_name not in ("read_file", "fs.read")
        or not path_arg
        or not isinstance(actual_result, dict)
    ):
        return None

    status = actual_result.get("status")
    if status is None:
        if actual_result.get("items") is not None or "content" in actual_result:
            status = "ok"
    if status != "ok":
        return None

    resolved = str((Path(working_dir) / path_arg).resolve())
    task = (state.get("task") or "").lower()
    modification_keywords = (
        "add ",
        "prepend",
        "append",
        "edit ",
        "modify",
        "update ",
        "change ",
        "replace ",
        "insert ",
        "delete ",
        "remove ",
        "top of ",
        "beginning of ",
        "after ",
        "before ",
        "on top of ",
        "inside ",
        "contents of ",
        "fix ",
        "create ",
        "write ",
        "implement",
        "correct",
        "repair",
        "patch",
    )
    task_implies_write = any(keyword in task for keyword in modification_keywords)

    if not task_implies_write:
        return {
            "verified_reads": [resolved],
            "files_read": {**dict(files_read), resolved: True},
        }

    enhanced_context = (
        f"Task: {state.get('task')}\n"
        f"Context: You just read the file '{path_arg}'.\n"
        f"File contents:\n{actual_result.get('content', '')}\n"
        f"Today's date: {date.today().isoformat()}\n"
        f"Use write_file tool to write the updated content based on the task above."
    )
    new_messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "tool_execution_result": truncate_tool_output(dict(result)),
                    "orchestration_hint": "write_required",
                    "file_path": path_arg,
                    "enhanced_context": enhanced_context,
                }
            ),
        }
    ]
    return {
        "last_result": result,
        "last_tool_name": tool_name,
        "verified_reads": [resolved],
        "history": new_messages,
        "next_action": None,
        "tool_call_count": int(state.get("tool_call_count") or 0) + 1,
        "tool_last_used": dict(tool_last_used),
        "files_read": {**dict(files_read), resolved: True},
    }


def build_tool_history_messages(
    *,
    result: Mapping[str, Any],
    truncate_tool_output: Callable[[dict], dict],
) -> List[Dict[str, str]]:
    return [
        {
            "role": "user",
            "content": json.dumps(
                {"tool_execution_result": truncate_tool_output(dict(result))}
            ),
        }
    ]


def compute_replan_trigger(*, result: Mapping[str, Any]) -> Dict[str, Any]:
    if result.get("requires_split") is True:
        error_msg = result.get(
            "error", "Patch exceeded 200 lines. Split into multiple targeted functions."
        )
        return {
            "replan_required": error_msg,
            "action_failed": True,
            "next_action": None,
        }
    return {}


def compute_plan_progress_payload(
    *,
    state: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    execution_ok: bool,
) -> Dict[str, Any]:
    if not current_plan or current_step >= len(current_plan):
        return {}
    step_desc = current_plan[current_step].get("description", "Unknown step")
    progress_payload = {
        "sessionUpdate": "plan_progress",
        "planId": f"plan_{state.get('session_id', 'default')}",
        "currentStep": current_step + 1,
        "totalSteps": len(current_plan),
        "stepDescription": step_desc,
        "status": "completed" if execution_ok else "in_progress",
    }
    return {"plan_progress": progress_payload}


def compute_plan_exit_update(*, orchestrator: Any) -> Dict[str, Any]:
    if not orchestrator:
        return {}
    committed = getattr(orchestrator, "_committed_plan_steps", None)
    if committed:
        setattr(orchestrator, "_committed_plan_steps", None)
        setattr(orchestrator, "_plan_mode_approved", True)
        return {
            "current_plan": committed,
            "plan_mode_approved": True,
        }
    return {}


def build_execution_return_payload(
    *,
    result: Mapping[str, Any],
    tool_name: str,
    verified_reads: List[str],
    history: List[Dict[str, str]],
    tool_call_count: int,
    tool_last_used: Mapping[str, Any],
    files_read: Mapping[str, Any],
    recent_tool_calls: List[Any],
    plan_advance: Mapping[str, Any],
    wave_advance: Mapping[str, Any],
    replan_triggered: Mapping[str, Any],
    plan_progress_event: Mapping[str, Any],
    plan_approval_consumed: Mapping[str, Any],
    no_plan_fail_update: Mapping[str, Any],
    affected_files_update: Mapping[str, Any],
    plan_exit_update: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "last_result": result,
        "last_tool_name": tool_name,
        "verified_reads": verified_reads,
        "history": history,
        "next_action": None,
        "tool_call_count": tool_call_count,
        "tool_last_used": dict(tool_last_used),
        "files_read": dict(files_read),
        "recent_tool_calls": recent_tool_calls,
        **dict(plan_advance),
        **dict(wave_advance),
        **dict(replan_triggered),
        **dict(plan_progress_event),
        **dict(plan_approval_consumed),
        **dict(no_plan_fail_update),
        **dict(affected_files_update),
        **dict(plan_exit_update),
    }
# _capture_snapshot and _validate_python_syntax live in execution_guards.py (P3-T6)
# and are re-exported at the top of this file for backwards compatibility.
