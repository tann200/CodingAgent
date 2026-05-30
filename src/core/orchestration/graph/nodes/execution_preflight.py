"""execution_preflight.py — Sandbox, role, and plan-mode gate checks.

Extracted from execution_helpers.py (P3-4) for improved modularity.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from src.core.orchestration.graph.nodes.execution_guards import (
    check_agent_definition_tool_gate,
)


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
        _prev_ok_flag = prev_result.get("ok")
        prev_ok = (_prev_ok_flag is True) or (_prev_ok_flag is None and prev_result.get("status") == "ok")
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

    # P3-1: Second gate — AgentDefinition.is_tool_permitted() via active_agent.
    _agent_gate = check_agent_definition_tool_gate(
        orchestrator=orchestrator, tool_name=tool_name, logger=logger
    )
    if _agent_gate is not None:
        return _agent_gate

    return None
