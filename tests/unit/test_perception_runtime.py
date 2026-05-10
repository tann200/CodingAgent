from types import SimpleNamespace


def test_resolve_active_model_name_prefers_provider_capabilities_model():
    from src.core.orchestration.graph.nodes.perception_node import (
        _resolve_active_model_name,
    )

    orchestrator = SimpleNamespace(
        adapter=SimpleNamespace(models=["fallback-model"], default_model="default-model")
    )

    result = _resolve_active_model_name(
        {"model": "provider-model"},
        orchestrator,
    )

    assert result == "provider-model"


def test_resolve_active_model_name_falls_back_to_adapter_models_then_default():
    from src.core.orchestration.graph.nodes.perception_node import (
        _resolve_active_model_name,
    )

    orchestrator = SimpleNamespace(
        adapter=SimpleNamespace(models=[None, "model-a", "model-b"], default_model="default-model")
    )
    assert _resolve_active_model_name({}, orchestrator) == "model-a"

    orchestrator = SimpleNamespace(
        adapter=SimpleNamespace(models=None, default_model="default-model")
    )
    assert _resolve_active_model_name({}, orchestrator) == "default-model"


def test_build_llm_kwargs_deterministic_includes_seed():
    from src.core.orchestration.graph.nodes.perception_node import _build_llm_kwargs

    orchestrator = SimpleNamespace(
        deterministic=True,
        seed=7,
        get_provider_capabilities=lambda: {},
    )

    result = _build_llm_kwargs(orchestrator)

    assert result["temperature"] == 0.0
    assert result["seed"] == 7


def test_build_llm_kwargs_reasoning_no_think_model_disables_thinking():
    from src.core.orchestration.graph.nodes.perception_node import _build_llm_kwargs

    orchestrator = SimpleNamespace(
        deterministic=False,
        seed=None,
        get_provider_capabilities=lambda: {"model": "qwen/qwen3.5-9b"},
    )

    result = _build_llm_kwargs(orchestrator)

    assert result["temperature"] == 0.4
    assert result["think"] is False


def test_maybe_warn_small_context_window_publishes_warning_event():
    from src.core.orchestration.graph.nodes.perception_node import (
        _maybe_warn_small_context_window,
    )

    events = []

    class EventBus:
        def publish(self, event_name, payload):
            events.append((event_name, payload))

    orchestrator = SimpleNamespace(event_bus=EventBus())
    adapter = SimpleNamespace(context_window=7168)

    _maybe_warn_small_context_window(
        state={"rounds": 0},
        orchestrator=orchestrator,
        adapter=adapter,
        model="qwen/qwen3.5-9b",
        model_tier_str="small",
    )

    assert len(events) == 1
    event_name, payload = events[0]
    assert event_name == "ui.notification"
    assert payload["level"] == "warning"
    assert payload["source"] == "context_window_check"
    assert "7,168" in payload["message"]


def test_maybe_warn_small_context_window_skips_non_round_zero_or_large_window():
    from src.core.orchestration.graph.nodes.perception_node import (
        _maybe_warn_small_context_window,
    )

    events = []

    class EventBus:
        def publish(self, event_name, payload):
            events.append((event_name, payload))

    orchestrator = SimpleNamespace(event_bus=EventBus())

    _maybe_warn_small_context_window(
        state={"rounds": 1},
        orchestrator=orchestrator,
        adapter=SimpleNamespace(context_window=7168),
        model="qwen/qwen3.5-9b",
        model_tier_str="small",
    )
    _maybe_warn_small_context_window(
        state={"rounds": 0},
        orchestrator=orchestrator,
        adapter=SimpleNamespace(context_window=32768),
        model="qwen/qwen3.5-9b",
        model_tier_str="small",
    )
    _maybe_warn_small_context_window(
        state={"rounds": 0},
        orchestrator=orchestrator,
        adapter=SimpleNamespace(context_window=7168),
        model="qwen/qwen3.5-9b",
        model_tier_str="medium",
    )

    assert events == []
