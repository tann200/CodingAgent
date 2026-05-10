from src.core.inference.provider_loading import (
    attach_provider_metadata,
    cache_static_provider_models,
    instantiate_adapter,
    load_registered_providers,
    load_provider_entries,
    resolve_adapter_class,
)


def test_load_provider_entries_filters_non_dict_values():
    result = load_provider_entries(
        [
            {"type": "openai"},
            "bad",
            1,
            {"type": "anthropic"},
        ]
    )

    assert result == [{"type": "openai"}, {"type": "anthropic"}]


def test_instantiate_adapter_prefers_named_args():
    class _Adapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    adapter, error = instantiate_adapter(
        adapter_cls=_Adapter,
        provider={
            "name": "openai",
            "base_url": "https://api.example.com",
            "models": ["gpt-4o"],
        },
        providers_config_path="/tmp/providers.json",
        normalize_models_for_provider=lambda provider: list(provider.get("models", [])),
    )

    assert error is None
    assert adapter.kwargs["name"] == "openai"
    assert adapter.kwargs["models"] == ["gpt-4o"]


def test_attach_provider_metadata_sets_expected_attributes():
    class _Adapter:
        pass

    adapter = _Adapter()
    attach_provider_metadata(adapter, {"type": "openai"})

    assert adapter.provider == {"type": "openai"}
    assert adapter.missing_provider is False


def test_cache_static_provider_models_updates_caches():
    provider_cache = {}
    module_cache = {}
    module_cache_time = {}

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    cache_static_provider_models(
        provider_key="openai",
        provider={"models": ["gpt-4o"]},
        normalize_models_for_provider=lambda provider: list(provider.get("models", [])),
        provider_models_cache=provider_cache,
        module_models_cache=module_cache,
        module_models_cache_time=module_cache_time,
        model_cache_lock=_Lock(),
        now=lambda: 123.0,
        event_bus=None,
    )

    assert provider_cache["openai"] == ["gpt-4o"]
    assert module_cache["openai"] == ["gpt-4o"]
    assert module_cache_time["openai"] == 123.0


def test_resolve_adapter_class_returns_error_for_missing_module():
    adapter_cls, error = resolve_adapter_class(
        provider_type="definitely_missing_provider",
        camelize=lambda text: text.title(),
    )

    assert adapter_cls is None
    assert error is not None


def test_load_registered_providers_registers_adapter_and_static_models():
    providers_map = {}
    provider_cache = {}
    module_cache = {}
    module_cache_time = {}
    attached = []

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Logger:
        def error(self, *_args, **_kwargs):
            return None

    load_registered_providers(
        providers=[{"name": "LM Studio", "type": "lm_studio", "models": ["qwen"]}],
        providers_map=providers_map,
        provider_models_cache=provider_cache,
        module_models_cache=module_cache,
        module_models_cache_time=module_cache_time,
        model_cache_lock=_Lock(),
        now=lambda: 123.0,
        event_bus=None,
        providers_config_path="/tmp/providers.json",
        canonical_provider=lambda name: "lm_studio",
        resolve_adapter_class_fn=lambda **kwargs: (object, None),
        instantiate_adapter_fn=lambda **kwargs: (type("Adapter", (), {})(), None),
        attach_provider_metadata_fn=lambda adapter, provider: attached.append(
            (adapter, provider)
        ),
        cache_static_provider_models_fn=cache_static_provider_models,
        normalize_models_for_provider=lambda provider: list(provider.get("models", [])),
        camelize=lambda value: value,
        logger=_Logger(),
    )

    assert "lm_studio" in providers_map
    assert attached and attached[0][1]["type"] == "lm_studio"
    assert provider_cache == {"lm_studio": ["qwen"]}
    assert module_cache == {"lm_studio": ["qwen"]}
    assert module_cache_time == {"lm_studio": 123.0}


def test_load_registered_providers_records_missing_adapter_as_none():
    providers_map = {}

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Logger:
        def __init__(self):
            self.errors = []

        def error(self, message):
            self.errors.append(message)

    logger = _Logger()

    load_registered_providers(
        providers=[{"type": "missing"}],
        providers_map=providers_map,
        provider_models_cache={},
        module_models_cache={},
        module_models_cache_time={},
        model_cache_lock=_Lock(),
        now=lambda: 0.0,
        event_bus=None,
        providers_config_path=None,
        canonical_provider=lambda name: "missing",
        resolve_adapter_class_fn=lambda **kwargs: (None, "boom"),
        instantiate_adapter_fn=lambda **kwargs: (None, None),
        attach_provider_metadata_fn=lambda adapter, provider: None,
        cache_static_provider_models_fn=lambda **kwargs: None,
        normalize_models_for_provider=lambda provider: [],
        camelize=lambda value: value,
        logger=logger,
    )

    assert providers_map == {"missing": None}
    assert logger.errors == ["boom"]
