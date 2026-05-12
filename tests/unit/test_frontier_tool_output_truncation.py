import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.orchestration.graph.nodes.frontier_loop_node import (
    _TOOL_OUTPUT_MAX_BYTES,
    _extract_content_text,
    _render_tool_calls_text,
    _truncate_tool_output,
    frontier_loop_node,
)


def test_frontier_truncate_tool_output_uses_frontier_marker():
    result = _truncate_tool_output({"ok": True, "output": "x" * 60_000})

    assert result["_output_truncated"] is True
    assert "frontier_loop" in result["output"]
    assert len(json.dumps(result, default=str).encode()) <= _TOOL_OUTPUT_MAX_BYTES + 200


def test_extract_content_text_reads_normalized_choice_message_content():
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "normalized content",
                }
            }
        ]
    }

    assert _extract_content_text(response) == "normalized content"


def test_render_tool_calls_text_serializes_native_tool_calls():
    tool_calls = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "main.py"}',
            },
        }
    ]

    rendered = _render_tool_calls_text(tool_calls)

    assert '"name": "read_file"' in rendered
    assert '"path": "main.py"' in rendered


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
    assert result["history"][0]["content"] == "done"
    assert "conversation_history" not in result


@pytest.mark.asyncio
async def test_frontier_loop_marks_natural_completion_without_tool_calls():
    orch = SimpleNamespace(
        working_dir=None,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
    )
    state = {
        "task": "say hi",
        "history": [],
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

    assert result["last_result"]["ok"] is True
    assert result["last_result"]["completed_without_tool"] is True
    assert result["last_result"]["_completion_detected"] is True


@pytest.mark.asyncio
async def test_frontier_loop_reads_choice_level_tool_calls_from_normalized_response():
    orch = SimpleNamespace(
        working_dir=None,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
        execute_tool=lambda action: {"ok": True, "path": action["args"]["path"]},
    )
    state = {
        "task": "read a file",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
    }
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": ""},
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "main.py"}',
                        },
                    }
                ],
            }
        ]
    }

    with (
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
            return_value=orch,
        ),
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            new=AsyncMock(side_effect=[response, {"choices": [{"message": {"content": "done"}}]}]),
        ),
    ):
        result = await frontier_loop_node(state, config={})

    assert result["tool_call_count"] == 1
    assert result["last_result"]["completed_without_tool"] is True
    assert any(msg.get("role") == "assistant" and '"name": "read_file"' in msg.get("content", "") for msg in result["history"])
    # Tool result is appended as a user message (legacy_history), not a raw tool role message
    assert any(msg.get("role") == "user" and "tool_execution_result" in msg.get("content", "") for msg in result["history"])


@pytest.mark.asyncio
async def test_frontier_loop_executes_precomputed_next_action_before_llm():
    executed = []
    orch = SimpleNamespace(
        working_dir=None,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
        execute_tool=lambda action: executed.append(action) or {"ok": True, "path": action["arguments"]["path"]},
    )
    state = {
        "task": "read a file",
        "history": [],
        "next_action": {"name": "read_file", "arguments": {"path": "main.py"}},
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
        ) as mock_call_model,
    ):
        result = await frontier_loop_node(state, config={})

    assert executed == [{"name": "read_file", "arguments": {"path": "main.py"}}]
    assert mock_call_model.await_count == 1
    assert result["tool_call_count"] == 1
    assert any(msg.get("role") == "assistant" and '"name": "read_file"' in msg.get("content", "") for msg in result["history"])
    assert any(msg.get("role") == "user" and "tool_execution_result" in msg.get("content", "") for msg in result["history"])
    # Raw tool role messages are no longer appended; result is in legacy_history as user message


@pytest.mark.asyncio
async def test_frontier_loop_forwards_resolved_provider_and_model():
    captured = {}
    orch = SimpleNamespace(
        working_dir=None,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
        adapter=SimpleNamespace(),
        _provider_name="lm_studio",
        model="google/gemma-4-26B-A4B",
    )
    state = {
        "task": "say hi",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
    }

    async def _mock_call_model(*args, **kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "done"}}]}

    with (
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
            return_value=orch,
        ),
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            new=_mock_call_model,
        ),
    ):
        await frontier_loop_node(state, config={})

    assert captured.get("provider") == "lm_studio"
    assert captured.get("model") == "google/gemma-4-26B-A4B"


@pytest.mark.asyncio
async def test_frontier_loop_injects_write_required_context_after_read_for_modifying_task(tmp_path):
    orch = SimpleNamespace(
        working_dir=str(tmp_path),
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
        execute_tool=lambda action: {
            "ok": True,
            "result": {"status": "ok", "content": "def add(a, b)\n    return a + b\n"},
        },
    )
    state = {
        "task": "Edit buggy.py to fix the syntax error",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
        "working_dir": str(tmp_path),
        "next_action": {"name": "read_file", "arguments": {"path": "buggy.py"}},
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

    payloads = [
        json.loads(msg["content"])
        for msg in result["history"]
        if msg.get("role") == "user" and "tool_execution_result" in msg.get("content", "")
    ]
    assert any(payload.get("orchestration_hint") == "write_required" for payload in payloads)
    assert any(payload.get("file_path") == "buggy.py" for payload in payloads)
