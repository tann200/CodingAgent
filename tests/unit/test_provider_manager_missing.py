from src.core.inference.llm_manager import ProviderManager
from src.core.orchestration.event_bus import EventBus


def test_provider_manager_publishes_missing(tmp_path, monkeypatch):
    # Create a temp directory with no providers.json
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    providers_path = config_dir / "providers.json"
    # Ensure file does not exist
    if providers_path.exists():
        providers_path.unlink()

    bus = EventBus()
    events = []

    def handler(payload):
        events.append(payload)

    bus.subscribe("provider.config.missing", handler)

    pm = ProviderManager(providers_config_path=str(providers_path))
    pm.set_event_bus(bus)

    # Initialize should publish missing event
    import asyncio

    asyncio.run(pm.initialize())

    assert len(events) == 1
    assert "path" in events[0]
    assert str(providers_path) in events[0]["path"]
