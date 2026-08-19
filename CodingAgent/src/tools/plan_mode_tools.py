"""ORCH-W4: plan_enter / plan_exit tool calls.

These tools let the LLM explicitly transition the agent's operating mode
between "planning" (strategic, read-only analysis) and "execution"
(operational, write-enabled).  They are registered in the global tool
registry and wired into execute_tool() in the orchestrator so that the
mode change is reflected in the next system prompt rebuild.

Usage flow
----------
1. LLM calls ``plan_enter()`` at the start of a planning phase.
2. orchestrator.execute_tool() intercepts → sets ``orchestrator._agent_mode = "planning"``.
3. perception_node reads ``_agent_mode`` on the next turn and calls
   ``build_prompt(role_name="strategic")`` instead of ``"operational"``.
4. LLM produces a plan in the appropriate strategic role context.
5. LLM calls ``plan_exit(steps=[...])`` to commit the plan and return to
   execution mode.  The ``steps`` list becomes the agent's ``current_plan``
   — execution is gated on this committed plan.
6. orchestrator sets ``orchestrator._agent_mode = "execution"`` and fires
   an ``agent.mode_changed`` event so the TUI can display the current mode.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.tools._tool import tool, PermissionKind


@tool(side_effects=[], tags=["planning", "mode"], permission_kind=PermissionKind.PLAN)
def plan_enter(
    reason: Optional[str] = None,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Switch the agent into planning mode.

    In planning mode the system prompt adopts the strategic role so the LLM
    focuses on analysis and design rather than immediate execution.  Write
    tools are not blocked by this call — use explicit permission rules if
    you want write-tool gating.

    Args:
        reason: Optional description of why planning mode is being entered
                (logged in the audit trail).
        workdir: Ignored — present for uniform tool signature compatibility.

    Returns:
        {"ok": True, "agent_mode": "planning", "message": "..."}
    """
    msg = f"Entered planning mode. {reason}" if reason else "Entered planning mode."
    return {
        "ok": True,
        "agent_mode": "planning",
        "message": msg,
    }


@tool(side_effects=[], tags=["planning", "mode"], permission_kind=PermissionKind.PLAN)
def plan_exit(
    steps: Optional[List[Dict[str, Any]]] = None,
    reason: Optional[str] = None,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Commit a plan and switch back to execution mode.

    This is the formal plan-to-build handoff.  Pass the approved plan as a
    list of step dicts so the agent can track progress against a committed
    plan rather than executing ad-hoc.  If ``steps`` is omitted the agent
    switches to execution mode without a committed plan (legacy behaviour).

    Each step dict should contain at minimum:
    - ``description`` (str): human-readable description of the step
    - ``tool`` (str, optional): primary tool to use for this step
    - ``target`` (str, optional): file or symbol the step operates on

    Args:
        steps: Optional list of plan step dicts to commit as ``current_plan``.
               When provided, ``plan_mode_approved`` is set to True and the
               execution node is unblocked to proceed with write operations.
        reason: Optional description of why planning mode is being exited.
        workdir: Ignored — present for uniform tool signature compatibility.

    Returns:
        {
            "ok": True,
            "agent_mode": "execution",
            "plan_committed": True/False,
            "step_count": N,
            "message": "..."
        }
    """
    plan_committed = bool(steps)
    step_count = len(steps) if steps else 0
    if plan_committed:
        msg = f"Exited planning mode with {step_count} committed steps. " + (
            reason or "Plan approved; execution unblocked."
        )
    else:
        msg = f"Exited planning mode. {reason}" if reason else "Exited planning mode."

    result: Dict[str, Any] = {
        "ok": True,
        "agent_mode": "execution",
        "plan_committed": plan_committed,
        "step_count": step_count,
        "message": msg,
    }
    # Attach steps to result so execute_tool / orchestrator can propagate them
    # into state["current_plan"] and set state["plan_mode_approved"] = True.
    if steps is not None:
        result["steps"] = steps
    return result
