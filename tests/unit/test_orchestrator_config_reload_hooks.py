class _DummyEventBus:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None):
        self.published.append((topic, payload))

    def subscribe(self, topic, fn):
        pass


def test_orchestrator_registers_config_reload_callback(monkeypatch):
    """Ensure bootstrap_orchestrator registers a config reload callback that
    refreshes AgentBrainManager, toolset cache, tool registry, and provider init.
    """
    from src.core.orchestration.orchestrator_bootstrap import bootstrap_orchestrator

    # Create a minimal orchestrator-like object
    class Orch:
        pass

    orch = Orch()
    orch.event_bus = _DummyEventBus()
    orch.working_dir = None
    orch.tool_registry = object()
    # Provide minimal attributes expected by bootstrap_orchestrator
    orch._compact_messages = lambda *a, **k: None
    orch._ensure_working_dir = lambda: None
    orch._allow_external = False
    orch._current_task_id = None
    orch._current_task = None

    # Monkeypatch AgentBrainManager.reload
    called = {
        "agent_reload": False,
        "clear_cache": False,
        "registry_built": False,
        "provider_init": False,
    }

    class FakeAgentBrain:
        def reload(self):
            called["agent_reload"] = True

    monkeypatch.setattr(
        "src.core.orchestration.agent_brain.get_agent_brain_manager",
        lambda: FakeAgentBrain(),
    )

    # Monkeypatch toolsets.loader.clear_cache
    def fake_clear():
        called["clear_cache"] = True

    monkeypatch.setattr("src.config.toolsets.loader.clear_cache", fake_clear)

    # Monkeypatch registry_builder.example_registry
    class FakeReg:
        pass

    def fake_example_registry():
        called["registry_built"] = True
        return FakeReg()

    monkeypatch.setattr(
        "src.core.orchestration.registry_builder.example_registry",
        fake_example_registry,
    )

    # Monkeypatch provider manager functions
    class FakePM:
        def set_event_bus(self, bus):
            pass

    monkeypatch.setattr(
        "src.core.inference.llm_manager.get_provider_manager", lambda: FakePM()
    )
    monkeypatch.setattr(
        "src.core.inference.llm_manager._ensure_provider_manager_initialized_sync",
        lambda: called.update({"provider_init": True}),
    )

    # Register handlers directly (we avoid running the full bootstrap to keep
    # the test lightweight and focused on the reload handler registration).
    from src.core.orchestration.orchestrator_bootstrap import (
        register_config_reload_handlers,
    )

    register_config_reload_handlers(orch)

    # Trigger reloader callbacks directly
    from src.core.config_hot_reload import get_config_reloader

    loader = get_config_reloader(initial_load=False)
    # Simulate change set
    loader._invoke_callbacks({"/fake/path"})

    assert called["agent_reload"]
    assert called["clear_cache"]
    assert called["registry_built"]
    assert called["provider_init"]
