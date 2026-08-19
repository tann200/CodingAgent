from unittest.mock import patch, MagicMock

# ruff: noqa: E501
import pytest


def _make_perception_state(**kwargs):
    base = {
        "task": "test",
        "working_dir": "/tmp",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": None,
        "rounds": 0,
        "system_prompt": "",
        "errors": [],
        "session_id": "s1",
        "current_plan": None,
        "current_step": 0,
        "tool_call_count": 0,
        "empty_response_count": 0,
        "cancel_event": None,
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_perception_emits_corrective_prompt_event_on_empty_response():
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    events = []
    orc = MagicMock()
    orc.adapter = MagicMock()
    orc.cancel_event = None
    orc.msg_mgr = MagicMock()
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))
    def _pt(event):
        from src.core.orchestration.event_bus import _get_event_name_for_class
        name = _get_event_name_for_class(type(event)) or type(event).__name__
        d = event.to_dict()
        d.pop("correlation_id", None)
        d.pop("timestamp", None)
        events.append((name, d))
    orc.event_bus.publish_typed = _pt

    # Simulate an LLM response that is only thinking text
    fake_response = {
        "choices": [{"message": {"content": "<think>planning</think>"}}],
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "total_tokens": 11,
    }

    state = _make_perception_state()
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            return_value=fake_response,
        ),
        patch("src.core.orchestration.graph.nodes.perception_node.ContextBuilder"),
    ):
        result = await perception_node(state, config)

    # Ensure corrective prompt event was published
    assert any(e == "perception.corrective_prompt" for (e, _) in events), (
        f"No corrective prompt event published: {events}"
    )


@pytest.mark.asyncio
async def test_perception_emits_corrective_prompt_event_on_truncated_yaml():
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    events = []
    orc = MagicMock()
    orc.adapter = MagicMock()
    orc.cancel_event = None
    orc.msg_mgr = MagicMock()
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))
    def _pt2(event):
        from src.core.orchestration.event_bus import _get_event_name_for_class
        name = _get_event_name_for_class(type(event)) or type(event).__name__
        d = event.to_dict()
        d.pop("correlation_id", None)
        d.pop("timestamp", None)
        events.append((name, d))
    orc.event_bus.publish_typed = _pt2

    # Simulate truncated/invalid YAML inside a fenced block (no 'name:' key)
    fake_response = {
        "choices": [
            {
                "message": {
                    "content": "Here is a partial answer:\n```yaml\nthis is not a valid yaml block\n```"
                }
            }
        ],
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "total_tokens": 11,
    }

    state = _make_perception_state()
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            return_value=fake_response,
        ),
        patch("src.core.orchestration.graph.nodes.perception_node.ContextBuilder"),
    ):
        result = await perception_node(state, config)

    # Ensure corrective prompt event was published with truncated_yaml flag True
    found = False
    for ename, payload in events:
        if ename == "perception.corrective_prompt":
            if payload.get("truncated_yaml"):
                found = True
                break
    assert found, f"Truncated YAML corrective event not published: {events}"
