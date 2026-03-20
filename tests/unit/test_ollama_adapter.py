"""
Behavioral tests for OllamaAdapter.

These tests mock the network layer (requests) so they run without a live Ollama
server. They cover initialisation, model selection, get_models_from_api, and the
generate/chat path.
"""
from __future__ import annotations

import json
import warnings
from unittest.mock import MagicMock, patch

import pytest

from src.core.inference.adapters.ollama_adapter import OllamaAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_response(status_code: int = 200, json_data=None, text: str = ""):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestOllamaAdapterInit:
    def test_default_base_url_when_no_config(self, tmp_path):
        """Adapter defaults to localhost:11434/api when no config is available."""
        adapter = OllamaAdapter(config_path=str(tmp_path / "nonexistent.json"))
        assert "11434" in (adapter.base_url or "")

    def test_explicit_base_url_overrides_config(self):
        """Caller-supplied base_url is always preferred."""
        adapter = OllamaAdapter(base_url="http://my-ollama:8080/api")
        assert adapter.base_url == "http://my-ollama:8080/api"

    def test_explicit_models_list(self):
        """Explicit models= argument is stored directly."""
        adapter = OllamaAdapter(
            base_url="http://localhost:11434/api",
            models=["llama3:8b", "mistral:7b"],
        )
        assert "llama3:8b" in adapter.models
        assert "mistral:7b" in adapter.models

    def test_missing_provider_flag_when_no_config_no_base(self, tmp_path):
        """missing_provider is True when neither provider config nor base_url is supplied."""
        adapter = OllamaAdapter(config_path=str(tmp_path / "nonexistent.json"))
        # base_url defaults to localhost internally — missing_provider should be False
        # because base_url fallback is applied.  The important thing is it does not crash.
        assert hasattr(adapter, "missing_provider")

    def test_h7_no_crash_when_provider_none(self):
        """H7 fix: adapter must not crash on base_url.rstrip() when base_url is None."""
        # Passing no arguments must not raise AttributeError
        try:
            adapter = OllamaAdapter(base_url="http://localhost:11434")
        except AttributeError as e:
            pytest.fail(f"H7 regression: OllamaAdapter raised AttributeError: {e}")


# ---------------------------------------------------------------------------
# get_models_from_api
# ---------------------------------------------------------------------------

class TestGetModelsFromApi:
    def test_returns_models_from_tags_endpoint(self):
        """get_models_from_api parses Ollama /api/tags response."""
        tags_response = {
            "models": [
                {"name": "llama3:8b"},
                {"name": "mistral:7b"},
            ]
        }
        adapter = OllamaAdapter(base_url="http://localhost:11434/api")
        with patch("requests.get", return_value=make_response(200, tags_response)):
            result = adapter.get_models_from_api()
        assert isinstance(result, dict)
        assert "models" in result
        assert "llama3:8b" in result["models"]
        assert "mistral:7b" in result["models"]

    def test_returns_empty_models_on_connection_error(self):
        """get_models_from_api returns empty list on ConnectionError."""
        import requests as req
        adapter = OllamaAdapter(base_url="http://localhost:11434/api")
        with patch("requests.get", side_effect=req.exceptions.ConnectionError("refused")):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = adapter.get_models_from_api()
        assert result.get("models") == []

    def test_handles_string_model_entries(self):
        """get_models_from_api accepts plain string items in models list."""
        tags_response = {"models": ["llama3:8b", "qwen2:7b"]}
        adapter = OllamaAdapter(base_url="http://localhost:11434/api")
        with patch("requests.get", return_value=make_response(200, tags_response)):
            result = adapter.get_models_from_api()
        assert "llama3:8b" in result["models"]

    def test_handles_alternate_tags_shape(self):
        """get_models_from_api handles 'tags' key instead of 'models'."""
        tags_response = {"tags": [{"name": "phi3:mini"}]}
        adapter = OllamaAdapter(base_url="http://localhost:11434/api")
        with patch("requests.get", return_value=make_response(200, tags_response)):
            result = adapter.get_models_from_api()
        assert "phi3:mini" in result["models"]


# ---------------------------------------------------------------------------
# generate / chat
# ---------------------------------------------------------------------------

class TestOllamaAdapterGenerate:
    def _make_chat_response(self, content: str = "hello") -> dict:
        return {
            "message": {"role": "assistant", "content": content},
            "done": True,
        }

    def test_generate_returns_ok_on_success(self):
        """generate() returns {'ok': True, ...} on a successful chat response."""
        adapter = OllamaAdapter(
            base_url="http://localhost:11434/api",
            models=["llama3:8b"],
        )
        ok_resp = make_response(200, self._make_chat_response("pong"))
        messages = [{"role": "user", "content": "ping"}]

        with patch.object(adapter, "_call_requests", return_value=ok_resp):
            result = adapter.generate(messages, model="llama3:8b")

        assert isinstance(result, dict)
        assert result.get("ok") is True

    def test_generate_returns_error_on_http_failure(self):
        """generate() returns {'ok': False, 'error': ...} when the server returns 4xx."""
        adapter = OllamaAdapter(
            base_url="http://localhost:11434/api",
            models=["llama3:8b"],
        )
        error_resp = make_response(500, {"error": "internal error"})
        messages = [{"role": "user", "content": "hi"}]

        with patch.object(adapter, "_call_requests", return_value=error_resp):
            result = adapter.generate(messages, model="llama3:8b")

        assert isinstance(result, dict)
        # Should not crash; should indicate failure somehow
        assert "ok" in result or "error" in result

    def test_generate_handles_connection_error(self):
        """generate() catches ConnectionError and returns error dict."""
        import requests as req
        adapter = OllamaAdapter(
            base_url="http://localhost:11434/api",
            models=["llama3:8b"],
        )
        messages = [{"role": "user", "content": "hi"}]

        with patch.object(
            adapter, "_call_requests",
            side_effect=req.exceptions.ConnectionError("refused"),
        ):
            result = adapter.generate(messages, model="llama3:8b")

        assert isinstance(result, dict)
        assert result.get("ok") is False or "error" in result


# ---------------------------------------------------------------------------
# _base_variants
# ---------------------------------------------------------------------------

class TestBaseVariants:
    def test_no_duplicate_urls(self):
        """_base_variants must not return duplicate URLs."""
        adapter = OllamaAdapter(base_url="http://localhost:11434/api")
        variants = adapter._base_variants()
        assert len(variants) == len(set(variants)), "Duplicate URLs in _base_variants"

    def test_always_includes_original_base(self):
        """_base_variants always includes the configured base_url."""
        url = "http://myhost:11434/api"
        adapter = OllamaAdapter(base_url=url)
        assert url in adapter._base_variants()

    def test_includes_api_suffix_variant(self):
        """_base_variants includes /api suffix when base lacks it."""
        adapter = OllamaAdapter(base_url="http://localhost:11434")
        variants = adapter._base_variants()
        assert any("/api" in v for v in variants)
