from types import SimpleNamespace


def test_publish_active_config_uses_get_provider_capabilities(monkeypatch):
    # Create a fake orchestrator with get_provider_capabilities
    published = {}

    def fake_publish(topic, payload):
        published["topic"] = topic
        published["payload"] = payload

    caps = {"provider_name": "fakeprov", "model": "m1"}

    orch = SimpleNamespace()
    orch.get_provider_capabilities = lambda: caps
    orch.event_bus = SimpleNamespace(publish=fake_publish)

    # Import and call the helper
    from src.core.orchestration.orchestrator_helpers import _publish_active_config_impl

    _publish_active_config_impl(orch)
    assert published.get("topic") == "model.routing"
    assert published.get("payload", {}).get("provider") == "fakeprov"
    assert published.get("payload", {}).get("selected") == "m1"


def test_publish_active_config_falls_back_to_adapter(monkeypatch):
    published = {}

    def fake_publish(topic, payload):
        published["topic"] = topic
        published["payload"] = payload

    adapter = SimpleNamespace(
        provider={"name": "oldprov", "type": "openai"}, default_model="oldm"
    )
    orch = SimpleNamespace()
    orch.get_provider_capabilities = lambda: (_ for _ in ()).throw(Exception("boom"))
    orch._adapter = adapter
    orch.event_bus = SimpleNamespace(publish=fake_publish)

    from src.core.orchestration.orchestrator_helpers import _publish_active_config_impl

    _publish_active_config_impl(orch)
    assert published.get("payload", {}).get("provider") == "oldprov"
    assert published.get("payload", {}).get("selected") == "oldm"


def test_publish_active_config_uses_provider_manager(monkeypatch):
    # Ensure ProviderManager is consulted when orchestrator-level caps are absent
    published = {}

    def fake_publish(topic, payload):
        published["topic"] = topic
        published["payload"] = payload

    adapter = SimpleNamespace(provider={"name": "irrelevant"})
    orch = SimpleNamespace()
    orch._adapter = adapter
    orch.event_bus = SimpleNamespace(publish=fake_publish)

    caps = {"provider_name": "pmprov", "model": "pm_model"}

    class FakePM:
        def get_provider_capabilities(self, a):
            return caps

    import src.core.inference.llm_manager as llm_manager

    monkeypatch.setattr(llm_manager, "get_provider_manager", lambda: FakePM())

    from src.core.orchestration.orchestrator_helpers import _publish_active_config_impl

    _publish_active_config_impl(orch)
    assert published.get("topic") == "model.routing"
    assert published.get("payload", {}).get("provider") == "pmprov"
    assert published.get("payload", {}).get("selected") == "pm_model"


def test_publish_active_config_provider_manager_filters_magicmock(monkeypatch):
    # ProviderManager returns MagicMock-like values -> should be ignored
    published = {}

    def fake_publish(topic, payload):
        published["topic"] = topic
        published["payload"] = payload

    adapter = SimpleNamespace(
        provider={"name": "oldprov", "type": "openai"},
        default_model="oldm",
        models=["good", "MagicMock name='x'"],
    )
    orch = SimpleNamespace()
    orch._adapter = adapter
    orch.event_bus = SimpleNamespace(publish=fake_publish)

    caps = {"provider_name": "MagicMock name='pm'", "model": "MagicMock name='m'"}

    class FakePM:
        def get_provider_capabilities(self, a):
            return caps

    import src.core.inference.llm_manager as llm_manager

    monkeypatch.setattr(llm_manager, "get_provider_manager", lambda: FakePM())

    from src.core.orchestration.orchestrator_helpers import _publish_active_config_impl

    _publish_active_config_impl(orch)
    # Should have fallen back to adapter values
    assert published.get("payload", {}).get("provider") == "oldprov"
    assert published.get("payload", {}).get("selected") == "oldm"
    # available_models should filter out MagicMock entries
    assert published.get("payload", {}).get("available_models") == ["good"]
