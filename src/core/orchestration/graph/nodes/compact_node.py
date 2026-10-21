"""Compaction Node for the unified 7-node LangGraph cognitive pipeline.

Performs deterministic-first history reduction per Engineering Audit §Appendix:
- Deterministic-first tool output pruning and message reduction.
- Monotonically bounded compaction limit per tier policy (medium: 1, frontier: 2).
- Prevents infinite compaction loops.
- Routes compacted -> agent_loop; repeat overflow or failure -> finalize.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node
from src.core.orchestration.graph.nodes.tool_output_truncation import prune_tool_outputs
from src.core.orchestration.graph.state import StateLike, replace_state_list

logger = logging.getLogger(__name__)


async def compact_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _compact_node_impl."""
    with _span_node("compact", {"turn": state.get("turn_count", 0)}):
        return await _compact_node_impl(state, config)


async def _compact_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Deterministic-first history reduction."""
    logger.info("=== compact_node START ===")
    run_state = dict(state.get("run") or {})
    runtime_state = dict(state.get("runtime") or {})
    policy = dict(runtime_state.get("policy") or {})

    compaction_limit = int(policy.get("compaction_limit") or 1)
    compactions_taken = int(run_state.get("compactions") or 0) + 1
    run_state["compactions"] = compactions_taken

    if compactions_taken > compaction_limit:
        logger.warning(
            "compact_node: compaction limit exceeded (%d > %d) — routing to finalize",
            compactions_taken,
            compaction_limit,
        )
        run_state["status"] = "failed"
        run_state["outcome"] = "failed"
        errors = list(state.get("errors") or [])
        errors.append("context_overflow_exhausted")
        return {
            "run": run_state,
            "errors": errors,
            "_budget_compaction": False,
        }

    # Deterministic reduction: prune large tool outputs
    history = list(state.get("history") or [])
    initial_len = len(history)

    pruned = prune_tool_outputs(history)
    # If still large (>30 messages), retain system/initial prompt and keep recent window
    if len(pruned) > 30:
        logger.info("compact_node: trimming message window from %d to 20 messages", len(pruned))
        keep_initial = pruned[:2]
        keep_recent = pruned[-18:]
        pruned = keep_initial + [{"role": "system", "content": "[Context compacted: older conversational turns trimmed]"}] + keep_recent

    logger.info("compact_node: compaction %d/%d complete (messages: %d -> %d)", compactions_taken, compaction_limit, initial_len, len(pruned))
    run_state["status"] = "running"
    run_state["outcome"] = "continue"

    errors = [e for e in (state.get("errors") or []) if e != "context_overflow"]

    return {
        "run": run_state,
        "history": replace_state_list(pruned),
        "errors": errors,
        "_budget_compaction": False,
        "_compacted_history": pruned,
    }
