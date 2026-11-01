"""Finalize Node for the unified 6-node 2026 LangGraph cognitive pipeline.

Completes agent run:
- Enforces the fail-closed Transaction Settlement Guard on open step snapshots
  so unverified, failed, or cancelled deferred-rollback edits never leak to disk.
- Persists trajectory/session summaries deterministically without an extra
  blocking end-of-run LLM distillation call unless explicitly requested.
- Guarantees terminal exit to END.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    span_node as _span_node,
)
from src.core.orchestration.graph.state import StateLike

logger = logging.getLogger(__name__)


async def finalize_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _finalize_node_impl."""
    with _span_node("finalize", {"turn": state.get("turn_count", 0)}):
        return await _finalize_node_impl(state, config)


async def _finalize_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Terminal execution node with fail-closed Transaction Settlement Guard."""
    logger.info("=== finalize_node START ===")
    run_state = dict(state.get("run") or {})
    orchestrator = _resolve_orchestrator(state, config)

    cancel_event = state.get("cancel_event") or (
        getattr(orchestrator, "cancel_event", None) if orchestrator else None
    )
    if (
        cancel_event is not None
        and hasattr(cancel_event, "is_set")
        and callable(cancel_event.is_set)
        and cancel_event.is_set() is True
    ):
        run_state["status"] = "cancelled"
        run_state["outcome"] = "cancelled"

    outcome = run_state.get("outcome") or "complete"
    if outcome in ("complete", "verify", "continue"):
        if run_state.get("status") not in ("failed", "cancelled"):
            run_state["status"] = "completed"
            run_state["outcome"] = "complete"

    # Transaction Settlement Guard: settle any open deferred-rollback snapshot
    snap_id = getattr(orchestrator, "_step_snapshot_id", None) if orchestrator else None
    if isinstance(snap_id, str) and snap_id:
        can_commit = state.get("verification_passed") is True or (
            state.get("use_verification") is False
            and run_state.get("status") not in ("failed", "cancelled")
            and run_state.get("outcome") not in ("failed", "cancelled")
        )
        if can_commit:
            try:
                if hasattr(orchestrator, "commit_step_transaction"):
                    orchestrator.commit_step_transaction()
                    logger.info("finalize_node: committed open snapshot %s", snap_id)
            except Exception as exc:
                logger.warning("finalize_node: commit_step_transaction error: %s", exc)
        else:
            # Fail closed: never leave unverified/failed/cancelled mutations on disk
            try:
                if hasattr(orchestrator, "rollback_step_transaction"):
                    rb = orchestrator.rollback_step_transaction()
                    logger.info(
                        "finalize_node: settlement guard rolled back unverified snapshot %s (restored=%s)",
                        snap_id,
                        rb.get("restored_count", 0) if isinstance(rb, dict) else "unknown",
                    )
            except Exception as exc:
                logger.warning("finalize_node: rollback_step_transaction error: %s", exc)
            if run_state.get("status") != "cancelled":
                run_state["status"] = "failed"
                run_state["outcome"] = "failed"

    eval_verdict = state.get("evaluation_result") or (
        "complete" if run_state.get("status") == "completed" else "failed"
    )
    mem_state = dict(state)
    mem_state["evaluation_result"] = eval_verdict
    if "_should_distill" not in state and not state.get("_force_compact"):
        mem_state["_should_distill"] = False

    base_res = await memory_update_node(mem_state, config)
    res = dict(base_res)
    res["run"] = run_state
    if not res.get("evaluation_result"):
        res["evaluation_result"] = eval_verdict

    logger.info(
        "finalize_node: pipeline completed with status=%s, outcome=%s",
        run_state.get("status"),
        run_state.get("outcome"),
    )
    return res
