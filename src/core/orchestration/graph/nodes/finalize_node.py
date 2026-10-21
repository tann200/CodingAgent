"""Finalize Node for the unified 7-node LangGraph cognitive pipeline.

Completes agent run per Engineering Audit §Appendix:
- Writes session/memory output, persists trajectory, emits terminal events.
- Guarantees no re-entry into perception or agent_loop.
- Exits to END.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.memory_update_node import memory_update_node
from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node
from src.core.orchestration.graph.state import StateLike

logger = logging.getLogger(__name__)


async def finalize_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _finalize_node_impl."""
    with _span_node("finalize", {"turn": state.get("turn_count", 0)}):
        return await _finalize_node_impl(state, config)


async def _finalize_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Terminal execution node."""
    logger.info("=== finalize_node START ===")
    run_state = dict(state.get("run") or {})

    # Invariant: Finalize never re-enters perception or agent loop.
    outcome = run_state.get("outcome") or "complete"
    if outcome in ("complete", "verify", "continue"):
        if run_state.get("status") not in ("failed", "cancelled"):
            run_state["status"] = "completed"
            run_state["outcome"] = "complete"

    # Synchronize memory, write trajectory and durable summaries
    base_res = await memory_update_node(state, config)
    res = dict(base_res)
    res["run"] = run_state
    if not res.get("evaluation_result"):
        res["evaluation_result"] = "complete" if run_state["status"] == "completed" else "failed"

    logger.info("finalize_node: pipeline completed with status=%s, outcome=%s", run_state["status"], run_state["outcome"])
    return res
