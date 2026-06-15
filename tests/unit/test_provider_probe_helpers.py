from src.core.inference.provider_probe import (
    cache_probed_models,
    determine_explicit_status,
    probe_adapter_models,
    publish_provider_probe_events,
    publish_unknown_provider_status,
    run_provider_probe_cycle,
    should_probe_provider,
)


class _EventBus:
    def __init__(self):
        self.events = []

    def publish(self, event_name, payload):
        self.events.append((event_name, payload))

    def publish_typed(self, event):
        from src.core.orchestration.event_bus import _get_event_name_for_class
        name = _get_event_name_for_class(type(event)) or type(event).__name__
        d = event.to_dict()
        d.pop("correlation_id", None)
        d.pop("timestamp", None)
        self.publish(name, d)


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_should_probe_provider_skips_inactive_non_local():
    assert (
        should_probe_provider(
            provider_config={"type": "openai", "active": False},
            canonical_provider=lambda value: str(value),
        )
        is False
    )


def test_should_probe_provider_keeps_inactive_local_provider():
    assert (
        should_probe_provider(
            provider_config={"type": "lm_studio", "active": False},
            canonical_provider=lambda value: str(value),
        )
        is True
    )


def test_determine_explicit_status_ignores_awaitable_validation():
    class _Adapter:
        async def validate_connection(self):
            return True

    assert determine_explicit_status(_Adapter()) is None


def test_determine_explicit_status_returns_connected_state():
    class _Adapter:
        def validate_connection(self):
            return True

    assert determine_explicit_status(_Adapter()) == "connected"


def test_probe_adapter_models_normalizes_lm_studio_models():
    class _Adapter:
        def get_models_from_api(self):
            return {"models": ["qwen3.5:9b"]}

    models = probe_adapter_models(
        adapter=_Adapter(),
        provider_key="lm_studio",
        extract_models_from_api_response=lambda response: list(
            response.get("models", [])
        ),
        normalize_lmstudio_models=lambda items: [f"full/{item}" for item in items],
    )

    assert models == ["full/qwen3.5:9b"]


def test_cache_probed_models_updates_all_caches():
    provider_cache = {}
    module_cache = {}
    module_cache_time = {}

    cache_probed_models(
        provider_key="openai",
        models=["gpt-4o"],
        provider_models_cache=provider_cache,
        module_models_cache=module_cache,
        module_models_cache_time=module_cache_time,
        model_cache_lock=_Lock(),
        now=lambda: 321.0,
    )

    assert provider_cache["openai"] == ["gpt-4o"]
    assert module_cache["openai"] == ["gpt-4o"]
    assert module_cache_time["openai"] == 321.0


def test_publish_provider_probe_events_emits_connected_events_for_models():
    bus = _EventBus()

    publish_provider_probe_events(
        event_bus=bus,
        provider_key="openai",
        models=["gpt-4o"],
        explicit_status=None,
    )

    assert bus.events == [
        ("provider.models.list", {"provider": "openai", "models": ["gpt-4o"]}),
        ("provider.models.cached", {"provider": "openai", "models": ["gpt-4o"]}),
        ("provider.status.changed", {"provider": "openai", "status": "connected"}),
    ]


def test_publish_provider_probe_events_emits_empty_status_override():
    bus = _EventBus()

    publish_provider_probe_events(
        event_bus=bus,
        provider_key="github_copilot",
        models=[],
        explicit_status="connected",
    )

    assert bus.events == [
        ("provider.models.empty", {"provider": "github_copilot"}),
        (
            "provider.status.changed",
            {"provider": "github_copilot", "status": "connected"},
        ),
    ]


def test_publish_unknown_provider_status_defaults_unknown():
    bus = _EventBus()

    publish_unknown_provider_status(
        event_bus=bus,
        provider_key="custom",
        explicit_status=None,
    )

    assert bus.events == [
        ("provider.status.changed", {"provider": "custom", "status": "unknown"})
    ]


def test_run_provider_probe_cycle_publishes_disconnect_for_missing_adapter():
    bus = _EventBus()
    provider_cache = {}

    run_provider_probe_cycle(
        providers=[{"type": "openai"}],
        providers_map={"openai": None},
        provider_models_cache=provider_cache,
        module_models_cache={},
        module_models_cache_time={},
        model_cache_lock=_Lock(),
        now=lambda: 1.0,
        event_bus=bus,
        canonical_provider=lambda value: str(value).lower().replace(" ", "_"),
        should_probe_provider_fn=lambda **kwargs: True,
        determine_explicit_status_fn=lambda adapter: None,
        probe_adapter_models_fn=lambda **kwargs: [],
        cache_probed_models_fn=lambda **kwargs: None,
        publish_provider_probe_events_fn=lambda **kwargs: None,
        publish_unknown_provider_status_fn=lambda **kwargs: None,
        logger=type("Logger", (), {"info": lambda *args, **kwargs: None})(),
        get_loaded_context_length_fn=lambda adapter, active_model: None,
        set_active_context_length_fn=lambda context_length: None,
        is_active_provider_fn=lambda provider_key, provider_config: False,
    )

    assert provider_cache == {"openai": []}
    assert bus.events == [
        ("provider.status.changed", {"provider": "openai", "status": "disconnected"})
    ]


def test_run_provider_probe_cycle_caches_models_and_publishes_context_window():
    bus = _EventBus()
    provider_cache = {}
    module_cache = {}
    module_cache_time = {}
    seen_context = []

    class _Adapter:
        def get_models_from_api(self):
            return {"models": [{"id": "model-a"}]}

        def get_loaded_context_length(self, active_model):
            return 8192

    run_provider_probe_cycle(
        providers=[{"type": "lm_studio"}],
        providers_map={"lm_studio": _Adapter()},
        provider_models_cache=provider_cache,
        module_models_cache=module_cache,
        module_models_cache_time=module_cache_time,
        model_cache_lock=_Lock(),
        now=lambda: 2.0,
        event_bus=bus,
        canonical_provider=lambda value: "lm_studio",
        should_probe_provider_fn=lambda **kwargs: True,
        determine_explicit_status_fn=lambda adapter: "connected",
        probe_adapter_models_fn=lambda **kwargs: ["model-a"],
        cache_probed_models_fn=cache_probed_models,
        publish_provider_probe_events_fn=publish_provider_probe_events,
        publish_unknown_provider_status_fn=publish_unknown_provider_status,
        logger=type("Logger", (), {"info": lambda *args, **kwargs: None})(),
        get_loaded_context_length_fn=lambda adapter, active_model: adapter.get_loaded_context_length(
            active_model
        ),
        set_active_context_length_fn=lambda context_length, provider_key: seen_context.append(
            context_length
        ),
        is_active_provider_fn=lambda provider_key, provider_config: True,
    )

    assert provider_cache == {"lm_studio": ["model-a"]}
    assert module_cache == {"lm_studio": ["model-a"]}
    assert module_cache_time == {"lm_studio": 2.0}
    assert seen_context == [8192]
    assert (
        "provider.context_window",
        {"provider": "lm_studio", "model": "model-a", "context_window": 8192},
    ) in bus.events


def test_run_provider_probe_cycle_does_not_overwrite_live_context_for_inactive_provider():
    bus = _EventBus()
    seen_context = []

    class _Adapter:
        def get_models_from_api(self):
            return {"models": [{"id": "model-b"}]}

        def get_loaded_context_length(self, active_model):
            return 16384

    run_provider_probe_cycle(
        providers=[{"type": "lm_studio", "active": False}],
        providers_map={"lm_studio": _Adapter()},
        provider_models_cache={},
        module_models_cache={},
        module_models_cache_time={},
        model_cache_lock=_Lock(),
        now=lambda: 3.0,
        event_bus=bus,
        canonical_provider=lambda value: "lm_studio",
        should_probe_provider_fn=lambda **kwargs: True,
        determine_explicit_status_fn=lambda adapter: "connected",
        probe_adapter_models_fn=lambda **kwargs: ["model-b"],
        cache_probed_models_fn=lambda **kwargs: None,
        publish_provider_probe_events_fn=publish_provider_probe_events,
        publish_unknown_provider_status_fn=publish_unknown_provider_status,
        logger=type("Logger", (), {"info": lambda *args, **kwargs: None})(),
        get_loaded_context_length_fn=lambda adapter, active_model: adapter.get_loaded_context_length(
            active_model
        ),
        set_active_context_length_fn=lambda context_length: seen_context.append(
            context_length
        ),
        is_active_provider_fn=lambda provider_key, provider_config: bool(
            provider_config and provider_config.get("active")
        ),
    )

    assert seen_context == []
    assert not any(
        event[0] == "provider.context_window" for event in bus.events
    )
