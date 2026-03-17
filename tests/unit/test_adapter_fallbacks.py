from unittest.mock import patch, mock_open
import json
from src.adapters.lm_studio_adapter import LmStudioAdapter
from src.adapters.ollama_adapter import OllamaAdapter


def test_lm_studio_adapter_fallback(tmp_path):
    # write a temporary providers.json and pass its path explicitly to the adapter
    mock_providers = [
        {
            "type": "lm_studio",
            "name": "lm_studio",
            "base_url": "http://mock-lm-studio:1234",
            "api_key": "mock-key",
            "models": ["mock/model"]
        }
    ]
    providers_path = tmp_path / "providers.json"
    providers_path.write_text(json.dumps(mock_providers), encoding="utf-8")

    adapter = LmStudioAdapter(providers_config_path=str(providers_path))
    assert adapter.base_url == "http://mock-lm-studio:1234"
    assert adapter.api_key == "mock-key"
    assert adapter.default_model == "mock/model"


def test_ollama_adapter_fallback(tmp_path):
    # write a temporary providers.json and pass its path to the OllamaAdapter
    mock_providers = [
        {
            "type": "ollama",
            "name": "ollama",
            "base_url": "http://mock-ollama:11434",
            "models": ["mock-ollama-model"]
        }
    ]
    providers_path = tmp_path / "providers.json"
    providers_path.write_text(json.dumps(mock_providers), encoding="utf-8")

    adapter = OllamaAdapter(config_path=str(providers_path))
    # base_url may be normalized by the adapter; check startswith to be tolerant
    assert adapter.base_url.startswith("http://mock-ollama")
    assert adapter.models == ["mock-ollama-model"]
