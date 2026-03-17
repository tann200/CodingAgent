import json
import asyncio

from src.core.llm_manager import ProviderManager


def test_provider_manager_loads_adapter_from_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    providers_path = cfg_dir / 'providers.json'
    providers = [
        {
            "name": "lm_studio",
            "type": "lm_studio",
            "base_url": "http://localhost:1234",
            "models": [{"name": "qwen3.5:9b"}],
        }
    ]
    providers_path.write_text(json.dumps(providers), encoding='utf-8')

    pm = ProviderManager(providers_config_path=str(providers_path))
    # initialize should register provider
    asyncio.run(pm.initialize())
    keys = pm.list_providers()
    assert 'lm_studio' in keys
    adapter = pm.get_provider('lm_studio')
    assert adapter is not None
    # Adapter should have missing_provider attribute (we set it in adapters)
    assert hasattr(adapter, 'missing_provider')
    # because provider config exists, missing_provider should be False
    assert adapter.missing_provider is False
    # Adapter models should be present or default_model should be set
    if hasattr(adapter, 'models') and adapter.models:
        assert len(adapter.models) >= 1
    if hasattr(adapter, 'default_model'):
        assert adapter.default_model is not None

    # Deterministic check: ProviderManager should cache static models from providers.json
    cached_models = pm.get_cached_models('lm_studio')
    assert cached_models, f"Expected cached models for 'lm_studio', got {cached_models}"

    # Deterministic check: ProviderManager should attach provider metadata to adapter
    assert getattr(adapter, 'provider', None) is not None
    assert adapter.provider.get('name') == 'lm_studio'


def test_provider_manager_loads_ollama_adapter_from_config(tmp_path):
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    providers_path = cfg_dir / 'providers.json'
    providers = [
        {
            "name": "ollama",
            "type": "ollama",
            "base_url": "http://localhost:11434/api",
            "models": ["qwen3.5:9b"],
        }
    ]
    providers_path.write_text(json.dumps(providers), encoding='utf-8')

    pm = ProviderManager(providers_config_path=str(providers_path))
    import asyncio
    asyncio.run(pm.initialize())
    keys = pm.list_providers()
    assert 'ollama' in keys
    adapter = pm.get_provider('ollama')
    assert adapter is not None
    assert hasattr(adapter, 'missing_provider')
    assert adapter.missing_provider is False

    # Deterministic check: ProviderManager should cache static models for ollama
    cached_models_ollama = pm.get_cached_models('ollama')
    assert cached_models_ollama, f"Expected cached models for 'ollama', got {cached_models_ollama}"

    # Deterministic check: Adapter should have provider metadata attached
    assert getattr(adapter, 'provider', None) is not None
    assert adapter.provider.get('name') == 'ollama'
