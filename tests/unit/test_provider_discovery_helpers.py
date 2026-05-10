from src.core.inference.provider_discovery import (
    get_active_models,
    get_models_for_provider_key,
    get_models_from_provider_adapter,
    get_models_from_provider_cache,
    get_models_from_provider_config,
)


class _Manager:
    def __init__(self):
        self.providers_config_path = None

    def get_cached_models(self, key):
        if key == "lm_studio":
            return ["qwen3.5-9b"]
        return []

    def get_provider(self, key):
        if key != "lm_studio":
            return None

        class _Adapter:
            def get_models_from_api(self):
                return {"models": [{"id": "qwen/qwen3.5-9b"}, "other/model"]}

        return _Adapter()


class _ActiveModelsManager:
    def __init__(self, *, active="ollama", cached=None, adapter=None):
        self._active = active
        self._cached = cached or []
        self._adapter = adapter

    def get_active_provider_name(self):
        return self._active

    def get_cached_models(self, key):
        return list(self._cached) if key == self._active else []

    def get_provider(self, key):
        if key == self._active:
            return self._adapter
        return None


def test_get_models_from_provider_cache_normalizes_lmstudio():
    result = get_models_from_provider_cache(
        manager=_Manager(),
        provider_key="lm_studio",
        normalize_lmstudio_models=lambda models: [f"full/{model}" for model in models],
    )

    assert result == ["full/qwen3.5-9b"]


def test_get_models_from_provider_adapter_extracts_models():
    result = get_models_from_provider_adapter(
        manager=_Manager(),
        provider_key="lm_studio",
        extract_models_from_api_response=lambda response: [
            item["id"] if isinstance(item, dict) else item for item in response["models"]
        ],
        normalize_lmstudio_models=lambda models: models,
    )

    assert result == ["qwen/qwen3.5-9b", "other/model"]


def test_get_models_from_provider_config_reads_matching_provider():
    class _ConfigManager:
        providers_config_path = "/tmp/providers.json"

    result = get_models_from_provider_config(
        manager=_ConfigManager(),
        provider_key="openai",
        load_provider=lambda _path: [
            {"type": "openai", "models": ["gpt-4o"]},
            {"type": "anthropic", "models": ["claude"]},
        ],
        normalize_models_for_provider=lambda provider: list(provider.get("models", [])),
    )

    assert result == ["gpt-4o"]


def test_get_active_models_prefers_cached_models():
    manager = _ActiveModelsManager(cached=["cached-model"], adapter=object())

    assert get_active_models(manager=manager) == ["cached-model"]


def test_get_active_models_falls_back_to_adapter_models_then_default_model():
    class _ModelsAdapter:
        models = ("adapter-a", "adapter-b")

    class _DefaultAdapter:
        default_model = "fallback-model"

    assert get_active_models(manager=_ActiveModelsManager(adapter=_ModelsAdapter())) == [
        "adapter-a",
        "adapter-b",
    ]
    assert get_active_models(manager=_ActiveModelsManager(adapter=_DefaultAdapter())) == [
        "fallback-model"
    ]


def test_get_active_models_returns_empty_without_active_provider_or_adapter():
    assert get_active_models(manager=_ActiveModelsManager(active=None)) == []
    assert get_active_models(manager=_ActiveModelsManager(adapter=None)) == []


class _CacheLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_get_models_for_provider_key_prefers_fresh_module_cache():
    result = get_models_for_provider_key(
        provider_key="ollama",
        manager=object(),
        cache={"ollama": ["cached-model"]},
        cache_time={"ollama": 10.0},
        cache_lock=_CacheLock(),
        cache_ttl=300,
        now=lambda: 20.0,
        get_cached_models_if_fresh=lambda **kwargs: kwargs["cache"]["ollama"],
        store_cached_models=lambda **kwargs: None,
        get_models_from_provider_cache_fn=lambda **kwargs: ["provider-cache"],
        get_models_from_provider_adapter_fn=lambda **kwargs: ["adapter-model"],
        get_models_from_provider_config_fn=lambda **kwargs: ["config-model"],
        extract_models_from_api_response=lambda response, valid_str: [],
        normalize_lmstudio_models=lambda models: models,
        load_provider=lambda path: None,
        normalize_models_for_provider=lambda provider: [],
        valid_str=lambda value: bool(value),
    )

    assert result == ["cached-model"]


def test_get_models_for_provider_key_caches_adapter_probe_results():
    cache = {}
    cache_time = {}

    def _store_cached(**kwargs):
        cache[kwargs["provider_key"]] = list(kwargs["models"])
        cache_time[kwargs["provider_key"]] = 123.0

    result = get_models_for_provider_key(
        provider_key="lm_studio",
        manager=object(),
        cache=cache,
        cache_time=cache_time,
        cache_lock=_CacheLock(),
        cache_ttl=300,
        now=lambda: 20.0,
        get_cached_models_if_fresh=lambda **kwargs: None,
        store_cached_models=_store_cached,
        get_models_from_provider_cache_fn=lambda **kwargs: None,
        get_models_from_provider_adapter_fn=lambda **kwargs: ["qwen/qwen3.5-9b"],
        get_models_from_provider_config_fn=lambda **kwargs: ["config-model"],
        extract_models_from_api_response=lambda response, valid_str: [],
        normalize_lmstudio_models=lambda models: models,
        load_provider=lambda path: None,
        normalize_models_for_provider=lambda provider: [],
        valid_str=lambda value: bool(value),
    )

    assert result == ["qwen/qwen3.5-9b"]
    assert cache == {"lm_studio": ["qwen/qwen3.5-9b"]}
    assert cache_time == {"lm_studio": 123.0}


def test_get_models_for_provider_key_falls_back_to_provider_config():
    result = get_models_for_provider_key(
        provider_key="openai",
        manager=object(),
        cache={},
        cache_time={},
        cache_lock=_CacheLock(),
        cache_ttl=300,
        now=lambda: 20.0,
        get_cached_models_if_fresh=lambda **kwargs: None,
        store_cached_models=lambda **kwargs: None,
        get_models_from_provider_cache_fn=lambda **kwargs: None,
        get_models_from_provider_adapter_fn=lambda **kwargs: None,
        get_models_from_provider_config_fn=lambda **kwargs: ["gpt-4o"],
        extract_models_from_api_response=lambda response, valid_str: [],
        normalize_lmstudio_models=lambda models: models,
        load_provider=lambda path: None,
        normalize_models_for_provider=lambda provider: [],
        valid_str=lambda value: bool(value),
    )

    assert result == ["gpt-4o"]
