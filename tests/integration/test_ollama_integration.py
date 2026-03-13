import os
import json
from pathlib import Path
import pytest
from src.adapters.ollama_adapter import OllamaAdapter

pytestmark = pytest.mark.ollama

RUN = os.getenv('RUN_INTEGRATION') == '1'
if not RUN:
    try:
        cfg_path = Path(__file__).parents[2] / 'src' / 'config' / 'providers.json'
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for p in providers:
                t = str(p.get('type') or '').lower()
                name = str(p.get('name') or '').lower()
                if 'ollama' in t or 'ollama' in name:
                    RUN = True
                    break
    except Exception:
        RUN = RUN

@pytest.mark.skipif(not RUN, reason='Integration tests disabled for Ollama')
def test_ollama_integration():
    # placeholder for Ollama integration smoke test
    assert True

@pytest.mark.skipif(not RUN, reason='Integration tests disabled')
def test_ollama_list_and_show():
    adapter = OllamaAdapter()
    models = adapter.get_models_from_api()
    assert isinstance(models, dict)
    # try update models list
    updated = adapter.update_models_list()
    assert isinstance(updated, list)
    # try show info for first model if exists
    if updated:
        info = adapter.get_model_info(updated[0])
        assert isinstance(info, dict)
