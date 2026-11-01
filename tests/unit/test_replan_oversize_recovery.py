import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
from src.core.orchestration.graph.state import AgentState


@pytest.mark.asyncio
async def test_frontier_loop_injects_current_plan_into_task():
    """Verify frontier_loop_node appends execution plan to task prompt when current_plan exists."""
    state: AgentState = {  # type: ignore[assignment]
        "task": "Implement feature X",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
        "errors": [],
        "next_action": None,
        "files_read": {},
        "tool_last_used": {},
        "working_dir": "/tmp",
        "current_plan": [
            {"description": "Step 1: schema", "completed": True},
            {"description": "Step 2: endpoint", "completed": False},
        ],
    }

    captured_messages = []

    async def mock_call_llm_for_turn(messages, **kwargs):
        captured_messages.extend(messages)
        return {"choices": [{"message": {"content": "Done"}}]}, "Done", []

    with patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._call_llm_for_turn",
        side_effect=mock_call_llm_for_turn,
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
        return_value=MagicMock(),
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node.build_repo_context_block",
        return_value="",
    ):
        result = await frontier_loop_node(state, MagicMock())

    assert len(captured_messages) > 0
    prompt_texts = [m.get("content", "") for m in captured_messages if isinstance(m, dict)]
    full_prompt = "\n".join(prompt_texts)
    assert "Execution Plan (split steps to execute):" in full_prompt
    assert "1. [x] Step 1: schema" in full_prompt
    assert "2. [ ] Step 2: endpoint" in full_prompt


@pytest.mark.asyncio
async def test_frontier_loop_oversize_patch_triggers_replan_with_requires_split():
    """Verify frontier_loop_node exits with replan_required when patch is oversized,
    routing to replan_node for task decomposition."""
    state: AgentState = {  # type: ignore[assignment]
        "task": "Implement large refactor",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
        "errors": [],
        "next_action": None,
        "files_read": {},
        "tool_last_used": {},
        "working_dir": "/tmp",
    }

    call_count = 0

    async def mock_call_llm_for_turn(messages, **kwargs):
        nonlocal call_count
        call_count += 1
        tool_call = {
            "id": f"tc_{call_count}",
            "function": {"name": "write_file", "arguments": '{"path": "a.py"}'},
        }
        return {"choices": [{"message": {"content": "Writing file"}}]}, "Writing file", [tool_call]

    async def mock_dispatch_tool_calls(**kwargs):
        return (
            [{"role": "tool", "content": "oversize"}],
            kwargs.get("tool_call_count", 0) + 1,
            {"ok": True, "requires_split": True, "split_recommendation": "Split edits"},
            "write_file",
            [],
        )

    orch_mock = MagicMock()
    orch_mock.plan_mode = None
    orch_mock.event_bus = None
    orch_mock.cancel_event = None

    with patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._call_llm_for_turn",
        side_effect=mock_call_llm_for_turn,
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._dispatch_tool_calls",
        side_effect=mock_dispatch_tool_calls,
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
        return_value=orch_mock,
    ), patch(
        "src.core.orchestration.graph.nodes.frontier_loop_node.build_repo_context_block",
        return_value="",
    ):
        result = await frontier_loop_node(state, MagicMock())

    assert call_count == 1
    assert result.get("replan_required") is not None
    assert "Patch exceeded" in result.get("replan_required")
