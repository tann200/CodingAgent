import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Mapping, Dict, Any

from src.core.orchestration.graph.state import AgentState, validate_state
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.loop_guards import (
    check_read_before_write,
    check_cooldown,
    check_doom_loop as _guard_doom_loop,
    MODIFYING_TOOLS,  # single source of truth (includes manage_todo — TS-5)
    RECENT_CALLS_WINDOW as _RECENT_CALLS_WINDOW,
)

logger = logging.getLogger(__name__)

# D-11: Named constants — avoids magic numbers scattered through the node body.
_EXECUTION_MAX_PROMPT_TOKENS = 4000  # token budget passed to ContextBuilder


def _inc_step_retry(state: Mapping[str, Any], current_step: int) -> dict:
    """Return a new step_retry_counts dict with current_step's count incremented."""
    counts = dict(state.get("step_retry_counts") or {})
    key = str(current_step)
    counts[key] = int(counts.get(key, 0)) + 1
    return counts


async def _execute_tool_with_locks(
    tool_name: str, args: Dict, lock_manager, orchestrator: Any, agent_id: str = "main"
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

        result = orchestrator.execute_tool({"name": tool_name, "arguments": args})
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


async def execution_node(state: Mapping[str, Any], config: Any) -> Dict[str, Any]:
    """
    Execution Layer: Programmatically enforces Operational Workflows.
    Uses the 'operational' role from ContextBuilder (loaded from agent-brain).
    Dynamic skill injection: If len(relevant_files) > 2, injects 'dry' skill.
    """
    # Validate state invariants at node entry (D-02: non-fatal, logs on issues)
    validate_state(state)

    # Resolve orchestrator first (needed for dynamic cancel_event lookup)
    try:
        orchestrator = _resolve_orchestrator(state, config)
        # Subagent scenarios may pass {"orchestrator": None} - this is acceptable
        # Only error if config was explicitly provided but orchestrator wasn't found
        if orchestrator is None:
            # Check if config explicitly passed {"orchestrator": None} (subagent case)
            config_has_orchestrator_field = False
            if config and isinstance(config, dict):
                cfg = config.get("configurable") or config
                if cfg is not None and "orchestrator" in cfg:
                    config_has_orchestrator_field = True

            # If config explicitly had orchestrator=None, this is a subagent - continue without orchestrator
            if config_has_orchestrator_field:
                logger.info(
                    "execution_node: subagent mode (orchestrator=None in config)"
                )
            else:
                logger.error("execution_node: orchestrator is None")
                return {
                    "last_result": None,
                    "errors": ["orchestrator not found"],
                }
    except Exception as e:
        logger.error(f"execution_node: failed to get orchestrator: {e}")
        return {
            "last_result": None,
            "errors": [f"config error: {e}"],
        }

    # Check for cancellation - dynamically resolve from orchestrator if not in state
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

    try:
        # HR-4 fix: planned_action (set by step_controller for the current step) takes priority
        # over next_action (set by perception which may be stale from a prior round).
        # Using next_action first caused stale perception outputs to override freshly computed
        # step actions from the planner/step_controller.
        action = state.get("planned_action") or state.get("next_action")
    except Exception as e:
        logger.error(f"execution_node: failed to get next_action: {e}")
        action = None

    # Begin step-level atomic transaction for multi-file rollback support.
    # All file writes during this execution will be captured in a single snapshot
    # that can be atomically rolled back by verification_node on failure.
    # Skip if no orchestrator (subagent mode)
    try:
        if orchestrator and hasattr(orchestrator, "begin_step_transaction"):
            orchestrator.begin_step_transaction()
            logger.debug("execution_node: step transaction started")
    except Exception as _tx_err:
        logger.debug(
            f"execution_node: step transaction init failed (non-fatal): {_tx_err}"
        )

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

    if execution_waves and current_wave < len(execution_waves):
        wave_steps = execution_waves[current_wave]
        logger.info(
            f"Wave execution: wave {current_wave + 1}/{len(execution_waves)} "
            f"with {len(wave_steps)} steps"
        )

    # If we have a plan but no action, we need to generate one for the current step
    if not action and current_plan and current_step < len(current_plan):
        current_step_desc = current_plan[current_step].get("description", "")
        logger.info(
            f"No action provided, generating tool for step: {current_step_desc}"
        )

        # Call LLM to generate a tool for this step
        try:
            if not orchestrator or not getattr(orchestrator, "tool_registry", None):
                raise RuntimeError(
                    "execution_node: orchestrator or tool_registry unavailable for LLM step generation"
                )
            builder = ContextBuilder(working_dir=state.get("working_dir"))
            tools_list = [
                {"name": n, "description": m.get("description", "")}
                for n, m in orchestrator.tool_registry.tools.items()
            ]

            # Dynamic skill injection: if many relevant files, inject DRY skill by name
            active_skills = []
            relevant_files = state.get("relevant_files") or []
            if len(relevant_files) > 2:
                active_skills.append("dry")
                logger.info(
                    "execution_node: injected DRY skill due to many relevant files"
                )

            step_prompt = f"""Execute this specific step: {current_step_desc}

Working directory: {state.get("working_dir")}
Original task: {original_task or state.get("task")}

Generate the appropriate tool call to complete this step. Respond with ONLY a tool call in the required YAML format."""

            provider_capabilities = {}
            if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
                provider_capabilities = orchestrator.get_provider_capabilities()

            messages = builder.build_prompt(
                role_name="operational",
                active_skills=active_skills,
                task_description=step_prompt,
                tools=tools_list,
                conversation=state.get("history", []),
                max_tokens=_EXECUTION_MAX_PROMPT_TOKENS,
                provider_capabilities=provider_capabilities,
                model_tier=state.get("model_tier"),  # S1-B/S1-C
            )

            cancel_event = state.get("cancel_event")
            if not cancel_event:
                cancel_event = getattr(orchestrator, "cancel_event", None)

            # Check cancellation before invoking the model (NEW-12 fix: replaced
            # create_task + polling loop with a simple pre/post cancel check).
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                logger.info("execution_node: Task canceled before LLM call")
                return {
                    "last_result": {"ok": False, "error": "Task canceled by user"},
                    "next_action": None,
                    "errors": ["canceled"],
                }

            # MC-1: Pass OpenAI function format tools for native function calling support.
            # GAP 2: temperature=0.0 for strict execution determinism.
            functions = None
            if orchestrator and hasattr(
                orchestrator.tool_registry, "get_openai_functions"
            ):
                functions = orchestrator.tool_registry.get_openai_functions()

            # SES-W3 / SM-1: Model override priority:
            # 1. state["override_model"] — set by delegate_task for per-subagent binding
            # 2. execution_model from providers.json (get_model_for_role("operational"))
            # 3. None — use active provider default
            _exec_model_override = state.get("override_model") or None
            if not _exec_model_override:
                try:
                    from src.core.config_loader import get_model_for_role as _gmfr

                    _exec_model_override = _gmfr("operational")
                except Exception:
                    pass

            # WF-5: resolve hard timeout for this LLM call.
            _exec_llm_timeout: int | None = 120
            try:
                from src.core.orchestration.project_settings import (
                    get_active_settings as _gas_exec,
                )

                _ps_exec = _gas_exec()
                if _ps_exec is not None:
                    _exec_llm_timeout = _ps_exec.max_llm_wait_seconds or None
            except Exception:
                pass

            try:
                _call_coro = call_model(
                    messages,
                    stream=False,
                    format_json=False,
                    tools=functions,
                    temperature=0.0,
                    session_id=state.get("session_id"),
                    **({"model": _exec_model_override} if _exec_model_override else {}),
                )
                if _exec_llm_timeout:
                    resp = await asyncio.wait_for(_call_coro, timeout=_exec_llm_timeout)
                else:
                    resp = await _call_coro
            except asyncio.TimeoutError:
                logger.warning(
                    f"execution_node: LLM call timed out after {_exec_llm_timeout}s"
                )
                return {
                    "last_result": {
                        "ok": False,
                        "error": f"LLM call timed out after {_exec_llm_timeout}s",
                    },
                    "next_action": "wait_for_user",
                    "errors": [f"llm_timeout:{_exec_llm_timeout}s"],
                }
            except asyncio.CancelledError:
                logger.info(
                    "execution_node: LLM call cancelled (asyncio.CancelledError)"
                )
                raise

            # Check cancellation after the model returns — cancel_event may have
            # been set while generation was in flight.
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                logger.info("execution_node: Task canceled after LLM call")
                return {
                    "last_result": {"ok": False, "error": "Task canceled by user"},
                    "next_action": None,
                    "errors": ["canceled"],
                }

            # REACT-OVF-EARLY-EXIT: context overflow detected — stop immediately so
            # route_execution can route to memory_sync instead of cycling back to
            # perception with the same oversized history.
            if isinstance(resp, dict) and resp.get("context_overflow"):
                _raw_hist = list(state.get("history") or [])
                _trunc_hist = _raw_hist[-6:] if len(_raw_hist) > 6 else _raw_hist
                logger.warning(
                    "execution_node: context overflow detected — "
                    f"truncating history {len(_raw_hist)} → {len(_trunc_hist)} messages; "
                    "errors=['context_overflow'] will route to memory_sync"
                )
                return {
                    "history": [],
                    "_compacted_history": _trunc_hist,
                    "next_action": None,
                    "_budget_compaction": True,
                    "_should_distill": True,
                    "errors": ["context_overflow"],
                    "last_result": {
                        "ok": False,
                        "error": "Context window overflow in execution — history truncated, compaction triggered",
                    },
                }

            content = ""
            tool_calls = None
            if isinstance(resp, dict):
                if resp.get("choices"):
                    ch = resp["choices"][0].get("message")
                    if isinstance(ch, dict):
                        content = ch.get("content") or ""
                        # MC-1: Check for native function calls first
                        tool_calls = ch.get("tool_calls")
                elif resp.get("message"):
                    content = resp.get("message", {}).get("content", "")
                    tool_calls = resp.get("message", {}).get("tool_calls")

            # Parse the tool call
            tool_call = None
            # MC-1: Prefer native function calls, fall back to YAML parsing
            if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                tc = tool_calls[0]
                if isinstance(tc, dict):
                    func = tc.get("function")
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
                            logger.info(f"execution_node: native function call: {name}")
            if not tool_call:
                tool_call = parse_tool_block(content)
            if tool_call:
                logger.info(f"Generated tool call for step: {tool_call}")
                # F3: Build an immutable copy before updating — never mutate state in place.
                updated_plan = [dict(s) for s in current_plan]
                updated_plan[current_step]["action"] = tool_call
                current_plan = updated_plan
                action = tool_call
        except Exception as e:
            logger.error(f"Failed to generate tool for step: {e}")
            _notify_provider_limit(str(e))

    # If we have a plan and haven't finished it, check if we need to advance
    if current_plan and current_step < len(current_plan):
        current_step_desc = current_plan[current_step].get("description", "")

        # Update the task to focus on the current step
        if task_decomposed and original_task:
            # Construct context for current step
            progress_msg = f"Working on step {current_step + 1}/{len(current_plan)} of multi-step task.\n"
            progress_msg += f"Current step: {current_step_desc}\n"
            if current_step > 0:
                progress_msg += f"Completed: {', '.join([s.get('description', '') for s in current_plan[:current_step] if s.get('completed')])}"
            logger.info(
                f"Plan execution: step {current_step + 1}/{len(current_plan)} - {current_step_desc}"
            )

    if not action:
        # Check if the model explicitly declared the step complete without a tool call.
        # This happens when the LLM outputs "STATUS: complete" after having already
        # executed all necessary tools in prior sub-turns.  Without this guard,
        # execution_node returns {"last_result": None}, which should_after_execution
        # treats as a failure and reroutes to perception — creating an infinite loop
        # where the same truncated history is replayed up to recursion_limit times.
        _content_lower = (content or "").lower()

        # Empty-content guard: if the model produced NO content at all (empty string,
        # not None) and no tool call, the context window is likely full and the model
        # was truncated before it could emit a tool call.  Returning last_result=None
        # here would route back to perception with the SAME bloated history, causing
        # an infinite loop (same history → same empty response → repeat).
        # Return a structured error instead so the debug/step_controller can handle it.
        if content is not None and content.strip() == "" and not action:
            logger.warning(
                "execution_node: model produced empty content with no tool call — "
                "context window may be full or model failed to generate output"
            )
            return {
                "last_result": {
                    "ok": False,
                    "error": "empty_model_response: model produced no content. "
                    "The conversation history may be too long for the context window. "
                    "Try breaking the task into smaller steps.",
                },
                "step_retry_counts": _inc_step_retry(state, current_step),
                "history": [],
            }

        if re.search(r"status\s*:\s*complete", _content_lower):
            logger.info(
                "execution_node: model declared STATUS: complete with no tool call — "
                "treating as successful step completion"
            )
            # Mark current plan step as completed so routing advances correctly.
            _updated_plan = [dict(s) for s in current_plan] if current_plan else []
            if _updated_plan and current_step < len(_updated_plan):
                _updated_plan[current_step]["completed"] = True
                _next_step = current_step + 1
                _plan_advance: Dict[str, Any] = {
                    "current_step": _next_step,
                    "current_plan": _updated_plan,
                }
                if _next_step < len(_updated_plan):
                    _plan_advance["task"] = _updated_plan[_next_step].get(
                        "description", ""
                    )
                else:
                    _plan_advance["task"] = original_task or "Task complete"
            else:
                _plan_advance = {}
            _history_entry = (
                [{"role": "assistant", "content": content}] if content else []
            )
            return {
                "last_result": {
                    "ok": True,
                    "status": "ok",
                    "completed_without_tool": True,
                },
                "history": _history_entry,
                **_plan_advance,
            }
        # GAP-SMALL-5: tag this as a format error, not a task failure.
        # The no_plan_fail_count guard should not fire on format errors alone.
        return {
            "last_result": {"ok": False, "error": "format_error: no tool call emitted"},
            "step_retry_counts": _inc_step_retry(state, current_step),
        }

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

    # Workflow Enforcement 2: Sandbox Preflight
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
        # Loop guard: if the tool doesn't exist AND we already have a successful
        # execution result (task was completed), treat the hallucinated tool name
        # as a completion signal instead of injecting a sandbox violation error.
        # This prevents small models from looping on non-existent "respond" tool.
        tool_not_found = "not found" in (preflight.get("error") or "").lower()
        prev_result = state.get("last_result") or {}
        prev_ok = prev_result.get("ok") or prev_result.get("status") == "ok"
        if tool_not_found and prev_ok and (state.get("rounds") or 0) >= 1:
            logger.info(
                f"route_execution: tool '{action.get('name')}' not found but task "
                "already completed — treating as completion signal"
            )
            import json as _json

            synthetic_result = _json.dumps(
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
        # Also publish to the orchestrator's history directly to guarantee it's captured
        try:
            orchestrator.msg_mgr.append("user", error_content)
        except Exception as e:
            logger.error(
                f"Failed to append sandbox violation to orchestrator history: {e}"
            )

        return {
            "last_result": preflight,
            "history": [
                {
                    "role": "user",
                    "content": error_content,
                }
            ],
            "next_action": None,
        }

    # Plan Mode Gate: block write tools until the user approves the plan.
    # Runs AFTER preflight (sandbox check) and BEFORE preview mode so that
    # plan approval is always required before any diff is shown or written.
    if state.get("plan_mode_enabled", False) and tool_name in MODIFYING_TOOLS:
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
                            "args": args,
                        }
                    )
                blocked_msg = (
                    f"Plan Mode: tool '{tool_name}' is blocked pending plan approval. "
                    f"Review and approve the proposed plan before execution continues."
                )
                logger.info(f"execution_node: plan mode blocked '{tool_name}'")
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

    # Phase 3: Preview Mode - Generate preview for modifying tools
    if state.get("preview_mode_enabled", False) and tool_name in MODIFYING_TOOLS:
        try:
            preview_service = getattr(orchestrator, "preview_service", None)
            if preview_service:
                # Read old content for diff
                old_content = None
                new_content = None
                file_path = args.get("path") or args.get("file_path")

                if file_path:
                    try:
                        file_full_path = Path(state["working_dir"]) / file_path
                        if file_full_path.exists():
                            old_content = file_full_path.read_text()
                    except Exception:
                        pass

                # For write_file, new_content is in args
                if tool_name == "write_file":
                    new_content = args.get("content", "")
                elif tool_name in ("edit_file", "edit_by_line_range"):
                    new_content = args.get("new_string") or args.get("content", "")

                # Generate preview
                preview = preview_service.generate_preview(
                    tool_name=tool_name,
                    args=args,
                    old_content=old_content,
                    new_content=new_content,
                )

                logger.info(
                    f"Preview mode: generated preview {preview.preview_id} for {tool_name}"
                )

                return {
                    "pending_preview_id": preview.preview_id,
                    "awaiting_user_input": True,
                    "preview_confirmed": False,
                }
        except Exception as e:
            logger.warning(f"Preview mode error: {e}, proceeding with execution")

    # Role enforcement for subagents (before execution)
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
            logger.warning(f"execution_node: {role_error}")
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

    # P4-4: Propagate plan_mode_approved from state so execute_tool can enforce it.
    if orchestrator:
        orchestrator._plan_mode_approved = state.get("plan_mode_approved")
        # GAP-S2: Propagate affected_files so scope guard in execute_tool can enforce it.
        orchestrator._affected_files = list(state.get("affected_files") or [])

    # Execute tool — use file locking when PRSW (wave/DAG) execution is active
    lock_manager = None
    if orchestrator and hasattr(orchestrator, "get_file_lock_manager"):
        lock_manager = orchestrator.get_file_lock_manager()

    # Step boundary event: step.start — mirrors opencode's StepStartPart.
    # Emitted just before tool execution so TUI can show "running..." state.
    import time as _time_mod

    _step_start_ts = None
    _step_num = (state.get("current_step") or 0) + 1
    _total_steps = len(current_plan) if current_plan else 1
    try:
        _step_start_ts = _time_mod.monotonic()
        if hasattr(orchestrator, "event_bus"):
            orchestrator.event_bus.publish(
                "step.start",
                {
                    "step": _step_num,
                    "total": _total_steps,
                    "tool": tool_name,
                    "description": (
                        current_plan[current_step].get("description", "")
                        if current_plan and current_step < len(current_plan)
                        else ""
                    ),
                    "session_id": state.get("session_id"),
                },
            )
    except Exception:
        pass

    if lock_manager and (state.get("execution_waves") or state.get("plan_dag")):
        agent_id = state.get("session_id") or "main"
        res = await _execute_tool_with_locks(
            tool_name, args, lock_manager, orchestrator, agent_id
        )
    else:
        res = orchestrator.execute_tool(action)

    # TASK-13: Async post-tool hooks — fire-and-forget, non-blocking.
    # The synchronous run_post() inside execute_tool() may block the thread
    # pool for up to _POST_HOOK_TIMEOUT seconds.  Here we additionally fire
    # the asyncio-native variant from the event loop so callers in async
    # context pay no blocking cost.
    # BUG-VOL23-1: Store the Task reference so the GC cannot collect it before
    # completion; attach a done-callback to log any exceptions instead of
    # swallowing them silently.
    try:
        _async_hook_runner = getattr(orchestrator, "_tool_hook_runner", None)
        if _async_hook_runner is not None and hasattr(
            _async_hook_runner, "async_run_post"
        ):
            _hook_task = asyncio.ensure_future(
                _async_hook_runner.async_run_post(tool_name, args, res)
            )

            def _log_hook_exc(t: "asyncio.Task[None]") -> None:  # type: ignore[type-arg]
                if not t.cancelled() and t.exception() is not None:
                    logger.warning("async_run_post failed: %s", t.exception())

            _hook_task.add_done_callback(_log_hook_exc)
    except Exception:
        pass

    # Step boundary event: step.finish — emitted immediately after tool returns.
    try:
        _elapsed_ms = (
            int((_time_mod.monotonic() - _step_start_ts) * 1000)
            if _step_start_ts is not None
            else None
        )
        if hasattr(orchestrator, "event_bus"):
            _step_ok = bool(res.get("ok") or res.get("status") == "ok")
            orchestrator.event_bus.publish(
                "step.finish",
                {
                    "step": _step_num,
                    "total": _total_steps,
                    "tool": tool_name,
                    "ok": _step_ok,
                    "elapsed_ms": _elapsed_ms,
                    "tool_call_count": int(state.get("tool_call_count") or 0) + 1,
                    "session_id": state.get("session_id"),
                },
            )
    except Exception:
        pass

    # _recent_calls already has the current fingerprint appended by _guard_doom_loop
    # (it returns updated_recent in both the error and non-error paths).
    # Trim to window just in case extra entries accumulated.
    _recent_calls = _recent_calls[-_RECENT_CALLS_WINDOW:]

    # UI Sync: Forward tool result to TUI so user can see execution result
    if orchestrator and hasattr(orchestrator, "msg_mgr"):
        try:
            orchestrator.msg_mgr.append(
                "user", json.dumps({"tool_execution_result": res})
            )
        except Exception as e:
            logger.debug(f"UI sync failed: {e}")

    # Successful tool execution
    verified_update = []
    plan_advance = {}
    # Build updated tool_last_used and files_read dicts for state return
    _tool_last_used_update = dict(state.get("tool_last_used") or {})
    _files_read_update = dict(state.get("files_read") or {})
    _current_count = int(state.get("tool_call_count") or 0)
    # Record this tool execution in cooldown tracker (use pre-increment count as key)
    _cooldown_key = f"{tool_name}:{path_arg or ''}"
    _tool_last_used_update[_cooldown_key] = _current_count

    # Check for multi-step plan completion
    if current_plan and current_step < len(current_plan):
        # Check if execution was successful (handle both {"ok": True} and {"status": "ok"} formats)
        execution_ok = res.get("ok") or res.get("status") == "ok"
        if execution_ok:
            # Build an immutable copy with the completed step marked — never mutate state in place
            updated_plan = [dict(s) for s in current_plan]
            updated_plan[current_step]["completed"] = True
            next_step = current_step + 1

            # Phase A: Wave-based execution advancement
            wave_advance = {}
            if execution_waves and current_wave < len(execution_waves):
                wave_step_ids = execution_waves[current_wave]
                step_id_str = str(current_step)
                if step_id_str in wave_step_ids or current_step in wave_step_ids:
                    all_in_wave_complete = True
                    # P2-8: Read retry budget to detect permanently-failed steps
                    _step_retry_counts: dict = state.get("step_retry_counts") or {}
                    _MAX_STEP_RETRIES = 3
                    for ws in wave_step_ids:
                        ws_idx = (
                            int(ws.split("_")[-1])
                            if isinstance(ws, str) and ws.startswith("step_")
                            else ws
                        )
                        if isinstance(ws_idx, str):
                            try:
                                ws_idx = int(ws_idx.replace("step_", ""))
                            except (ValueError, AttributeError):
                                ws_idx = ws
                        if isinstance(ws_idx, int) and ws_idx < len(updated_plan):
                            step_done = updated_plan[ws_idx].get("completed")
                            # P2-8: Treat exhausted-retry steps as "done" so wave can advance
                            step_retries = int(_step_retry_counts.get(str(ws_idx), 0))
                            step_retry_exhausted = step_retries >= _MAX_STEP_RETRIES
                            if not step_done and not step_retry_exhausted:
                                all_in_wave_complete = False
                                break

                    if all_in_wave_complete:
                        next_wave = current_wave + 1
                        if next_wave < len(execution_waves):
                            wave_advance = {"current_wave": next_wave}
                            logger.info(
                                f"Wave {current_wave + 1} complete, advancing to wave {next_wave + 1}"
                            )
                        else:
                            wave_advance = {"current_wave": next_wave}
                            logger.info("All waves completed")

            if next_step < len(updated_plan):
                # Move to next step
                plan_advance = {
                    "current_step": next_step,
                    "current_plan": updated_plan,
                    "task": updated_plan[next_step].get("description", ""),
                }
                logger.info(
                    f"Step {current_step + 1} complete, advancing to step {next_step + 1}"
                )
            else:
                # Plan complete
                plan_advance = {
                    "current_step": next_step,
                    "current_plan": updated_plan,
                    "task": original_task or "Task complete",
                }
                logger.info("All plan steps completed")

    # Check if execution was successful (handle both {"ok": True} and {"status": "ok"} formats)
    execution_ok = res.get("ok") or res.get("status") == "ok"
    if execution_ok:
        # Check actual tool logic success (different from tool wrapper success)
        actual_res = res.get("result", {})

        # Log failure for debugging if needed
        if tool_name == "edit_file" and actual_res.get("status") != "ok":
            logging.getLogger("coding_agent").info(f"PATCH FAILED: {actual_res}")

        status = actual_res.get("status")
        if status is None:
            if actual_res.get("items") is not None:
                status = "ok"
            elif "content" in actual_res:
                status = "ok"

        # Read-then-write enforcement: after successful read, if task implies modification,
        # update task context so perception knows to generate write tool.
        # The orchestration enforces reading before writing.
        verified_update = []
        if status == "ok" and tool_name in ("read_file", "fs.read") and path_arg:
            try:
                resolved = str((Path(state["working_dir"]) / path_arg).resolve())
                verified_update = [resolved]
                _files_read_update[resolved] = True

                # Check if task implies modification (orchestration enforces read-before-write)
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
                )
                task_implies_write = any(kw in task for kw in modification_keywords)

                if task_implies_write:
                    from datetime import date

                    today = date.today().isoformat()
                    file_content = ""
                    actual_res = res.get("result", {})
                    if isinstance(actual_res, dict):
                        file_content = actual_res.get("content", "")

                    # HIGH-10 fix: pass enhanced context exclusively via new_messages
                    # instead of overwriting state["task"], which would permanently
                    # replace the original task description across all graph turns.
                    # BUG-VOL23-2: removed hardcoded "add today's date on top" directive
                    # that was injected for every file-read on modification-keyword tasks,
                    # causing the agent to corrupt files regardless of the actual task.
                    enhanced_context = (
                        f"Task: {state.get('task')}\n"
                        f"Context: You just read the file '{path_arg}'.\n"
                        f"File contents:\n{file_content}\n"
                        f"Today's date: {today}\n"
                        f"Use write_file tool to write the updated content based on the task above."
                    )
                    logger.info(
                        f"execution_node: read succeeded, task implies modification. "
                        f"Injecting write context via messages: {path_arg}"
                    )
                    new_messages = [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "tool_execution_result": res,
                                    "orchestration_hint": "write_required",
                                    "file_path": path_arg,
                                    "enhanced_context": enhanced_context,
                                }
                            ),
                        }
                    ]
                    return {
                        "last_result": res,
                        "last_tool_name": tool_name,
                        "verified_reads": verified_update,
                        "history": new_messages,
                        "next_action": None,
                        "tool_call_count": int(state.get("tool_call_count") or 0) + 1,
                        "tool_last_used": _tool_last_used_update,
                        "files_read": _files_read_update,
                    }
            except Exception as e:
                logger.error(f"execution_node: read-then-write enforcement error: {e}")

        # Sync session state immediately to ensure the next turn in THIS graph run sees it
        if verified_update:
            for path in verified_update:
                orchestrator._session_read_files.add(path)

    # FIX: Return ONLY the new message as "user" role so ContextBuilder doesn't filter it out.
    # The ContextBuilder filters out non-user/assistant roles, so tool results need to be "user".
    # Also, we must NOT mutate the existing history list in place - LangGraph will duplicate it!
    new_messages = [
        {"role": "user", "content": json.dumps({"tool_execution_result": res})}
    ]

    # Phase 2: Patch Size Guard - Intercept requires_split flag
    replan_triggered = {}
    if res.get("requires_split") is True:
        error_msg = res.get(
            "error", "Patch exceeded 200 lines. Split into multiple targeted functions."
        )
        logger.warning(
            f"execution_node: patch too large, triggering replan - {error_msg}"
        )
        replan_triggered = {
            "replan_required": error_msg,
            "action_failed": True,
            "next_action": None,
        }

    # Publish plan.progress event for UI dashboard (GAP 2: ACP sessionUpdate schema)
    plan_progress_event = {}
    if current_plan and current_step < len(current_plan):
        step_desc = current_plan[current_step].get("description", "Unknown step")
        # GAP 2: ACP-compliant sessionUpdate schema for plan progress
        progress_payload = {
            "sessionUpdate": "plan_progress",
            "planId": f"plan_{state.get('session_id', 'default')}",
            "currentStep": current_step + 1,
            "totalSteps": len(current_plan),
            "stepDescription": step_desc,
            "status": "completed" if execution_ok else "in_progress",
        }
        plan_progress_event = {"plan_progress": progress_payload}
        # Fire EventBus event so TUI plan panel updates in real time
        try:
            if hasattr(orchestrator, "event_bus"):
                orchestrator.event_bus.publish("plan.progress", progress_payload)
        except Exception:
            pass

        # Check off the completed step in TODO.md
        if execution_ok:
            try:
                from src.tools.todo_tools import manage_todo

                manage_todo(
                    action="check",
                    workdir=str(state.get("working_dir", ".")),
                    step_id=current_step,
                )
            except Exception:
                pass

    # W12: Increment tool call budget counter on every execution
    tool_call_count = int(state.get("tool_call_count") or 0) + 1

    # HR-4: Track no-plan execution failure count
    # GAP-SMALL-5: only increment on genuine task failures, not format errors.
    # Format errors (model didn't emit a tool call) are retried without counting
    # against the doom-loop guard — small models need a few extra retries for this.
    no_plan_fail_update = {}
    current_plan = state.get("current_plan")
    if not current_plan:
        execution_ok = res.get("ok") or res.get("status") == "ok"
        _is_format_error = "format_error" in str(res.get("error", ""))
        if execution_ok:
            no_plan_fail_update = {"no_plan_fail_count": 0}
        elif not _is_format_error:
            no_plan_fail_update = {
                "no_plan_fail_count": int(state.get("no_plan_fail_count") or 0) + 1
            }

    # Consume plan_mode_approved after first successful write tool execution.
    # This resets the approval flag so subsequent plan cycles require fresh approval.
    plan_approval_consumed = {}
    if (
        state.get("plan_mode_approved")
        and tool_name in MODIFYING_TOOLS
        and (res.get("ok") or res.get("status") == "ok")
    ):
        plan_approval_consumed = {"plan_mode_approved": False}

    # GAP-S2: ask_user scope-expansion — if the user approved extra files via
    # ask_user, extract them from the answer and merge into affected_files so the
    # scope guard allows subsequent writes to those paths.
    #
    # Two signals are recognised in the answer text:
    #   1. Explicit file paths matching _FILE_PAT (e.g. "yes, src/foo.py and src/bar.py")
    #   2. A blanket approval keyword ("yes all", "approve all", "expand scope",
    #      "allow all") — clears the restriction entirely (sets affected_files=[]).
    affected_files_update: dict = {}
    if (
        tool_name == "ask_user"
        and res.get("status") == "ok"
        and state.get("affected_files") is not None
    ):
        import re as _re

        _APPROVAL_KW = _re.compile(
            r"\b(yes\s+all|approve\s+all|expand\s+scope|allow\s+all|unrestrict)\b",
            _re.IGNORECASE,
        )
        _FILE_PAT2 = _re.compile(
            r"\b([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|yaml|yml|json|md|txt|toml|cfg|ini|sh|bash|html|css|scss|sql))\b"
        )
        answer_text = str(res.get("answer") or "")
        if _APPROVAL_KW.search(answer_text):
            # Blanket approval — remove the scope restriction entirely.
            affected_files_update = {"affected_files": []}
        else:
            # Extract individual file paths from the answer.
            new_paths = []
            seen_paths = set(state.get("affected_files") or [])
            for _m in _FILE_PAT2.finditer(answer_text):
                _p = _m.group(1)
                if _p not in seen_paths and not _p.startswith(".."):
                    seen_paths.add(_p)
                    new_paths.append(_p)
            if new_paths:
                expanded = list(state.get("affected_files") or []) + new_paths
                affected_files_update = {"affected_files": expanded}
                # Keep orchestrator instance in sync immediately.
                if orchestrator:
                    orchestrator._affected_files = expanded

    # ORCH-W4 plan_exit handoff: pick up steps committed via plan_exit(steps=[...]).
    # If the orchestrator has _committed_plan_steps, move them into state["current_plan"]
    # and set plan_mode_approved so the write gate is unblocked.
    plan_exit_update: dict = {}
    if orchestrator:
        _committed = getattr(orchestrator, "_committed_plan_steps", None)
        if _committed:
            plan_exit_update["current_plan"] = _committed
            plan_exit_update["plan_mode_approved"] = True
            # Clear after consuming so it doesn't re-apply on the next turn.
            setattr(orchestrator, "_committed_plan_steps", None)
            setattr(orchestrator, "_plan_mode_approved", True)

    return {
        "last_result": res,
        "last_tool_name": tool_name,  # W1: enables verification_node to detect side-effecting tools
        "verified_reads": verified_update,
        "history": new_messages,
        "next_action": None,  # Reset after execution
        "tool_call_count": tool_call_count,
        # Cooldown + read-before-edit tracking dicts
        "tool_last_used": _tool_last_used_update,
        "files_read": _files_read_update,
        # Doom loop tracking: updated fingerprint window
        "recent_tool_calls": _recent_calls,
        **plan_advance,
        **wave_advance,
        **replan_triggered,
        **plan_progress_event,
        **plan_approval_consumed,
        **no_plan_fail_update,
        **affected_files_update,
        **plan_exit_update,
    }
