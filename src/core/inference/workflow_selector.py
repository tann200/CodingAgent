"""workflow_selector.py — Binary workflow selection (SMALL vs MEDIUM+).

Replaces tier-based routing with two modes:
- SMALL: Single-loop ReAct, minimal tools, no verification
- MEDIUM+: Full pipeline with verification, deeper reasoning
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from .runtime_profile import RuntimeProfile, get_runtime_profile
from .model_capability_profile import AgentMode, ThinkingMode

logger = logging.getLogger(__name__)


class WorkflowType(str, Enum):
    SINGLE_LOOP = "single_loop"  # ReAct++ for small models
    FRONTIER_LOOP = "frontier_loop"  # Multi-turn loop for medium+


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
    - SMALL/LITE mode → SINGLE_LOOP
    - STANDARD/FULL mode → FRONTIER_LOOP
    """
    is_small = runtime.agent_mode == AgentMode.LITE
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
            workflow_type=WorkflowType.FRONTIER_LOOP,
            context_limit=runtime.safe_context_tokens,
            tool_limit=runtime.tool_limit,
            max_turns=runtime.max_turns,
            thinking_mode=runtime.thinking_mode,
            use_verification=runtime.use_verification,
            use_replan=runtime.use_replan,
            use_vector_memory=runtime.use_vector_memory,
            graph_type="frontier_8node"
            if runtime.agent_mode == AgentMode.STANDARD
            else "full_16node",
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
    return runtime.agent_mode == AgentMode.LITE and not runtime.is_cloud


@dataclass
class LoopControl:
    max_llm_calls: int
    max_turns: int
    llm_calls_used: int = 0
    turns_used: int = 0

    @property
    def llm_calls_remaining(self) -> int:
        return max(0, self.max_llm_calls - self.llm_calls_used)

    @property
    def turns_remaining(self) -> int:
        return max(0, self.max_turns - self.turns_used)

    @property
    def should_stop(self) -> bool:
        return (
            self.llm_calls_used >= self.max_llm_calls
            or self.turns_used >= self.max_turns
        )

    def increment(self) -> None:
        self.llm_calls_used += 1
        self.turns_used += 1


def get_loop_control(runtime: RuntimeProfile) -> LoopControl:
    """Get loop control settings for a runtime profile."""
    max_llm_calls = {
        AgentMode.LITE: 6,
        AgentMode.STANDARD: 15,
        AgentMode.FULL: 40,
    }.get(runtime.agent_mode, 15)

    return LoopControl(
        max_llm_calls=max_llm_calls,
        max_turns=runtime.max_turns,
    )
