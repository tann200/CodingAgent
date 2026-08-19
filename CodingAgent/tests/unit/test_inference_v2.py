"""Unit tests for Phase 0 inference module (ARCHITECTURE_V2).

Tests:
- model_capability_profile
- hardware_capability_profile
- runtime_profile
- workflow_selector
"""

import pytest

from src.core.inference import (
    ModelProfile,
    ModelTier,
    Architecture,
    ThinkingMode,
    AgentMode,
    get_model_profile,
    select_agent_mode,
    get_agent_mode_settings,
    list_available_profiles,
)
from src.core.inference import (
    HardwareProfile,
    HARDWARE_PROFILES,
    get_hardware_profile,
    compute_safe_context_tokens,
    list_hardware_profiles,
)
from src.core.inference import (
    RuntimeProfile,
    merge_profiles,
    get_runtime_profile,
    preview_runtime,
)
from src.core.inference import (
    WorkflowType,
    WorkflowConfig,
    select_workflow,
    select_workflow_from_names,
    should_use_single_loop,
    LoopControl,
    get_loop_control,
)


class TestModelCapabilityProfile:
    def test_primary_profiles_exist(self):
        profiles = list_available_profiles()
        assert "gemma-4-27b-a4b" in profiles
        assert "qwen3.5-9b" in profiles

    def test_get_qwen_profile(self):
        profile = get_model_profile("qwen3.5-9b")
        assert profile.params_total == 9.0
        assert profile.architecture == Architecture.GDN
        assert profile.thinking_mode == ThinkingMode.OFF
        assert profile.tool_limit == 50

    def test_get_gemma_profile(self):
        profile = get_model_profile("gemma-4-27b-a4b")
        assert profile.params_total == 27.0
        assert profile.params_active == 4.0
        assert profile.architecture == Architecture.MOE
        assert profile.thinking_mode == ThinkingMode.AUTO

    def test_weights_calculation(self):
        profile = get_model_profile("qwen3.5-9b")
        assert profile.weights_gb == 6.6
        assert profile.compute_weights_gb() == 6.6

    def test_kv_cache_calculation(self):
        profile = get_model_profile("qwen3.5-9b")
        kv_1k = profile.compute_kv_cache_gb(1000)
        assert kv_1k > 0 and kv_1k < 1.0

    def test_agent_mode_selection(self):
        assert select_agent_mode(9.0, is_local=True) == AgentMode.LITE
        assert select_agent_mode(14.0, is_local=True) == AgentMode.LITE
        assert select_agent_mode(27.0, is_local=True) == AgentMode.STANDARD
        assert select_agent_mode(9.0, is_local=False) == AgentMode.FULL

    def test_agent_mode_settings(self):
        lite = get_agent_mode_settings(AgentMode.LITE)
        assert lite.tool_limit == 15
        assert lite.thinking_mode == ThinkingMode.OFF
        assert lite.verification is False
        assert lite.vector_memory is False

        standard = get_agent_mode_settings(AgentMode.STANDARD)
        assert standard.tool_limit == 35
        assert standard.verification is True


class TestHardwareCapabilityProfile:
    def test_predefined_profiles(self):
        assert "rtx5070ti-16g" in HARDWARE_PROFILES
        assert "m4-mac-24g" in HARDWARE_PROFILES
        assert "cloud" in HARDWARE_PROFILES

    def test_get_hardware_profile(self):
        profile = get_hardware_profile("rtx5070ti-16g")
        assert profile.vram_gb == 16.0
        assert profile.ram_gb == 64.0
        assert profile.cpu_cores == 6

    def test_safe_context_calculation(self):
        tokens = compute_safe_context_tokens(
            vram_gb=16.0,
            model_weights_gb=5.5,
            kv_per_token_mb=1.2,
            overhead_gb=0.5,
        )
        assert tokens >= 8192  # Minimum floor

    def test_list_profiles(self):
        profiles = list_hardware_profiles()
        assert len(profiles) > 0
        assert "auto" not in profiles  # auto is not a selectable profile


class TestRuntimeProfile:
    def test_merge_profiles(self):
        model = get_model_profile("qwen3.5-9b")
        hardware = get_hardware_profile("rtx5070ti-16g")
        runtime = merge_profiles(model, hardware)

        assert runtime.model_tier == ModelTier.LARGE
        assert runtime.safe_context_tokens >= 8192
        assert runtime.tool_limit <= 50
        assert runtime.thinking_mode == ThinkingMode.OFF  # profile overrides tier default

    def test_runtime_vram_check(self):
        model = get_model_profile("qwen3.5-9b")
        hardware = get_hardware_profile("rtx5070ti-16g")
        runtime = merge_profiles(model, hardware)

        assert runtime.will_fit_in_vram(8000) is True
        assert runtime.compute_vram_usage(8000) > 0

    def test_preview_runtime(self):
        result = preview_runtime("qwen3-14b", 16.0)
        assert result["model"] == "qwen3-14b"
        assert result["vram_gb"] == 16.0
        assert result["model_tier"] == "small"
        assert "safe_context_tokens" in result
        assert "will_fit" in result

    def test_is_cloud(self):
        model = get_model_profile("gpt-4o")
        hardware = get_hardware_profile("cloud")
        runtime = merge_profiles(model, hardware)
        assert runtime.is_cloud is True

    def test_is_small_model(self):
        model = get_model_profile("qwen3.5-9b")
        hardware = get_hardware_profile("rtx5070ti-16g")
        runtime = merge_profiles(model, hardware)
        assert runtime.is_small_model is True


class TestWorkflowSelector:
    def test_single_loop_for_small_model(self):
        workflow = select_workflow_from_names("gemma-4-4b", "rtx5070ti-16g")
        assert workflow.workflow_type == WorkflowType.SINGLE_LOOP
        assert workflow.use_verification is False
        assert workflow.use_replan is False
        assert workflow.use_vector_memory is False

    def test_capable_loop_for_medium_model(self):
        workflow = select_workflow_from_names("gemma-4-26b-a4b", "rtx5070ti-16g")
        assert workflow.workflow_type == WorkflowType.CAPABLE_LOOP
        assert workflow.use_verification is True
        assert workflow.context_limit >= 8192

    def test_capable_loop_for_large_qwen(self):
        """qwen3.5-9b is LARGE tier (262K context) — capable loop."""
        workflow = select_workflow_from_names("qwen3.5-9b", "rtx5070ti-16g")
        assert workflow.workflow_type == WorkflowType.CAPABLE_LOOP

    def test_should_use_single_loop(self):
        assert should_use_single_loop("gemma-4-4b", "rtx5070ti-16g") is True
        assert should_use_single_loop("gemma-4-26b-a4b", "rtx5070ti-16g") is False
        assert should_use_single_loop("gpt-4o") is False

    def test_loop_control_small(self):
        runtime = get_runtime_profile("gemma-4-4b", "rtx5070ti-16g")
        control = get_loop_control(runtime)

        assert control.max_llm_calls == 6
        assert control.llm_calls_remaining == 6
        assert control.should_stop is False

        control.increment()
        assert control.llm_calls_remaining == 5
        assert control.turns_used == 1

    def test_loop_control_small_full_stop(self):
        runtime = get_runtime_profile("gemma-4-4b", "rtx5070ti-16g")
        control = get_loop_control(runtime)

        for _ in range(control.max_llm_calls):
            control.increment()

        assert control.should_stop is True

    def test_loop_control_large(self):
        """qwen3.5-9b is LARGE — higher LLM call budget."""
        runtime = get_runtime_profile("qwen3.5-9b", "rtx5070ti-16g")
        control = get_loop_control(runtime)

        assert control.max_llm_calls == 15

    def test_gpt_cloud_workflow(self):
        workflow = select_workflow_from_names("gpt-4o")
        assert workflow.workflow_type == WorkflowType.CAPABLE_LOOP
        assert workflow.max_turns >= 40


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
