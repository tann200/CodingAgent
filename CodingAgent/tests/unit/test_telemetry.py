"""tests/unit/test_telemetry.py

Telemetry event bus tests.
  - test_message_truncation_emits_event: MessageManager truncation event
  - test_model_routing_emits_event: S10-D refactored with MockAdapter (no live LLM)
  - test_telemetry_decorator_publishes_to_event_bus: with_telemetry decorator
"""

from __future__ import annotations


from src.core.inference.adapters.mock_adapter import MockAdapter
from src.core.orchestration.event_bus import EventBus, get_event_bus
from src.core.orchestration.message_manager import MessageManager
from src.core.orchestration.orchestrator import Orchestrator

# Every graph node that calls call_model at module-load time needs patching.
_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.planning_node.call_model",
    "src.core.orchestration.graph.nodes.execution_node.call_model",
    "src.core.orchestration.graph.nodes.debug_node.call_model",
    "src.core.orchestration.graph.nodes.replan_node.call_model",
    "src.core.inference.llm_manager.call_model",
    "src.core.inference.llm_manager._call_model_internal",
]


def test_message_truncation_emits_event(tmp_path):
    bus = EventBus()
    received = {}

    def on_trunc(payload):
        received["payload"] = payload

    bus.subscribe("message.truncation", on_trunc)

    # Use a very small token window to force truncation quickly
    mm = MessageManager(max_tokens=5, event_bus=bus)
    mm.append("system", "system init")
    # append many long token-like messages to force truncation
    for i in range(6):
        mm.append("user", ("word " * 100))

    # ensure event was received
    assert "payload" in received
    p = received["payload"]
    assert "dropped_count" in p and "dropped_tokens" in p and "tokens_after" in p
    assert isinstance(p["dropped_count"], int)


def test_model_routing_emits_event(monkeypatch, tmp_path):
    """S10-D: Verify model.routing event is published — no live LLM required."""
    bus = EventBus()
    captured = {}

    def on_route(payload):
        captured["payload"] = payload

    bus.subscribe("model.routing", on_route)

    adapter = MockAdapter(responses=["OK"])

    async def mock_call_model(messages, model=None, provider=None, *args, **kwargs):
        return {
            "ok": True,
            "provider": "mock",
            "model": "mock-model",
            "prompt_tokens": 5,
            "completion_tokens": 5,
            "total_tokens": 10,
            "content": "OK",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
        }

    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass

    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )

    orch = Orchestrator(
        adapter=adapter,
        working_dir=str(tmp_path),
        allow_external_working_dir=True,
        message_max_tokens=1000,
    )
    orch.event_bus = bus
    _ = orch.run_agent_once(
        None, [{"role": "user", "content": "Hello, select model"}], {}
    )

    assert "payload" in captured
    p = captured["payload"]
    assert p.get("provider") in (None, "mock")
    assert "available_models" in p and isinstance(p["available_models"], list)
    assert "selected" in p and p["selected"] in adapter.models


def test_telemetry_decorator_publishes_to_event_bus(monkeypatch):
    """Test that with_telemetry decorator publishes model.response to get_event_bus."""
    from src.core.inference.telemetry import with_telemetry
    from src.core.inference.llm_client import LLMClient

    captured_events = []

    def capture_event(payload):
        captured_events.append(payload)

    bus = get_event_bus()
    bus.subscribe("model.response", capture_event)

    class TestClient(LLMClient):
        def __init__(self):
            self.name = "test_provider"

        @with_telemetry
        def generate(self, messages, model=None, **kwargs):
            return {
                "ok": True,
                "provider": "test_provider",
                "model": model or "test-model",
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "choices": [{"message": {"role": "assistant", "content": "test"}}],
            }

    client = TestClient()
    client.generate([{"role": "user", "content": "test"}])

    bus.unsubscribe("model.response", capture_event)

    assert len(captured_events) > 0, f"Expected events but got {captured_events}"
    payload = captured_events[0]
    assert payload["prompt_tokens"] == 10
    assert payload["completion_tokens"] == 20
    assert payload["total_tokens"] == 30
    assert payload["provider"] == "test_provider"
