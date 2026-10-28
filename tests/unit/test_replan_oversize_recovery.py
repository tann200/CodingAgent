import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
from src.core.orchestration.graph.nodes.replan_node import replan_node
from src.core.orchestration.graph.state import AgentState


@pytest.mark.asyncio
async def test_replan_node_empty_current_plan_synthesizes_step():
    """Finding 47: When current_plan is empty (e.g. from frontier_loop),
    replan_node must synthesize a task-level step and prompt the LLM to split it,
    rather than returning an error 'current_plan is empty'."""
    state: AgentState = {  # type: ignore[assignment]
        "task": "Refactor authentication system to use JWT tokens",
        "original_task": "Refactor authentication system to use JWT tokens",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": {"ok": True, "requires_split": True},
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "errors": [],
        "current_plan": [],  # Empty plan from frontier loop
        "current_step": 0,
        "replan_required": "Patch exceeded 200 lines. Split into multiple targeted functions.",
        "action_failed": True,
        "replan_attempts": 0,
        "total_recovery_attempts": 0,
        "session_id": "test_sess_47",
    }

    config = MagicMock()
    orch_mock = MagicMock()
    orch_mock.tool_registry.tools = {}
    orch_mock.cancel_event = None

    mock_llm_response = {
        "choices": [
            {
                "message": {
                    "content": (
                        '[\n'
                        '  {"description": "Create JWT token generator helper", "completed": false},\n'
                        '  {"description": "Update login handler to issue JWT", "completed": false}\n'
                        ']'
                    )
                }
            }
        ]
    }

    with patch(
        "src.core.orchestration.graph.nodes.replan_node._resolve_orchestrator",
        return_value=orch_mock,
    ), patch(
        "src.core.orchestration.graph.nodes.replan_node.call_model",
        new=AsyncMock(return_value=mock_llm_response),
    ):
        result = await replan_node(state, config)

    # Must NOT contain "current_plan is empty"
    errors = result.get("errors") or []
    assert "current_plan is empty" not in errors
    assert result.get("replan_required") is None
    assert result.get("action_failed") is False

    # current_plan must be populated with the split steps
    new_plan = result.get("current_plan")
    assert isinstance(new_plan, list)
    assert len(new_plan) == 2
    assert new_plan[0]["description"] == "Create JWT token generator helper"
    assert new_plan[1]["description"] == "Update login handler to issue JWT"
    assert result.get("replan_attempts") == 1


@pytest.mark.asyncio
async def test_replan_node_empty_current_plan_llm_failure_does_not_poison_with_empty_plan():
    """When LLM replan fails on an empty plan, it returns replan failure,
    not 'current_plan is empty'."""
    state: AgentState = {  # type: ignore[assignment]
        "task": "Fix database connection leak",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": None,
        "rounds": 0,
        "working_dir": ".",
        "system_prompt": "",
        "errors": [],
        "current_plan": None,  # None plan
        "current_step": 0,
        "replan_required": "Patch exceeded size limit",
        "action_failed": True,
        "replan_attempts": 0,
        "total_recovery_attempts": 0,
    }

    config = MagicMock()
    orch_mock = MagicMock()
    orch_mock.tool_registry.tools = {}
    orch_mock.cancel_event = None

    with patch(
        "src.core.orchestration.graph.nodes.replan_node._resolve_orchestrator",
        return_value=orch_mock,
    ), patch(
        "src.core.orchestration.graph.nodes.replan_node.call_model",
        new=AsyncMock(return_value={"choices": [{"message": {"content": "not json"}}]}),
    ):
        result = await replan_node(state, config)

    errors = result.get("errors") or []
    assert "current_plan is empty" not in errors
    assert any("Failed to generate smaller steps" in e for e in errors)
    assert result.get("replan_attempts") == 1


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
