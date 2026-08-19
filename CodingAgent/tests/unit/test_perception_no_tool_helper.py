from types import SimpleNamespace


def _make_orchestrator(events_list: list):
    class EventBus:
        def publish(self, event_name, payload):
            events_list.append((event_name, payload))

    return SimpleNamespace(event_bus=EventBus())


def test_handle_no_tool_early_exit_small_tier():
    from src.core.orchestration.graph.nodes.perception_node import (
        _handle_no_tool_or_empty_response,
    )

    events: list = []
    orchestrator = _make_orchestrator(events)

    content = "Model emitted a non-tool completion"
    state = {"empty_response_count": 1, "rounds": 2, "session_id": "s1"}

    # small tier: _max_corrective_2 == 2, starting at 1 -> increments to 2 -> early exit
    res = _handle_no_tool_or_empty_response(
        content=content,
        content_stripped=content.strip(),
        thinking_only=False,
        _is_truncated_yaml=False,
        state=state,
        orchestrator=orchestrator,
        _model_tier_str="small",
    )

    assert isinstance(res, dict)
    assert res.get("errors") and "infinite_loop_no_tool" in res.get("errors")
    assert res.get("empty_response_count") == 0
    assert res.get("history") == [{"role": "assistant", "content": content}]


def test_handle_no_tool_emits_corrective_prompt_truncated_yaml():
    from src.core.orchestration.graph.nodes.perception_node import (
        _handle_no_tool_or_empty_response,
    )

    events: list = []
    orchestrator = _make_orchestrator(events)

    content = "Here is a partial answer:\n```yaml\nthis is not a valid yaml block\n```"
    state = {"empty_response_count": 0, "rounds": 0, "session_id": "s1"}

    res = _handle_no_tool_or_empty_response(
        content=content,
        content_stripped=content.strip(),
        thinking_only=False,
        _is_truncated_yaml=True,
        state=state,
        orchestrator=orchestrator,
        _model_tier_str="small",
    )

    # Should have returned corrective messages and updated the empty_response_count
    assert isinstance(res, dict)
    assert res.get("empty_response_count") == 1
    h = res.get("history")
    assert isinstance(h, list) and len(h) == 2
    assert h[0]["role"] == "assistant"
    assert h[1]["role"] == "user"

    # Ensure corrective prompt event was published with truncated_yaml flag
    found = False
    for ename, payload in events:
        if ename == "perception.corrective_prompt" and payload.get("truncated_yaml"):
            found = True
            break
    assert found, f"Truncated YAML corrective event not published: {events}"


def test_maybe_return_content_after_no_tool_retry_returns_final_answer():
    from src.core.orchestration.graph.nodes.perception_node import (
        _maybe_return_content_after_no_tool_retry,
    )

    state = {"empty_response_count": 1, "history": [{"role": "user", "content": "hi"}]}

    res = _maybe_return_content_after_no_tool_retry(
        content_no_thinking="Here is the answer.",
        state=state,
        rounds_now=2,
        turn_count=3,
        model_tier_str="small",
    )

    assert isinstance(res, dict)
    assert res["next_action"] is None
    assert res["rounds"] == 3
    assert res["turn_count"] == 3
    assert res["empty_response_count"] == 0
    assert res["model_tier"] == "small"
    assert res["history"] == [{"role": "assistant", "content": "Here is the answer."}]


def test_maybe_return_content_after_no_tool_retry_rejects_thinking_like_content():
    from src.core.orchestration.graph.nodes.perception_node import (
        _maybe_return_content_after_no_tool_retry,
    )

    state = {"empty_response_count": 1}

    res = _maybe_return_content_after_no_tool_retry(
        content_no_thinking="Let me think about this first, then I will proceed.",
        state=state,
        rounds_now=0,
        turn_count=1,
        model_tier_str=None,
    )

    assert res is None
