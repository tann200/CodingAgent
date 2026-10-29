"""tests/unit/test_workflow_authority.py — Finding 48 regression coverage.

Verifies that WorkflowConfig is the authoritative configuration source for:
1. Runtime graph compilation and tier selection (single_loop vs capable_loop).
2. Runtime state limits (tool_limit, max_turns, context_limit).
3. Runtime capabilities (use_verification, use_replan, use_vector_memory).
4. Exit routing for verification and replan bypasses when disabled.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.inference.model_capability_profile import ThinkingMode
from src.core.inference.workflow_selector import (
    WorkflowConfig,
    WorkflowType,
    resolve_workflow_config,
    select_workflow_from_names,
)
from src.core.orchestration.graph.builder import (
    _resolve_graph_tier,
    get_compiled_graph_for_orchestrator,
)
from src.core.orchestration.graph.canonical_routing import route_after_agent_loop
from src.core.orchestration.graph.tier_graph_routing import route_frontier_loop_exit
from src.core.orchestration.inference_loop_state import (
    build_initial_state,
    resolve_max_turns,
)


def test_resolve_workflow_config_respects_explicit_orchestrator_config():
    custom_wf = WorkflowConfig(
        workflow_type=WorkflowType.SINGLE_LOOP,
        context_limit=8192,
        tool_limit=10,
        max_turns=15,
        thinking_mode=ThinkingMode.OFF,
        use_verification=False,
        use_replan=False,
        use_vector_memory=False,
        graph_type="single_loop",
    )
    orch = MagicMock()
    orch.workflow_config = custom_wf

    resolved = resolve_workflow_config(orch)
    assert resolved == custom_wf
    assert resolved.tool_limit == 10
    assert resolved.max_turns == 15


def test_resolve_workflow_config_derives_from_model_and_hardware():
    # Small model on local hardware derives SINGLE_LOOP
    small_wf = resolve_workflow_config(
        model="gemma-4-4b", hardware_name="rtx5070ti-16g"
    )
    assert small_wf.workflow_type == WorkflowType.SINGLE_LOOP
    assert small_wf.use_verification is False
    assert small_wf.use_replan is False
    assert small_wf.tool_limit <= 15
    assert small_wf.max_turns <= 25

    # Capable model derives CAPABLE_LOOP
    capable_wf = resolve_workflow_config(model="gpt-4o")
    assert capable_wf.workflow_type == WorkflowType.CAPABLE_LOOP
    assert capable_wf.use_verification is True
    assert capable_wf.use_replan is True
    assert capable_wf.max_turns >= 40


def test_graph_builder_tier_driven_by_workflow_config():
    # When workflow config indicates SINGLE_LOOP, tier is resolved as "lite"
    orch_small = MagicMock()
    orch_small.workflow_config = WorkflowConfig(
        workflow_type=WorkflowType.SINGLE_LOOP,
        context_limit=8192,
        tool_limit=10,
        max_turns=15,
        thinking_mode=ThinkingMode.OFF,
        use_verification=False,
        use_replan=False,
        use_vector_memory=False,
        graph_type="single_loop",
    )
    with patch(
        "src.core.orchestration.graph.builder.build_tier_graph"
    ) as mock_build:
        get_compiled_graph_for_orchestrator(orch_small)
        mock_build.assert_called_once_with("lite")


def test_graph_builder_tier_for_capable_loop():
    orch_capable = MagicMock()
    orch_capable.workflow_config = WorkflowConfig(
        workflow_type=WorkflowType.CAPABLE_LOOP,
        context_limit=128000,
        tool_limit=50,
        max_turns=60,
        thinking_mode=ThinkingMode.ON,
        use_verification=True,
        use_replan=True,
        use_vector_memory=True,
        graph_type="capable_loop",
    )
    # Mock model classify to return "frontier"
    with patch(
        "src.core.orchestration.graph.builder.build_tier_graph"
    ) as mock_build, patch(
        "src.core.inference.provider_utils.resolve_provider_capabilities",
        return_value={"model": "gpt-4o"},
    ):
        get_compiled_graph_for_orchestrator(orch_capable)
        mock_build.assert_called_once_with("frontier")


def test_initial_state_limits_and_capabilities_reflect_workflow_config():
    orch = MagicMock()
    orch._current_task_id = "task_wf_48"
    orch.working_dir = "/tmp/test"
    orch.msg_mgr.messages = []
    orch._session_read_files = set()
    orch.workflow_config = WorkflowConfig(
        workflow_type=WorkflowType.SINGLE_LOOP,
        context_limit=8192,
        tool_limit=12,
        max_turns=18,
        thinking_mode=ThinkingMode.OFF,
        use_verification=False,
        use_replan=False,
        use_vector_memory=False,
        graph_type="single_loop",
    )

    state = build_initial_state(
        orch=orch,
        prompt="Test task",
        full_system_prompt="System instructions",
        cancel_event=None,
    )

    assert state["max_tool_calls"] == 12
    assert state["tool_limit"] == 12
    assert state["max_turns"] == 18
    assert state["context_limit"] == 8192
    assert state["use_verification"] is False
    assert state["use_replan"] is False
    assert state["use_vector_memory"] is False
    assert state["workflow_type"] == "single_loop"
    assert state["thinking_mode"] == "off"

    # Verify resolve_max_turns respects state max_turns from WorkflowConfig
    resolved_turns = resolve_max_turns(initial_state=state)
    assert resolved_turns == 18


def test_routing_bypasses_verification_and_replan_when_disabled():
    # In frontier graph routing:
    state_no_verify = {
        "last_result": {"ok": True},
        "use_verification": False,
    }
    assert route_frontier_loop_exit(state_no_verify) == "memory_sync"

    state_no_replan = {
        "replan_required": "Oversize patch",
        "use_replan": False,
    }
    assert route_frontier_loop_exit(state_no_replan) == "memory_sync"

    # In canonical graph routing:
    assert (
        route_after_agent_loop(
            {
                "run": {"outcome": "verify"},
                "use_verification": False,
            }
        )
        == "finalize"
    )

    assert (
        route_after_agent_loop(
            {
                "run": {"outcome": "recover"},
                "use_replan": False,
            }
        )
        == "finalize"
    )
