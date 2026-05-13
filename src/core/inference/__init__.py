"""v2 inference module exports.

Phase 0: Two-axis profiling (ARCHITECTURE_V2.md)
Phase 1: workflow_selector integration
Phase 2: kv_cache_governor, thinking_utils
"""

from .model_tiers import (
    ModelTier,
    TierConfig,
    get_tier_config,
    classify_model,
    get_tool_limit,
    get_plan_step_limit,
    get_max_turns,
    supports_native_tools,
    is_simple_mode,
)
from .model_capability_profile import (
    ModelProfile,
    Architecture,
    ThinkingMode,
    AgentMode,
    AgentModeSettings,
    get_model_profile,
    select_agent_mode,
    get_agent_mode_settings,
    list_available_profiles,
)
from .hardware_capability_profile import (
    HardwareProfile,
    HARDWARE_PROFILES,
    get_hardware_profile,
    detect_hardware,
    compute_safe_context_tokens,
    list_hardware_profiles,
)
from .runtime_profile import (
    RuntimeProfile,
    merge_profiles,
    get_runtime_profile,
    preview_runtime,
)
from .workflow_selector import (
    WorkflowType,
    WorkflowConfig,
    select_workflow,
    select_workflow_from_names,
    should_use_single_loop,
    LoopControl,
    get_loop_control,
)
from .kv_cache_governor import (
    KVCacheGovernor,
    CompactionAction,
    KVCacheState,
    create_governor_for_model,
)
from .thinking_utils import (
    is_reasoning_model,
    supports_no_think,
    strip_thinking,
    budget_max_tokens,
    resolve_thinking_mode,
    get_thinking_directive,
)

__all__ = [
    # model_tiers
    "ModelTier",
    "TierConfig",
    "get_tier_config",
    "classify_model",
    "get_tool_limit",
    "get_plan_step_limit",
    "get_max_turns",
    "supports_native_tools",
    "is_simple_mode",
    # model_capability_profile
    "ModelProfile",
    "Architecture",
    "ThinkingMode",
    "AgentMode",
    "AgentModeSettings",
    "get_model_profile",
    "select_agent_mode",
    "get_agent_mode_settings",
    "list_available_profiles",
    # hardware_capability_profile
    "HardwareProfile",
    "HARDWARE_PROFILES",
    "get_hardware_profile",
    "detect_hardware",
    "compute_safe_context_tokens",
    "list_hardware_profiles",
    # runtime_profile
    "RuntimeProfile",
    "merge_profiles",
    "get_runtime_profile",
    "preview_runtime",
    # workflow_selector
    "WorkflowType",
    "WorkflowConfig",
    "select_workflow",
    "select_workflow_from_names",
    "should_use_single_loop",
    "LoopControl",
    "get_loop_control",
    # kv_cache_governor
    "KVCacheGovernor",
    "CompactionAction",
    "KVCacheState",
    "create_governor_for_model",
    # thinking_utils
    "is_reasoning_model",
    "supports_no_think",
    "strip_thinking",
    "budget_max_tokens",
    "resolve_thinking_mode",
    "get_thinking_directive",
]
