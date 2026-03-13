import pytest
from unittest.mock import patch, mock_open
import json
from src.adapters.lm_studio_adapter import LmStudioAdapter
from src.adapters.ollama_adapter import OllamaAdapter

def test_lm_studio_adapter_fallback(monkeypatch):
    mock_data = json.dumps([{
        "type": "lm_studio",
        "name": "lm_studio",
        "base_url": "http://mock-lm-studio:1234",
        "api_key": "mock-key",
        "models": ["mock/model"]
    }])
    
    with patch('pathlib.Path.read_text', return_value=mock_data), \
         patch('pathlib.Path.exists', return_value=True):
        adapter = LmStudioAdapter()
        assert adapter.base_url == "http://mock-lm-studio:1234"
        assert adapter.api_key == "mock-key"
        assert adapter.default_model == "mock/model"
        
def test_ollama_adapter_fallback(monkeypatch):
    mock_data = json.dumps([{
        "type": "ollama",
        "name": "ollama",
        "base_url": "http://mock-ollama:11434",
        "models": ["mock-ollama-model"]
    }])
    
    with patch('pathlib.Path.read_text', return_value=mock_data), \
         patch('pathlib.Path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=mock_data)):
        adapter = OllamaAdapter()
        assert adapter.base_url == "http://mock-ollama:11434"
        assert adapter.models == ["mock-ollama-model"]
