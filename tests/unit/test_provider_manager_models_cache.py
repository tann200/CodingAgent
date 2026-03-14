import importlib
import asyncio
import src.core.llm_manager as lm


def test_provider_manager_probes_models_once(monkeypatch, tmp_path):
    """Ensure ProviderManager.initialize calls adapter.get_models_from_api once and caches models."""
    # Prepare a fake providers.json in tmp_path
    providers = [
        {
            "name": "lm_studio",
            "type": "lm_studio",
            "base_url": "http://localhost:1234/v1",
            "models": ["qwen/qwen3.5-9b"]
        }
    ]
    cfg = tmp_path / "providers.json"
    cfg.write_text(importlib.import_module('json').dumps(providers))

    # Monkeypatch LMStudioAdapter.get_models_from_api to count calls
    importlib.reload(importlib.import_module('src.adapters.lm_studio_adapter'))
    from src.adapters.lm_studio_adapter import LmStudioAdapter as _LM
    calls = {"n": 0}

    def _fake_get_models(self):
        calls['n'] += 1
        return {"models": [{"id": "qwen/qwen3.5-9b", "display_name": "qwen3.5-9b"}]}

    monkeypatch.setattr(_LM, 'get_models_from_api', _fake_get_models)

    # Reset provider manager singleton
    lm._provider_manager._initialized = False
    lm._provider_manager._providers = {}
    lm._provider_manager._models_cache = {}

    # Initialize with custom providers path
    lm._provider_manager.providers_config_path = str(cfg)
    # Run initialize (sync shim)
    lm._ensure_provider_manager_initialized_sync()

    # Assert LMStudioAdapter.get_models_from_api was called exactly once
    assert calls['n'] == 1

    # Cached models should include the model id
    cached = lm._provider_manager.get_cached_models('lm_studio')
    assert 'qwen/qwen3.5-9b' in cached

    # Calling get_available_models should return the cached models and not call adapter again
    models_first = asyncio.run(lm.get_available_models('', '', 'lm_studio'))
    models_second = asyncio.run(lm.get_available_models('', '', 'lm_studio'))
    assert models_first == models_second
    assert 'qwen/qwen3.5-9b' in models_first

