import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.orchestration.graph.nodes.frontier_loop_node import (
    _TOOL_OUTPUT_MAX_BYTES,
    _truncate_tool_output,
    frontier_loop_node,
)


def test_frontier_truncate_tool_output_uses_frontier_marker():
    result = _truncate_tool_output({"ok": True, "output": "x" * 60_000})

    assert result["_output_truncated"] is True
    assert "frontier_loop" in result["output"]
    assert len(json.dumps(result, default=str).encode()) <= _TOOL_OUTPUT_MAX_BYTES + 200


@pytest.mark.asyncio
async def test_frontier_loop_uses_canonical_history_key():
    orch = SimpleNamespace(
        working_dir=None,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
    )
    state = {
        "task": "say hi",
        "history": [{"role": "user", "content": "hello"}],
        "conversation_history": [{"role": "user", "content": "stale"}],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
    }

    with (
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
            return_value=orch,
        ),
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            new=AsyncMock(return_value={"choices": [{"message": {"content": "done"}}]}),
        ),
    ):
        result = await frontier_loop_node(state, config={})

    assert "history" in result
    assert "conversation_history" not in result
    assert result["history"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_frontier_loop_ignores_legacy_conversation_history_alias():
    orch = SimpleNamespace(
        working_dir=None,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
    )
    state = {
        "task": "say hi",
        "conversation_history": [{"role": "user", "content": "stale"}],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
    }

    with (
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
            return_value=orch,
        ),
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            new=AsyncMock(return_value={"choices": [{"message": {"content": "done"}}]}),
        ),
    ):
        result = await frontier_loop_node(state, config={})

    assert len(result["history"]) == 1
    assert result["history"][0]["role"] == "assistant"
    assert result["history"][0]["content"] == ""
    assert "conversation_history" not in result
