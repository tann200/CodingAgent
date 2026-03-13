import json
from pathlib import Path
import asyncio
import tempfile

from src.core.llm_manager import call_model


def test_call_model_uses_providers_json(tmp_path, monkeypatch):
    # create a dummy providers.json pointing to a mock adapter module location by type 'mock'
    p = tmp_path / 'providers.json'
    # We'll create a fake adapter module under src.adapters.mock_adapter
    # But simplest: write providers.json with name 'test' and type 'ollama' and base_url that won't be used
    data = {"name": "test_local", "type": "ollama", "base_url": "http://localhost:9999", "models": ["m1"]}
    p.write_text(json.dumps(data), encoding='utf-8')
    # monkeypatch llm_manager config path resolution to use this tmp providers.json
    monkeypatch.setattr('src.core.llm_manager.resolve_config_path', lambda path=None: Path(p))

    # monkeypatch OllamaAdapter to a local dummy adapter class
    class DummyAdapter:
        def __init__(self, base_url=None, config_path=None):
            self.default_model = 'm1'
        def chat(self, messages, model=None, stream=False, format_json=False, **kwargs):
            return {'ok': True, 'model': model or 'm1', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}], 'usage': {'prompt_tokens':1,'completion_tokens':2,'total_tokens':3}}

    # insert DummyAdapter into src.adapters.ollama_adapter module namespace
    import importlib, types
    modname = 'src.adapters.ollama_adapter'
    try:
        mod = importlib.import_module(modname)
    except Exception:
        mod = types.ModuleType(modname)
        import sys
        sys.modules[modname] = mod
    setattr(mod, 'OllamaAdapter', DummyAdapter)

    # call call_model asynchronously
    async def _run():
        res = await call_model([{'role':'user','content':'hi'}], provider='test_local', model='m1')
        return res

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        res = loop.run_until_complete(_run())
    finally:
        loop.close()

    assert isinstance(res, dict)
    assert res.get('ok') is True
    assert 'choices' in res

