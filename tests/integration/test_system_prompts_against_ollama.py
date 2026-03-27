import os
import json
from pathlib import Path
import pytest
from src.core.inference.adapters.ollama_adapter import OllamaAdapter

pytestmark = pytest.mark.ollama

import requests as _requests

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
                base_url = p.get('base_url', 'http://localhost:11434')
                if 'ollama' in t or 'ollama' in name:
                    # Only enable if Ollama is actually reachable
                    try:
                        _requests.get(f"{base_url}/api/tags", timeout=2)
                        RUN = True
                    except Exception:
                        pass
                    break
    except Exception:
        pass

@pytest.mark.skipif(not RUN, reason='Integration tests disabled for Ollama')
def test_system_prompts_against_ollama():
    # placeholder: test system prompts against Ollama adapter
    assert True

@pytest.mark.skipif(not RUN, reason='Integration tests disabled')
def test_system_prompts_behaviour():
    adapter = OllamaAdapter()
    # read system prompts (markdown filenames)
    coding = (Path('agent-brain') / 'system_prompt_coding.md').read_text()
    planner = (Path('agent-brain') / 'system_prompt_planner.md').read_text()
    # send a simple prompt using coding prompt
    messages = [
        {"role": "system", "content": coding},
        {"role": "user", "content": "Explain why the sky is blue."}
    ]
    res = adapter.generate(messages, stream=False, format_json=False)
    assert isinstance(res, dict)
    # planner prompt
    messages = [
        {"role": "system", "content": planner},
        {"role": "user", "content": "Produce steps to add a new tool to the orchestrator."}
    ]
    pres = adapter.generate(messages, stream=False, format_json=False)
    assert isinstance(pres, dict)
