"""Unit tests for the Groq adapter.

Tests are network-free — all HTTP calls are monkeypatched.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _mock_response(status_code: int, json_data: dict):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.text = json.dumps(json_data)
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return r


class TestGroqAdapter:
    """Tests for GroqAdapter."""

    def test_import(self):
        """GroqAdapter can be imported."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        assert GroqAdapter is not None

    def test_instantiation_no_key(self):
        """GroqAdapter instantiates without an API key (will validate later)."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        with patch(
            "src.core.user_prefs.UserPrefs.load", side_effect=Exception("no prefs")
        ):
            a = GroqAdapter()
        assert a.name == "groq"
        assert a.base_url == "https://api.groq.com/openai/v1"
        assert a.default_model == "llama-3.1-8b-instant"

    def test_instantiation_with_key(self):
        """API key is stored on adapter."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        a = GroqAdapter(api_key="gsk_test_key_123")
        assert a.api_key == "gsk_test_key_123"

    def test_instantiation_with_env_key(self, monkeypatch):
        """GROQ_API_KEY env var is picked up."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        monkeypatch.setenv("GROQ_API_KEY", "gsk_env_key")
        with patch(
            "src.core.user_prefs.UserPrefs.load", side_effect=Exception("no prefs")
        ):
            a = GroqAdapter()
        assert a.api_key == "gsk_env_key"
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

    def test_default_models_list(self):
        """Default models list is non-empty."""
        from src.core.inference.adapters.groq_adapter import (
            GroqAdapter,
            _DEFAULT_MODELS,
        )

        a = GroqAdapter()
        assert len(a.models) > 0
        assert a.models[0] == _DEFAULT_MODELS[0]

    def test_validate_connection_no_key_returns_false(self):
        """validate_connection returns False when no API key is available."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        with patch("src.core.user_prefs.UserPrefs.load", side_effect=Exception):
            a = GroqAdapter()
        a.api_key = None
        assert a.validate_connection() is False

    def test_validate_connection_success(self):
        """validate_connection returns True on HTTP 200."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        a = GroqAdapter(api_key="gsk_test")
        with patch("requests.get", return_value=_mock_response(200, {"data": []})):
            assert a.validate_connection() is True

    def test_validate_connection_failure(self):
        """validate_connection returns False on HTTP error."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        a = GroqAdapter(api_key="gsk_test")
        with patch("requests.get", side_effect=Exception("network error")):
            assert a.validate_connection() is False

    def test_get_models_from_api_no_key_returns_defaults(self):
        """get_models_from_api returns default list when no API key."""
        from src.core.inference.adapters.groq_adapter import (
            GroqAdapter,
            _DEFAULT_MODELS,
        )

        with patch("src.core.user_prefs.UserPrefs.load", side_effect=Exception):
            a = GroqAdapter()
        a.api_key = None
        result = a.get_models_from_api()
        assert "models" in result
        ids = [m["id"] for m in result["models"]]
        assert _DEFAULT_MODELS[0] in ids

    def test_get_models_from_api_parses_response(self):
        """get_models_from_api parses model data from the Groq API."""
        from src.core.inference.adapters.groq_adapter import GroqAdapter

        api_response = {
            "data": [
                {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B Instant"},
                {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B"},
            ]
        }
        a = GroqAdapter(api_key="gsk_test")
        with patch("requests.get", return_value=_mock_response(200, api_response)):
            result = a.get_models_from_api()
        ids = [m["id"] for m in result["models"]]
        assert "llama-3.1-8b-instant" in ids
        assert "llama-3.3-70b-versatile" in ids

    def test_get_models_from_api_network_error_returns_defaults(self):
        """get_models_from_api falls back to defaults on network error."""
        from src.core.inference.adapters.groq_adapter import (
            GroqAdapter,
            _DEFAULT_MODELS,
        )

        a = GroqAdapter(api_key="gsk_test")
        with patch("requests.get", side_effect=Exception("timeout")):
            result = a.get_models_from_api()
        ids = [m["id"] for m in result["models"]]
        assert _DEFAULT_MODELS[0] in ids

    def test_adapter_alias(self):
        """Module-level Adapter alias matches GroqAdapter."""
        from src.core.inference.adapters.groq_adapter import Adapter, GroqAdapter

        assert Adapter is GroqAdapter

    def test_providers_json_contains_groq(self):
        """providers.json contains a Groq entry with inactive status."""
        import json
        from pathlib import Path

        providers_path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        providers = json.loads(providers_path.read_text())
        groq_entries = [p for p in providers if p.get("type") == "groq"]
        assert len(groq_entries) == 1
        g = groq_entries[0]
        assert g["active"] is False
        assert "llama-3.1-8b-instant" in g["models"]
        assert g.get("small_model") == "llama-3.1-8b-instant"
