import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


async def execution_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Execution Layer: Programmatically enforces Operational Workflows.
    Uses the 'operational' role from AgentBrainManager.
    Dynamic skill injection: If len(relevant_files) > 2, injects 'dry' skill.
    """
    # Get AgentBrainManager for role-specific prompts
    brain = get_agent_brain_manager()
    operational_role = brain.get_role("operational") or "You are a coding assistant."

    # Resolve orchestrator first (needed for dynamic cancel_event lookup)
    try:
        orchestrator = _resolve_orchestrator(state, config)
        if orchestrator is None:
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
        action = state["next_action"]
    except Exception as e:
        logger.error(f"execution_node: failed to get next_action: {e}")
        action = None

    # Handle multi-step plan execution
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    original_task = state.get("original_task")
    task_decomposed = state.get("task_decomposed", False)

    # If we have a plan but no action, we need to generate one for the current step
    if not action and current_plan and current_step < len(current_plan):
        current_step_desc = current_plan[current_step].get("description", "")
        logger.info(
            f"No action provided, generating tool for step: {current_step_desc}"
        )

        # Call LLM to generate a tool for this step
        try:
            builder = ContextBuilder()
            tools_list = [
                {"name": n, "description": m.get("description", "")}
                for n, m in orchestrator.tool_registry.tools.items()
            ]

            # Dynamic skill injection: if many relevant files, inject DRY skill
            active_skills = []
            relevant_files = state.get("relevant_files") or []
            if len(relevant_files) > 2:
                dry_skill = brain.get_skill("dry")
                if dry_skill:
                    active_skills.append(dry_skill)
                    logger.info(
                        "execution_node: injected DRY skill due to many relevant files"
                    )

            step_prompt = f"""Execute this specific step: {current_step_desc}

Working directory: {state.get("working_dir")}
Original task: {original_task or state.get("task")}

Generate the appropriate tool call to complete this step. Respond with ONLY a tool call in the required YAML format."""

            messages = builder.build_prompt(
                identity=operational_role,
                role=f"Executing step {current_step + 1}/{len(current_plan)}",
                active_skills=active_skills,
                task_description=step_prompt,
                tools=tools_list,
                conversation=state.get("history", []),
                max_tokens=4000,
            )

            cancel_event = state.get("cancel_event")
            if not cancel_event:
                cancel_event = getattr(orchestrator, "cancel_event", None)

            raw_resp = call_model(messages, stream=False, format_json=False)

            # Interrupt Polling: Check cancel_event every 0.2s during LLM generation
            if hasattr(raw_resp, "__await__"):
                llm_task = asyncio.create_task(raw_resp)
                while not llm_task.done():
                    if (
                        cancel_event
                        and hasattr(cancel_event, "is_set")
                        and cancel_event.is_set()
                    ):
                        llm_task.cancel()
                        logger.info("execution_node: Task canceled mid-generation")
                        return {
                            "last_result": {
                                "ok": False,
                                "error": "Task canceled by user",
                            },
                            "next_action": None,
                            "errors": ["canceled"],
                        }
                    await asyncio.sleep(0.2)
                resp = await llm_task
            else:
                resp = raw_resp

            content = ""
            if isinstance(resp, dict):
                if resp.get("choices"):
                    ch = resp["choices"][0].get("message")
                    if isinstance(ch, dict):
                        content = ch.get("content") or ""
                elif resp.get("message"):
                    content = resp.get("message", {}).get("content", "")

            # Parse the tool call
            tool_call = parse_tool_block(content)
            if tool_call:
                logger.info(f"Generated tool call for step: {tool_call}")
                # Update the step with the action
                current_plan[current_step]["action"] = tool_call
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
        return {"last_result": None}

    tool_name = action["name"]
    args = action.get("arguments", {})
    path_arg = args.get("path") or args.get("file_path")

    # SECURITY FIX: Extended read-before-edit requirement to ALL modifying tools
    # Previously only checked edit_file, now covers edit_file, edit_by_line_range, apply_patch
    MODIFYING_TOOLS = [
        "edit_file",
        "edit_by_line_range",
        "apply_patch",
        "write_file",
        "delete_file",
    ]
    if tool_name in MODIFYING_TOOLS and path_arg:
        try:
            resolved = str((Path(state["working_dir"]) / path_arg).resolve())
            # Check both the state (immutability) AND the orchestrator (current session)
            if (
                resolved not in state.get("verified_reads", [])
                and resolved not in orchestrator._session_read_files
            ):
                err_msg = (
                    f"Security/Logic violation: You must read '{path_arg}' before {tool_name}. "
                    f"Use read_file first to inspect the current content."
                )
                return {
                    "last_result": {"ok": False, "error": err_msg},
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
                    "next_action": None,  # Reset to force re-planning
                }
        except Exception:
            pass

    # Loop Prevention: Check for repeated tool calls
    if orchestrator and hasattr(orchestrator, "_check_loop_prevention"):
        orchestrator._read_execution_trace()  # Load trace for loop detection
        loop_detected = orchestrator._check_loop_prevention(tool_name, args)
        if loop_detected:
            loop_msg = "[LOOP DETECTED] Repeated tool calls blocked; consider alternate strategy."
            try:
                orchestrator.msg_mgr.append("system", loop_msg)
            except Exception:
                pass
            return {
                "last_result": {"ok": False, "error": loop_msg},
                "history": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "tool_execution_result": {
                                    "ok": False,
                                    "error": loop_msg,
                                }
                            }
                        ),
                    }
                ],
                "next_action": None,
            }

    # Workflow Enforcement 2: Sandbox Preflight
    preflight = orchestrator.preflight_check(action)
    if not preflight.get("ok"):
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

    # Execute tool
    res = orchestrator.execute_tool(action)

    # UI Sync: Forward tool result to TUI so user can see execution result
    if orchestrator and hasattr(orchestrator, "msg_mgr"):
        try:
            import json

            orchestrator.msg_mgr.append(
                "user", json.dumps({"tool_execution_result": res})
            )
        except Exception as e:
            logger.debug(f"UI sync failed: {e}")

    # Successful tool execution
    verified_update = []
    plan_advance = {}

    # Check for multi-step plan completion
    if current_plan and current_step < len(current_plan):
        # Check if execution was successful (handle both {"ok": True} and {"status": "ok"} formats)
        execution_ok = res.get("ok") or res.get("status") == "ok"
        if execution_ok:
            # Mark current step as completed
            current_plan[current_step]["completed"] = True
            next_step = current_step + 1

            if next_step < len(current_plan):
                # Move to next step
                plan_advance = {
                    "current_step": next_step,
                    "current_plan": current_plan,
                    "task": current_plan[next_step].get("description", ""),
                }
                logger.info(
                    f"Step {current_step + 1} complete, advancing to step {next_step + 1}"
                )
            else:
                # Plan complete
                plan_advance = {
                    "current_step": next_step,
                    "current_plan": current_plan,
                    "task": original_task or "Task complete",
                }
                logger.info("All plan steps completed")

    # Check if execution was successful (handle both {"ok": True} and {"status": "ok"} formats)
    execution_ok = res.get("ok") or res.get("status") == "ok"
    if execution_ok:
        # Log to trace
        import datetime

        orchestrator._append_execution_trace(
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "goal": state["task"],
                "tool": tool_name,
                "args": args,
                "result_summary": str(res)[:100],
            }
        )

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

        if status == "ok":
            if (
                tool_name in ["read_file", "fs.read", "list_dir", "list_files"]
                and path_arg
            ):
                try:
                    resolved = str((Path(state["working_dir"]) / path_arg).resolve())
                    verified_update = [resolved]
                except Exception:
                    pass

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

    # Publish plan.progress event for UI dashboard
    plan_progress_event = {}
    if current_plan and current_step < len(current_plan):
        step_desc = current_plan[current_step].get("description", "Unknown step")
        plan_progress_event = {
            "plan_progress": {
                "current_step": current_step + 1,
                "total_steps": len(current_plan),
                "step_description": step_desc,
                "completed": execution_ok,
            }
        }

    return {
        "last_result": res,
        "verified_reads": verified_update,
        "history": new_messages,
        "next_action": None,  # Reset after execution
        **plan_advance,
        **replan_triggered,
        **plan_progress_event,
    }
