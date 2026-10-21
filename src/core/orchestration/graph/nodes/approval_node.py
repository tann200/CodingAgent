"""Approval Node for the unified 7-node LangGraph cognitive pipeline.

Enforces approval contract per Engineering Audit §Appendix:
- Renders one pending action or structured mini-plan.
- Binds approval to the exact action digest; changed arguments require a new approval.
- Cannot approve an empty plan, stale action, or action changed after display.
- Routes approved -> agent_loop; rejected|cancelled -> finalize.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.node_utils import (
    span_node as _span_node,
)
from src.core.orchestration.graph.nodes.wait_for_user_node import wait_for_user_node
from src.core.orchestration.graph.policy import compute_action_digest
from src.core.orchestration.graph.state import StateLike

logger = logging.getLogger(__name__)


async def approval_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _approval_node_impl."""
    with _span_node("approval", {"step": state.get("current_step", 0)}):
        return await _approval_node_impl(state, config)


async def _approval_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Handle action approval and validation."""
    logger.info("=== approval_node START ===")
    run_state = dict(state.get("run") or {})
    pending_action = state.get("pending_action")

    # Invariant: Cannot approve an empty action or empty plan
    if not pending_action and not state.get("next_action") and not state.get("current_plan"):
        logger.warning("approval_node: rejected attempt to approve empty action/plan")
        run_state["status"] = "failed"
        run_state["outcome"] = "failed"
        return {
            "run": run_state,
            "errors": ["Cannot approve empty action or plan"],
            "awaiting_plan_approval": False,
            "awaiting_user_input": False,
            "plan_mode_approved": False,
        }

    # Validate action digest integrity if pending_action is present
    if isinstance(pending_action, Mapping):
        act_name = str(pending_action.get("name") or "")
        act_args = dict(pending_action.get("arguments") or {})
        act_digest = str(pending_action.get("digest") or "")
        computed = compute_action_digest(act_name, act_args)
        if act_digest and computed != act_digest:
            logger.warning(
                "approval_node: action arguments changed after display (digest mismatch: %s vs %s)",
                act_digest,
                computed,
            )
            run_state["status"] = "cancelled"
            run_state["outcome"] = "cancelled"
            return {
                "run": run_state,
                "errors": ["Action arguments changed after display — approval revoked"],
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": False,
            }

    # Delegate to user interaction / wait_for_user logic
    base_result = await wait_for_user_node(state, config)
    res = dict(base_result)

    approved = res.get("plan_mode_approved") or state.get("plan_mode_approved") or res.get("preview_confirmed")
    if approved:
        run_state["status"] = "running"
        run_state["outcome"] = "continue"
    else:
        # If cancelled or rejected
        if res.get("awaiting_user_input") is False and not approved:
            run_state["status"] = "cancelled"
            run_state["outcome"] = "cancelled"
        else:
            run_state["status"] = "awaiting_approval"
            run_state["outcome"] = "approval_required"

    res["run"] = run_state
    return res
