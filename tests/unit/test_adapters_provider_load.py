import json
from pathlib import Path
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
    # LM Studio models should be normalized to full ids when possible
    if hasattr(adapter, 'models') and adapter.models:
        # models are stored as strings; check they contain a '/'
        assert any('/' in str(m) for m in adapter.models), f"Expected full LM Studio ids in adapter.models, got {adapter.models}"


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
