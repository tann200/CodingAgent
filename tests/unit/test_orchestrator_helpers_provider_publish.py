from types import SimpleNamespace


from src.core.orchestration.orchestrator_helpers import _publish_active_config_impl


class DummyBus:
    def __init__(self):
        self.events = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))


class DummyOrch(SimpleNamespace):
    pass


def test_publish_with_orchestrator_caps():
    orch = DummyOrch()
    orch.event_bus = DummyBus()

    def get_caps():
        return {"provider_name": "OpenAI", "model": "gpt-4"}

    orch.get_provider_capabilities = get_caps
    orch._adapter = None

    _publish_active_config_impl(orch)
    assert orch.event_bus.events, "No events published"
    topic, payload = orch.event_bus.events[-1]
    assert topic == "model.routing"
    assert payload["provider"] in ("OpenAI", "openai")
    assert payload["selected"] == "gpt-4"


def test_publish_filters_magicmock_in_available_models():
    orch = DummyOrch()
    orch.event_bus = DummyBus()

    class Adapter(SimpleNamespace):
        pass

    adapter = Adapter(models=["gpt-4", "MagicMock name='mm'", "gemma-4"])
    orch._adapter = adapter

    # no orchestrator caps; rely on adapter inspection
    _publish_active_config_impl(orch)
    topic, payload = orch.event_bus.events[-1]
    assert topic == "model.routing"
    assert "MagicMock" not in ",".join(payload.get("available_models", []))
