import pytest

from src.core.orchestration.graph.nodes import perception_runtime
from src.core.orchestration.graph.nodes.perception_runtime import (
    _compute_active_skills_for_task,
    _filter_tools_near_turn_limit,
    _maybe_handle_turn_limit,
    _resolve_orchestrator_and_cancellation,
    _resolve_perception_provider_context,
    _select_perception_role,
    _validate_call_model_and_adapter,
)


def _capture_logger():
    """Return a fake runtime logger recording (level, first-arg message) calls."""
    logged = []

    class _L:
        @staticmethod
        def _record(*a):
            logged.append(("_", a))

        def debug(self, *a):
            logged.append(("debug", a[0]))

        def info(self, *a):
            logged.append(("info", a[0]))

        def warning(self, *a):
            logged.append(("warning", a[0]))

        def error(self, *a):
            logged.append(("error", a[0]))

    logger = _L()
    return logger, logged


def test_resolve_orchestrator_and_cancellation_returns_missing_orchestrator_error(
    monkeypatch,
):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    orchestrator, result = _resolve_orchestrator_and_cancellation(
        state={"rounds": 2},
        config={},
    )

    assert orchestrator is None
    assert result == {
        "history": [],
        "next_action": None,
        "rounds": 3,
        "errors": ["orchestrator not found in config"],
    }
    assert logged == [("error", "perception_node: orchestrator is None in config")]


def test_resolve_orchestrator_and_cancellation_returns_cancel_payload_from_state_event(
    monkeypatch,
):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    class _Event:
        def is_set(self):
            return True

    orch = type("Orch", (), {"cancel_event": None})()
    orchestrator, result = _resolve_orchestrator_and_cancellation(
        state={
            "rounds": 1,
            "history": [{"role": "user", "content": "x"}],
            "cancel_event": _Event(),
        },
        config={"orchestrator": orch},
    )

    assert orchestrator is orch
    assert result == {
        "history": [{"role": "user", "content": "x"}],
        "next_action": None,
        "rounds": 2,
        "last_result": {"ok": False, "error": "Task canceled by user"},
        "errors": ["canceled"],
        "empty_response_count": 0,
    }
    assert logged == [("info", "perception_node: Task canceled by user")]


def test_resolve_orchestrator_and_cancellation_returns_orchestrator_when_clear(
    monkeypatch,
):
    _, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {})())

    class _Event:
        def is_set(self):
            return False

    orch = type("Orch", (), {"cancel_event": _Event()})()
    orchestrator, result = _resolve_orchestrator_and_cancellation(
        state={"rounds": 0, "history": []},
        config={"orchestrator": orch},
    )

    assert orchestrator is orch
    assert result is None


def test_maybe_handle_turn_limit_publishes_event_and_returns_payload(monkeypatch):
    events = []
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    class _EventBus:
        def publish(self, name, payload):
            events.append((name, payload))

        def publish_typed(self, event):
            from src.core.orchestration.event_bus import _get_event_name_for_class
            name = _get_event_name_for_class(type(event)) or type(event).__name__
            d = event.to_dict()
            d.pop("correlation_id", None)
            d.pop("timestamp", None)
            events.append((name, d))

    orchestrator = type("Orch", (), {"event_bus": _EventBus()})()

    result = _maybe_handle_turn_limit(
        state={"history": [{"role": "user", "content": "x"}], "rounds": 2},
        orchestrator=orchestrator,
        turn_count=6,
        max_turns=5,
    )

    assert result == {
        "history": [{"role": "user", "content": "x"}],
        "next_action": None,
        "rounds": 3,
        "turn_count": 6,
        "last_result": {
            "ok": False,
            "error": "Turn limit reached (5 turns). Task stopped.",
        },
        "errors": ["turn_limit_reached"],
    }
    assert events == [("task.turn_limit", {"turn_count": 6, "max_turns": 5})]
    assert logged == [
        ("warning", "perception_node: turn_count=%d >= max_turns=%d — routing to END")
    ]


def test_maybe_handle_turn_limit_returns_none_when_under_limit(monkeypatch):
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {})())

    result = _maybe_handle_turn_limit(
        state={"history": [], "rounds": 0},
        orchestrator=None,
        turn_count=5,
        max_turns=5,
    )

    assert result is None


def test_validate_call_model_and_adapter_returns_error_when_call_model_missing(
    monkeypatch,
):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    adapter, result = _validate_call_model_and_adapter(
        state={"rounds": 1},
        orchestrator=object(),
        call_model_fn=None,
    )

    assert adapter is None
    assert result == {
        "history": [],
        "next_action": None,
        "rounds": 2,
        "errors": ["call_model not available"],
    }
    assert logged == [("error", "perception_node: call_model is not callable: %s")]


def test_validate_call_model_and_adapter_returns_error_when_adapter_access_fails(
    monkeypatch,
):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    class _Orch:
        @property
        def adapter(self):
            raise RuntimeError("boom")

    adapter, result = _validate_call_model_and_adapter(
        state={"rounds": 0},
        orchestrator=_Orch(),
        call_model_fn=lambda *a, **k: None,
    )

    assert adapter is None
    assert result == {
        "history": [],
        "next_action": None,
        "rounds": 1,
        "errors": ["adapter error: boom"],
    }
    assert logged == [("error", "perception_node: failed to get adapter: %s")]


def test_validate_call_model_and_adapter_returns_error_when_adapter_none(monkeypatch):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    adapter, result = _validate_call_model_and_adapter(
        state={"rounds": 2},
        orchestrator=type("Orch", (), {"adapter": None})(),
        call_model_fn=lambda *a, **k: None,
    )

    assert adapter is None
    assert result == {
        "history": [],
        "next_action": None,
        "rounds": 3,
        "errors": ["adapter is None"],
    }
    assert logged == [("warning", "perception_node: orchestrator.adapter is None")]


def test_validate_call_model_and_adapter_returns_adapter_when_valid(monkeypatch):
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {})())
    orch = type("Orch", (), {"adapter": object()})()

    adapter, result = _validate_call_model_and_adapter(
        state={"rounds": 0},
        orchestrator=orch,
        call_model_fn=lambda *a, **k: None,
    )

    assert adapter is orch.adapter
    assert result is None


def test_filter_tools_near_turn_limit_removes_modifying_tools(monkeypatch):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    result = _filter_tools_near_turn_limit(
        tools_list=[
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "edit_file"},
        ],
        turn_count=8,
        max_turns=10,
    )

    assert result == [{"name": "read_file"}]
    assert logged == [
        (
            "info",
            "perception_node: near turn limit (%d/%d) — write tools removed from prompt",
        )
    ]


def test_filter_tools_near_turn_limit_leaves_tools_when_not_near_limit(monkeypatch):
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {})())
    tools = [{"name": "read_file"}, {"name": "write_file"}]

    result = _filter_tools_near_turn_limit(
        tools_list=tools,
        turn_count=3,
        max_turns=10,
    )

    assert result == tools


def test_compute_active_skills_for_task_injects_context_hygiene_for_debug_tasks(
    monkeypatch,
):
    logger, logged = _capture_logger()
    monkeypatch.setattr(perception_runtime, "logger", logger)

    result = _compute_active_skills_for_task(
        task="debug why search is failing",
    )

    assert result == ["context_hygiene"]
    assert logged == [
        (
            "info",
            "perception_node: injected context_hygiene skill for debugging/searching task",
        )
    ]


def test_compute_active_skills_for_task_returns_empty_for_non_debug_tasks(monkeypatch):
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {})())

    result = _compute_active_skills_for_task(
        task="summarize project status",
    )

    assert result == []


def test_select_perception_role_prefers_state_agent_mode():
    result = _select_perception_role(
        {"agent_mode": "planning"},
        type("Orch", (), {"_agent_mode": "execution"})(),
    )

    assert result == "strategic"


def test_select_perception_role_falls_back_to_orchestrator_and_default():
    assert _select_perception_role(
        {},
        type("Orch", (), {"_agent_mode": "planning"})(),
    ) == "strategic"
    assert _select_perception_role({}, object()) == "operational"


def test_resolve_perception_provider_context_returns_combined_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {"debug": lambda *a, **k: None})())

    def _resolve_caps(orchestrator, adapter):
        calls.append("caps")
        return {"provider_name": "openai", "model": "gpt-test"}

    monkeypatch.setattr(perception_runtime, "_resolve_provider_caps", _resolve_caps)
    monkeypatch.setattr(
        perception_runtime,
        "_resolve_active_model_name",
        lambda caps, orchestrator: caps.get("model", ""),
    )
    monkeypatch.setattr(perception_runtime, "_classify_model_tier", lambda model, adapter: "small")

    result = _resolve_perception_provider_context(
        orchestrator=object(),
        adapter=object(),
    )

    assert result == {
        "provider_capabilities": {"provider_name": "openai", "model": "gpt-test"},
        "active_model_name": "gpt-test",
        "provider": "openai",
        "model": "gpt-test",
        "model_tier_str": "small",
    }
    assert calls == ["caps", "caps"]


def test_resolve_perception_provider_context_handles_provider_resolution_failure(
    monkeypatch,
):
    monkeypatch.setattr(perception_runtime, "logger", type("L", (), {"debug": lambda *a, **k: None})())

    def _boom(orchestrator, adapter):
        raise RuntimeError("boom")

    monkeypatch.setattr(perception_runtime, "_resolve_provider_caps", _boom)
    monkeypatch.setattr(
        perception_runtime,
        "_resolve_active_model_name",
        lambda caps, orchestrator: "",
    )
    monkeypatch.setattr(perception_runtime, "_classify_model_tier", lambda model, adapter: None)

    result = _resolve_perception_provider_context(
        orchestrator=object(),
        adapter=object(),
    )

    assert result == {
        "provider_capabilities": {},
        "active_model_name": "",
        "provider": None,
        "model": None,
        "model_tier_str": None,
    }
