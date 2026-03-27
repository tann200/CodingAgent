"""
wait_for_user_node.py - Suspends graph until user confirms preview or approves plan.

CRITICAL: This node uses asyncio.Event to properly suspend LangGraph.
The node awaits confirmation from the TUI before returning.

Two suspension modes:
  1. Preview Mode  — awaiting_user_input=True + pending_preview_id set
  2. Plan Mode     — awaiting_plan_approval=True (no preview_id)
"""

import asyncio
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


async def wait_for_user_node(state: Dict[str, Any], config: Any) -> Dict[str, Any]:
    """
    Suspend graph execution until user confirms or rejects preview / approves plan.

    Plan Mode branch (awaiting_plan_approval=True):
      - Publishes 'plan.requested' event so TUI can show the approval UI
      - Awaits orchestrator.wait_for_plan_approval()
      - Returns plan_mode_approved=True/False; clears awaiting_plan_approval

    Preview Mode branch (pending_preview_id set):
      - Awaits preview_service.wait_for_confirmation(preview_id)
      - Returns preview_confirmed=True/False; clears awaiting_user_input

    Returns:
        Plan mode:    {"awaiting_plan_approval": False, "awaiting_user_input": False,
                       "plan_mode_approved": <bool>, "plan_mode_blocked_tool": None}
        Preview mode: {"awaiting_user_input": False, "preview_confirmed": <bool>,
                       "pending_preview_id": None}
    """
    from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator

    orchestrator = _resolve_orchestrator(state, config)

    # ── Plan Mode ────────────────────────────────────────────────────────────
    if state.get("awaiting_plan_approval", False):
        if not orchestrator:
            logger.warning("wait_for_user_node: plan approval requested but no orchestrator")
            return {
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": False,
                "plan_mode_blocked_tool": None,
                "last_result": {"ok": False, "error": "No orchestrator for plan approval"},
            }

        # Publish plan.requested so TUI surfaces the approval panel
        try:
            event_bus = getattr(orchestrator, "event_bus", None)
            if event_bus:
                event_bus.publish(
                    "plan.requested",
                    {
                        "plan": state.get("current_plan"),
                        "blocked_tool": state.get("plan_mode_blocked_tool"),
                        "session_id": state.get("session_id"),
                    },
                )
        except Exception as e:
            logger.warning(f"wait_for_user_node: failed to publish plan.requested: {e}")

        try:
            approved = await orchestrator.wait_for_plan_approval()
            logger.info(f"wait_for_user_node: plan approval result={approved}")
            return {
                "awaiting_plan_approval": False,
                "awaiting_user_input": False,
                "plan_mode_approved": approved,
                "plan_mode_blocked_tool": None,
            }
        except asyncio.CancelledError:
            # Treat cancellation as rejection
            if hasattr(orchestrator, "reject_plan"):
                orchestrator.reject_plan()
            logger.warning("wait_for_user_node: plan approval cancelled")
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
        logger.warning("wait_for_user_node: no preview pending")
        return {
            "awaiting_user_input": False,
            "preview_confirmed": False,
            "last_result": {"ok": False, "error": "No preview pending"},
        }

    preview = preview_service.get_preview(preview_id)
    if not preview:
        logger.error(f"wait_for_user_node: preview {preview_id} not found")
        return {
            "awaiting_user_input": False,
            "preview_confirmed": False,
        }

    try:
        confirmed = await preview_service.wait_for_confirmation(preview_id)

        logger.info(f"wait_for_user_node: preview {preview_id} confirmed={confirmed}")

        return {
            "awaiting_user_input": False,
            "preview_confirmed": confirmed,
            "pending_preview_id": None,
        }

    except asyncio.CancelledError:
        preview_service.reject(preview_id)
        logger.warning(f"wait_for_user_node: preview {preview_id} cancelled")
        return {
            "awaiting_user_input": False,
            "preview_confirmed": False,
            "pending_preview_id": None,
            "last_result": {"ok": False, "error": "Preview cancelled"},
        }
