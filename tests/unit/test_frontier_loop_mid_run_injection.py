"""Unit tests for SPEC-P1.4: Mid-run steering injection drain in frontier_loop_node."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node


class MockInjectionSource:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    def pop_pending_injections(self):
        popped = list(self.msgs)
        self.msgs.clear()
        return popped


@pytest.mark.asyncio
async def test_frontier_loop_drains_mid_run_injection():
    """SPEC-P1.4: frontier_loop_node drains pending injections and appends them to history."""
    source = MockInjectionSource(["Focus on fixing the linter errors first."])

    orch = MagicMock()
    orch.working_dir = "."
    orch.event_bus = None
    orch.run_agent_once = MagicMock()
    orch.active_role = "operational"

    state = {
        "task": "Fix the codebase",
        "history": [],
        "max_turns": 2,
        "_pending_injections_source": source,
        "runtime": {"policy": {"turn_limit": 2}},
        "model_tier": "frontier",
    }
    config = {"configurable": {"orchestrator": orch}}

    # Mock _prepare_turn_messages and _call_llm_for_turn so turn completes instantly
    with patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
        new=AsyncMock(return_value=[{"role": "user", "content": "Fix the codebase"}]),
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._call_llm_for_turn",
        new=AsyncMock(return_value=({"choices": [{"message": {"content": "Done"}}]}, "Done", [])),
    ), patch(
        "src.core.inference.provider_utils.resolve_provider_capabilities",
        return_value={"provider_name": "mock", "model": "mock", "supports_native_tools": False},
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node.handle_no_tool_completion",
        return_value={"ok": True},
    ):
        res = await frontier_loop_node(state, config)

    history = res.get("history") or []
    # Assert that the operator steering message was added to history
    steering_entries = [
        msg for msg in history
        if msg.get("role") == "user" and "<operator_steering>" in msg.get("content", "")
    ]
    assert len(steering_entries) == 1
    assert "Focus on fixing the linter errors first." in steering_entries[0]["content"]
    # Assert source was drained
    assert source.pop_pending_injections() == []
