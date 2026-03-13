import os
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.adapters.ollama_adapter import OllamaAdapter


@pytest.mark.integration
def test_ollama_generate_and_chat():
    adapter = OllamaAdapter(os.path.join(ROOT, 'src', 'config', 'providers.json'))
    # skip if no models configured
    if not adapter.models:
        pytest.skip('No models configured for Ollama in providers.json')

    prompt = 'You are a strict JSON-only assistant. Respond ONLY with valid JSON. Return keys: summary, explanation. Question: Why is the sky blue?'
    try:
        res = adapter.generate(prompt, stream=False, format_json=True)
    except ValueError as e:
        pytest.skip(f'Ollama adapter not configured: {e}')
    assert isinstance(res, dict)
    # allow either empty response but meta thinking present or response parsed
    assert 'response' in res or 'meta' in res

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Respond in JSON if asked."},
        {"role": "user", "content": "Why is the sky blue? Respond in JSON with summary and explanation."}
    ]
    try:
        cresp = adapter.generate(messages, stream=False, format_json=True)
    except ValueError as e:
        pytest.skip(f'Ollama adapter not configured: {e}')
    assert isinstance(cresp, dict)
    assert 'response' in cresp or 'meta' in cresp

