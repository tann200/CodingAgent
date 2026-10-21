"""Initialization Node for the unified 7-node LangGraph cognitive pipeline.

Performs deterministic initialization per Engineering Audit §Appendix:
- Resolves provider, model, tier, capability set, and immutable policy.
- Prepares target state contract: runtime, run, pending_action, last_action, verification.
- Performs NO LLM call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    span_node as _span_node,
)
from src.core.orchestration.graph.policy import (
    create_run_state,
    create_runtime_state,
    normalize_target_tier,
)
from src.core.orchestration.graph.state import StateLike

logger = logging.getLogger(__name__)


async def initialize_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _initialize_node_impl."""
    with _span_node("initialize", {"turn": state.get("turn_count", 0)}):
        return await _initialize_node_impl(state, config)


async def _initialize_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Deterministic initialization of target graph state."""
    logger.info("=== initialize_node START ===")
    orchestrator = _resolve_orchestrator(state, config)

    # 1. Resolve or preserve runtime state
    runtime = state.get("runtime")
    if not isinstance(runtime, Mapping) or not runtime.get("tier"):
        tier = "medium"
        provider = ""
        model = ""
        try:
            from src.core.inference.model_tiers import classify_model
            from src.core.inference.provider_utils import resolve_provider_and_model

            provider, model = resolve_provider_and_model(orchestrator)
            adapter = getattr(orchestrator, "_adapter", None) or getattr(orchestrator, "adapter", None)
            ctx_window = int(getattr(adapter, "context_window", 0) or 0)
            tier_enum = classify_model(model or "", ctx_window)
            tier = normalize_target_tier(tier_enum)
        except Exception as exc:
            logger.debug("initialize_node: error resolving model tier: %s", exc)

        runtime = create_runtime_state(tier=tier, provider=provider, model=model)
    else:
        runtime = dict(runtime)

    # 2. Resolve or preserve run state
    run = state.get("run")
    if not isinstance(run, Mapping) or not run.get("status"):
        run = create_run_state(status="running", outcome="continue")
    else:
        run = dict(run)

    tier_name = str(runtime.get("tier") or "medium")

    return {
        "runtime": runtime,
        "run": run,
        "pending_action": None,
        "last_action": None,
        "verification": None,
        "modified_files": list(state.get("modified_files") or []),
        "errors": list(state.get("errors") or []),
        "model_tier": tier_name,
    }
