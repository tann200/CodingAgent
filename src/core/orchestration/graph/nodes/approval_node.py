"""Approval Node for the unified 6-node LangGraph cognitive pipeline.

Enforces approval contract per Engineering Audit §Appendix:
- Renders one pending action or structured mini-plan.
- Binds approval to the exact action digest; changed arguments require a new approval.
- Cannot approve an empty plan, stale action, or action changed after display.
- Suspends via asyncio.Event until user approves/rejects plan or confirms/rejects preview.
- Routes approved -> agent_loop; rejected|cancelled -> finalize.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.messaging.event_types import PlanRequested
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    span_node as _span_node,
)
from src.core.orchestration.graph.policy import compute_action_digest
from src.core.orchestration.graph.state import StateLike

logger = logging.getLogger(__name__)


async def _wait_for_user(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Suspend execution until user confirms/rejects preview or approves/rejects plan.

    Inlined from legacy ``wait_for_user_node.py`` (TASK-25 Stage A).
    """
    orchestrator = _resolve_orchestrator(state, config)

    # ── Plan Mode ────────────────────────────────────────────────────────────
    if state.get("awaiting_plan_approval", False):
        if not orchestrator:
            logger.warning("approval_node: plan approval requested but no orchestrator")
            return {
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": False,
                "plan_mode_blocked_tool": None,
                "last_result": {
                    "ok": False,
                    "error": "No orchestrator for plan approval",
                },
            }

        try:
            event_bus = getattr(orchestrator, "event_bus", None)
            if event_bus:
                event_bus.publish_typed(
                    PlanRequested(
                        plan=state.get("current_plan"),
                        blocked_tool=state.get("plan_mode_blocked_tool"),
                        session_id=state.get("session_id") or "",
                    )
                )
        except Exception as exc:
            logger.warning("approval_node: failed to publish plan.requested: %s", exc)

        try:
            approved = await orchestrator.wait_for_plan_approval()
            logger.info("approval_node: plan approval result=%s", approved)
            return {
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": approved,
                "plan_mode_blocked_tool": None,
            }
        except asyncio.CancelledError:
            if hasattr(orchestrator, "reject_plan"):
                orchestrator.reject_plan()
            logger.warning("approval_node: plan approval cancelled")
            return {
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": False,
                "plan_mode_blocked_tool": None,
                "last_result": {"ok": False, "error": "Plan approval cancelled"},
            }

    # ── Preview Mode ─────────────────────────────────────────────────────────
    preview_service = (
        getattr(orchestrator, "preview_service", None) if orchestrator else None
    )
    preview_id = state.get("pending_preview_id")

    if not preview_id or not preview_service:
        logger.warning("approval_node: no preview pending")
        return {
            "awaiting_user_input": False,
            "preview_confirmed": False,
            "last_result": {"ok": False, "error": "No preview pending"},
        }

    preview = preview_service.get_preview(preview_id)
    if not preview:
        logger.error("approval_node: preview %s not found", preview_id)
        return {
            "awaiting_user_input": False,
            "preview_confirmed": False,
        }

    try:
        confirmed = await preview_service.wait_for_confirmation(preview_id)
        logger.info("approval_node: preview %s confirmed=%s", preview_id, confirmed)
        return {
            "awaiting_user_input": False,
            "preview_confirmed": confirmed,
            "pending_preview_id": None,
        }
    except asyncio.CancelledError:
        preview_service.reject(preview_id)
        logger.warning("approval_node: preview %s cancelled", preview_id)
        return {
            "awaiting_user_input": False,
            "preview_confirmed": False,
            "pending_preview_id": None,
            "last_result": {"ok": False, "error": "Preview cancelled"},
        }


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

    # Validate action digest integrity if pending_action is present (fail closed if missing or mismatched)
    if isinstance(pending_action, Mapping):
        act_name = str(pending_action.get("name") or "")
        act_args = dict(pending_action.get("arguments") or {})
        act_digest = str(pending_action.get("digest") or "")
        computed = compute_action_digest(act_name, act_args)
        if not act_digest or computed != act_digest:
            logger.warning(
                "approval_node: missing or mismatched action digest (%s vs %s)",
                act_digest,
                computed,
            )
            run_state["status"] = "cancelled"
            run_state["outcome"] = "cancelled"
            return {
                "run": run_state,
                "errors": [
                    "Missing action digest — approval denied"
                    if not act_digest
                    else "Action arguments changed after display — approval revoked"
                ],
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": False,
            }

    base_result = await _wait_for_user(state, config)
    res = dict(base_result)

    # Determine approval strictly from the interactive result — never let an
    # earlier plan_mode_approved=True auto-approve a subsequent diff preview.
    if state.get("pending_preview_id") or "preview_confirmed" in res:
        approved = bool(res.get("preview_confirmed"))
    elif state.get("awaiting_plan_approval") or "plan_mode_approved" in res:
        approved = bool(res.get("plan_mode_approved"))
    else:
        approved = False

    orchestrator = _resolve_orchestrator(state, config)
    if approved:
        if orchestrator is not None:
            try:
                orchestrator._plan_mode_approved = True
            except Exception:
                pass
        resumed_action = state.get("next_action")
        if not resumed_action and isinstance(pending_action, Mapping) and pending_action.get("name"):
            resumed_action = {
                "name": str(pending_action["name"]),
                "arguments": dict(pending_action.get("arguments") or {}),
            }
        res["next_action"] = resumed_action
        res["pending_action"] = None
        run_state["status"] = "running"
        run_state["outcome"] = "continue"
    else:
        res["next_action"] = None
        res["pending_action"] = None
        if res.get("awaiting_user_input") is False and not approved:
            run_state["status"] = "cancelled"
            run_state["outcome"] = "cancelled"
        else:
            run_state["status"] = "awaiting_approval"
            run_state["outcome"] = "approval_required"

    res["run"] = run_state
    return res
