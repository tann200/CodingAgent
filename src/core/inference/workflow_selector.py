"""workflow_selector.py — Binary workflow selection (SMALL vs MEDIUM+).

Replaces tier-based routing with two modes:
- SMALL: Single-loop ReAct, minimal tools, no verification
- MEDIUM+: Full pipeline with verification, deeper reasoning
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .runtime_profile import RuntimeProfile, get_runtime_profile
from .model_tiers import ModelTier
from .model_capability_profile import ThinkingMode

logger = logging.getLogger(__name__)


class WorkflowType(str, Enum):
    SINGLE_LOOP = "single_loop"  # ReAct++ for small models
    CAPABLE_LOOP = "capable_loop"  # Multi-turn loop for medium+


@dataclass
class WorkflowConfig:
    workflow_type: WorkflowType
    context_limit: int
    tool_limit: int
    max_turns: int
    thinking_mode: ThinkingMode
    use_verification: bool
    use_replan: bool
    use_vector_memory: bool
    graph_type: str  # For backward compat: collapsed_5node, frontier_8node, etc.


def select_workflow(runtime: RuntimeProfile) -> WorkflowConfig:
    """Select workflow config based on runtime profile.

    Binary decision:
    - SMALL mode → SINGLE_LOOP
    - MEDIUM/LARGE/FRONTIER → CAPABLE_LOOP
    """
    is_small = runtime.model_tier == ModelTier.SMALL
    is_cloud = runtime.is_cloud

    if is_small and not is_cloud:
        return WorkflowConfig(
            workflow_type=WorkflowType.SINGLE_LOOP,
            context_limit=min(runtime.safe_context_tokens, 16384),
            tool_limit=min(runtime.tool_limit, 15),
            max_turns=min(runtime.max_turns, 25),
            thinking_mode=ThinkingMode.OFF,
            use_verification=False,
            use_replan=False,
            use_vector_memory=False,
            graph_type="single_loop",  # For compat with existing graph builder
        )
    else:
        return WorkflowConfig(
            workflow_type=WorkflowType.CAPABLE_LOOP,
            context_limit=runtime.safe_context_tokens,
            tool_limit=runtime.tool_limit,
            max_turns=runtime.max_turns,
            thinking_mode=runtime.thinking_mode,
            use_verification=runtime.use_verification,
            use_replan=runtime.use_replan,
            use_vector_memory=runtime.use_vector_memory,
            graph_type="capable_loop",
        )


def select_workflow_from_names(
    model_name: str,
    hardware_name: str = "auto",
    context_window: int = 0,
) -> WorkflowConfig:
    """Convenience: get workflow config from model + hardware names."""
    runtime = get_runtime_profile(model_name, hardware_name, context_window)
    return select_workflow(runtime)


def should_use_single_loop(model_name: str, hardware_name: str = "auto") -> bool:
    """Quick check: should this model use single-loop orchestrator?"""
    runtime = get_runtime_profile(model_name, hardware_name)
    return runtime.model_tier == ModelTier.SMALL and not runtime.is_cloud


def resolve_workflow_config(
    orchestrator: Any = None,
    *,
    model: str | None = None,
    adapter: Any = None,
    hardware_name: str = "auto",
) -> WorkflowConfig:
    """Resolve the authoritative WorkflowConfig for orchestrator, model, or adapter.

    Checks if orchestrator already defines an explicit workflow_config.
    Otherwise derives WorkflowConfig from active model, adapter context window,
    and hardware profile.
    """
    if orchestrator is not None:
        explicit = getattr(orchestrator, "workflow_config", None)
        if explicit is not None and isinstance(explicit, WorkflowConfig):
            return explicit

    resolved_adapter = adapter
    if resolved_adapter is None and orchestrator is not None:
        resolved_adapter = getattr(orchestrator, "_adapter", None) or getattr(
            orchestrator, "adapter", None
        )

    resolved_model = model
    if not resolved_model and orchestrator is not None:
        try:
            from src.core.inference.provider_utils import resolve_provider_and_model

            _, resolved_model = resolve_provider_and_model(orchestrator)
        except Exception:
            resolved_model = None

    if not resolved_model and resolved_adapter is not None:
        try:
            resolved_model = getattr(resolved_adapter, "default_model", None)
        except Exception:
            resolved_model = None
        if not resolved_model:
            try:
                models_attr = getattr(resolved_adapter, "models", None)
                if isinstance(models_attr, (list, tuple)) and models_attr:
                    resolved_model = str(models_attr[0])
                elif models_attr:
                    resolved_model = str(models_attr)
            except Exception:
                resolved_model = None

    hw = hardware_name
    if hw == "auto" and orchestrator is not None:
        hw = (
            getattr(orchestrator, "hardware_profile", None)
            or getattr(orchestrator, "_hardware_profile", None)
            or "auto"
        )

    context_window = 0
    if resolved_adapter is not None and hasattr(resolved_adapter, "context_window"):
        try:
            context_window = int(resolved_adapter.context_window or 0)
        except Exception:
            context_window = 0

    return select_workflow_from_names(
        model_name=str(resolved_model or "default"),
        hardware_name=str(hw or "auto"),
        context_window=context_window,
    )

