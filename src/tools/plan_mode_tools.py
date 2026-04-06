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
5. LLM calls ``plan_exit()`` to return to execution mode.
6. orchestrator sets ``orchestrator._agent_mode = "execution"`` and fires
   an ``agent.mode_changed`` event so the TUI can display the current mode.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.tools._tool import tool


@tool(side_effects=[], tags=["planning", "mode"])
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


@tool(side_effects=[], tags=["planning", "mode"])
def plan_exit(
    reason: Optional[str] = None,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Switch the agent back to execution mode (default).

    In execution mode the system prompt uses the operational role and all
    tools are available subject to normal permission checks.

    Args:
        reason: Optional description of why planning mode is being exited.
        workdir: Ignored — present for uniform tool signature compatibility.

    Returns:
        {"ok": True, "agent_mode": "execution", "message": "..."}
    """
    msg = f"Exited planning mode. {reason}" if reason else "Exited planning mode."
    return {
        "ok": True,
        "agent_mode": "execution",
        "message": msg,
    }
