"""Unit tests for the Google Gemini adapter.

Tests are network-free — all HTTP calls are monkeypatched.
"""

from __future__ import annotations

import json
from pathlib import Path
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


class TestGeminiAdapter:
    """Tests for GeminiAdapter."""

    def test_import_and_alias(self):
        from src.core.inference.adapters.gemini_adapter import Adapter, GeminiAdapter

        assert GeminiAdapter is not None
        assert Adapter is GeminiAdapter

    def test_instantiation_defaults_no_key(self, monkeypatch):
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with patch(
            "src.core.user_prefs.UserPrefs.load", side_effect=Exception("no prefs")
        ):
            a = GeminiAdapter()
        assert a.name == "gemini"
        assert a.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert a.default_model == "gemini-3.6-flash"
        assert a.supports_native_tools is True
        assert a.REQUIRES_API_KEY is True
        assert a.api_key is None

    def test_api_key_resolution_order(self, monkeypatch):
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        # 1. Constructor arg wins
        a = GeminiAdapter(api_key="ctor_key")
        assert a.api_key == "ctor_key"

        # 2. GEMINI_API_KEY env var fallback
        monkeypatch.setenv("GEMINI_API_KEY", "env_gemini_key")
        monkeypatch.setenv("GOOGLE_API_KEY", "env_google_key")
        with patch(
            "src.core.user_prefs.UserPrefs.load", side_effect=Exception("no prefs")
        ):
            a2 = GeminiAdapter()
        assert a2.api_key == "env_gemini_key"

        # 3. GOOGLE_API_KEY fallback when GEMINI_API_KEY unset
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with patch(
            "src.core.user_prefs.UserPrefs.load", side_effect=Exception("no prefs")
        ):
            a3 = GeminiAdapter()
        assert a3.api_key == "env_google_key"

    def test_compose_preserves_openai_compat_path(self):
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        a = GeminiAdapter(api_key="test_key")
        assert (
            a._compose("chat/completions")
            == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        assert (
            a._compose("models")
            == "https://generativelanguage.googleapis.com/v1beta/openai/models"
        )

    def test_apply_provider_flags_omits_functions_and_think(self):
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        a = GeminiAdapter(api_key="test_key")
        a.provider = {"disable_thinking": True, "skip_functions_compat": False}
        payload = {
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
            "functions": [{"name": "read_file"}],
            "think": False,
        }
        a._apply_provider_flags(payload)
        assert "tools" in payload
        assert "functions" not in payload
        assert "think" not in payload

    def test_get_models_from_api_strips_prefix_and_filters_non_chat(self):
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        api_response = {
            "data": [
                {"id": "models/gemini-3.6-flash"},
                {"id": "models/gemini-3.5-flash-lite"},
                {"id": "models/gemma-4-31b-it"},
                {"id": "models/gemini-3.1-flash-image"},
                {"id": "models/gemini-3.8-live"},
                {"id": "models/gemini-2.5-flash-native-audio-latest"},
                {"id": "models/gemini-robotics-er-2-preview"},
                {"id": "models/text-embedding-004"},
                {"id": "models/gemini-embedding-001"},
                {"id": "models/imagen-3.0-generate-002"},
            ]
        }
        a = GeminiAdapter(api_key="test_key")
        with patch("requests.get", return_value=_mock_response(200, api_response)):
            result = a.get_models_from_api()
        ids = [m["id"] for m in result["models"]]
        assert ids == ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemma-4-31b-it"]

    def test_validate_connection(self):
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        a = GeminiAdapter(api_key="test_key")
        with patch("requests.get", return_value=_mock_response(200, {"data": []})):
            assert a.validate_connection() is True
        with patch("requests.get", return_value=_mock_response(401, {"error": "bad"})):
            assert a.validate_connection() is False

    def test_providers_json_contains_gemini(self):
        providers_path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        providers = json.loads(providers_path.read_text(encoding="utf-8"))
        entries = [p for p in providers if p.get("type") == "gemini"]
        assert len(entries) == 1
        g = entries[0]
        assert g["active"] is False
        assert g["supports_native_tools"] is True
        assert g["skip_functions_compat"] is True
        assert g["disable_thinking"] is False
        assert "gemini-3.6-flash" in g["models"]
        assert "gemini-3.5-flash-lite" in g["models"]

    def test_hard_daily_quota_429_fails_fast_without_retry(self):
        """Daily/billing quota 429s must not trigger 3x retry backoff loops."""
        from src.core.inference.adapters.gemini_adapter import GeminiAdapter

        a = GeminiAdapter(api_key="test_key")
        quota_resp = _mock_response(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "Quota exceeded: Generate content requests per day limit exceeded on Free Tier. Please retry in 18h10m.",
                }
            },
        )
        quota_resp.headers = {}
        calls = []

        def _fake_post(*args, **kwargs):
            calls.append(1)
            return quota_resp

        with patch.object(a, "_safe_post", side_effect=_fake_post), patch("time.sleep") as mock_sleep:
            res = a._execute_with_retry("https://example.invalid/chat", {}, stream=False)

        assert res is quota_resp
        assert len(calls) == 1
        mock_sleep.assert_not_called()

    def test_parse_retry_wait_seconds_handles_go_durations(self):
        from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

        assert OpenAICompatibleAdapter._parse_retry_wait_seconds("12") == 12.0
        assert OpenAICompatibleAdapter._parse_retry_wait_seconds("24.5s") == 24.5
        assert abs((OpenAICompatibleAdapter._parse_retry_wait_seconds("500ms") or 0.0) - 0.5) < 1e-6
        assert OpenAICompatibleAdapter._parse_retry_wait_seconds("1m5s") == 65.0
        assert OpenAICompatibleAdapter._parse_retry_wait_seconds("invalid") is None

    def test_transient_rpm_429_is_not_treated_as_hard_quota(self):
        """Per-minute (15 RPM) Gemini 429s must still retry, unlike daily RPD 429s."""
        from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

        rpm_resp = _mock_response(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "You exceeded your current quota, please check your plan and billing details. Quota exceeded for metric: generate_content_free_tier_requests, limit: 15. Please retry in 8.5s.",
                }
            },
        )
        rpm_resp.headers = {}
        assert OpenAICompatibleAdapter._is_hard_quota_429(rpm_resp) is False

    def test_normalize_tool_calls_preserves_gemini_thought_signature(self):
        """Gemini 3 extra_content.google.thought_signature must survive history normalization."""
        from src.core.orchestration.graph.nodes.frontier_dispatch import (
            normalize_tool_calls_with_ids,
        )

        raw_tc = {
            "id": "call_gem_1",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path": "a.py", "content": "x=1"}'},
            "extra_content": {"google": {"thought_signature": "sig_abc123"}},
        }
        norm = normalize_tool_calls_with_ids([raw_tc], turn_index=0)
        assert len(norm) == 1
        _, name, args, openai_tc = norm[0]
        assert name == "write_file"
        assert args["path"] == "a.py"
        assert openai_tc["extra_content"] == {"google": {"thought_signature": "sig_abc123"}}
