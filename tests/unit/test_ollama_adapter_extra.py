import json
import requests
import pytest
from pathlib import Path

from src.adapters.ollama_adapter import OllamaAdapter


class DummyResp:
    def __init__(self, status_code=200, json_data=None, text_data=None, iter_lines=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text_data or (json.dumps(json_data) if json_data is not None else "")
        self._iter = iter_lines

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(f"Status: {self.status_code}")

    def json(self):
        return self._json

    def iter_lines(self):
        if self._iter is None:
            return iter([])
        return iter(self._iter)


@pytest.fixture()
def tmp_provider_file(tmp_path):
    p = tmp_path / "providers.json"
    p.write_text(json.dumps({"base_url": "http://localhost:11434/api", "models": ["qwen3.5:9b"]}), encoding='utf-8')
    return str(p)


def test_parse_json_response_field_with_valid_and_invalid(tmp_provider_file):
    a = OllamaAdapter(config_path=tmp_provider_file)
    assert a._parse_json_response_field('{"a":1}')["a"] == 1
    assert a._parse_json_response_field('not json') == 'not json'
    assert a._parse_json_response_field(None) is None


def test_get_model_info_posts_and_parses(monkeypatch, tmp_provider_file):
    a = OllamaAdapter(config_path=tmp_provider_file)
    payload = {"modelfile": "FROM x", "model_info": {"general": {"architecture": "qwen"}}}

    def fake_post(url, json=None, **kwargs):
        assert url.endswith('/show')
        assert json.get('model') == 'qwen3.5:9b'
        return DummyResp(json_data=payload)

    monkeypatch.setattr(requests, 'post', fake_post)
    info = a.get_model_info()
    assert info.get('modelfile') == 'FROM x' or info.get('model_info')


def test_generate_streaming_yields_json_chunks(monkeypatch, tmp_provider_file):
    a = OllamaAdapter(config_path=tmp_provider_file)
    # simulate streaming lines
    lines = [b'{"message": {"role":"assistant","content":"The"},"done":false}', b'{"done":true,"message": {"role":"assistant","content":" done"},"created_at":"now"}']

    def fake_post(url, json=None, stream=False, **kwargs):
        assert stream is True
        return DummyResp(json_data=None, iter_lines=lines)

    monkeypatch.setattr(requests, 'post', fake_post)
    res = a.generate([{"role": "user", "content": "hi"}], stream=True)
    assert res.get('ok') is True
    gen = res.get('raw')
    assert hasattr(gen, '__iter__')
    chunks = list(gen)
    assert isinstance(chunks, list)
    assert any(isinstance(c, dict) for c in chunks)


def test_chat_streaming_yields(monkeypatch, tmp_provider_file):
    a = OllamaAdapter(config_path=tmp_provider_file)
    lines = [b'{"message": {"role":"assistant","content":"Hel"}, "done": false}', b'{"message": {"role":"assistant","content":"lo"}, "done": true}']

    def fake_post(url, json=None, stream=False, **kwargs):
        assert stream is True
        return DummyResp(iter_lines=lines)

    monkeypatch.setattr(requests, 'post', fake_post)
    res = a.generate([{"role":"user","content":"hi"}], stream=True)
    gen = res.get('raw')
    out = list(gen)
    assert len(out) == 2
    assert any((isinstance(x, dict) and 'message' in x) for x in out)


def test_extract_tool_calls_with_native_and_json(tmp_provider_file):
    a = OllamaAdapter(config_path=tmp_provider_file)
    # native tool_calls present
    resp_native = {"message": {"tool_calls": [{"name": "fs.read", "args": {"path": "/tmp/x"}}]}}
    assert a.extract_tool_calls(resp_native)[0]["name"] == 'fs.read'

    # json embedded in content
    resp_json = {"message": {"content": '{"tool_call": {"tool_name":"fs.read","parameters":{"path":"/tmp/x"}}}'}}
    calls = a.extract_tool_calls(resp_json)
    assert len(calls) == 1
    assert calls[0]["tool_name"] == 'fs.read'

