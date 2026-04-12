"""Unit tests for the LiteLLM proxy adapter.

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


class TestLiteLLMAdapter:
    """Tests for LiteLLMAdapter."""

    def test_import(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        assert LiteLLMAdapter is not None

    def test_alias(self):
        from src.core.inference.adapters.litellm_adapter import (
            Adapter,
            LiteLLMAdapter,
            LitellmAdapter,
        )

        assert Adapter is LiteLLMAdapter
        assert LitellmAdapter is LiteLLMAdapter

    def test_default_base_url(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        with patch("src.core.user_prefs.UserPrefs.load", side_effect=Exception):
            a = LiteLLMAdapter()
        assert a.base_url is not None and "localhost:4000" in a.base_url

    def test_custom_base_url(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(base_url="http://litellm.company.com:8000")
        assert a.base_url is not None and "company.com" in a.base_url

    def test_no_api_key_is_allowed(self):
        """LiteLLM runs locally without auth — no key is not an error."""
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        with patch("src.core.user_prefs.UserPrefs.load", side_effect=Exception):
            a = LiteLLMAdapter()
        assert a.api_key is None

    def test_api_key_from_env(self, monkeypatch):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        monkeypatch.setenv("LITELLM_API_KEY", "sk-litellm-test")
        with patch("src.core.user_prefs.UserPrefs.load", side_effect=Exception):
            a = LiteLLMAdapter()
        assert a.api_key == "sk-litellm-test"
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)

    def test_models_from_providers_json(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["gpt-4o", "claude-3.5-sonnet"])
        assert "gpt-4o" in a.models
        assert "claude-3.5-sonnet" in a.models

    def test_validate_connection_no_base_url(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter()
        a.base_url = None
        assert a.validate_connection() is False

    def test_validate_connection_success(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(base_url="http://localhost:4000")
        with patch("requests.get", return_value=_mock_response(200, {"data": []})):
            assert a.validate_connection() is True

    def test_validate_connection_server_error_returns_false(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(base_url="http://localhost:4000")
        with patch("requests.get", return_value=_mock_response(500, {})):
            assert a.validate_connection() is False

    def test_validate_connection_network_error_returns_false(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(base_url="http://localhost:4000")
        with patch("requests.get", side_effect=Exception("connection refused")):
            assert a.validate_connection() is False

    def test_get_models_falls_back_when_unreachable(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(
            base_url="http://localhost:4000",
            models=["model-a", "model-b"],
        )
        with patch("requests.get", side_effect=Exception("timeout")):
            result = a.get_models_from_api()
        ids = [m["id"] for m in result["models"]]
        assert "model-a" in ids

    def test_get_models_parses_response(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(base_url="http://localhost:4000")
        api_response = {
            "data": [
                {"id": "gpt-4o", "name": "GPT-4o"},
                {"id": "llama-3.1-8b", "name": "Llama 3.1 8B"},
            ]
        }
        with patch("requests.get", return_value=_mock_response(200, api_response)):
            result = a.get_models_from_api()
        ids = [m["id"] for m in result["models"]]
        assert "gpt-4o" in ids
        assert "llama-3.1-8b" in ids

    def test_providers_json_contains_litellm(self):
        import json
        from pathlib import Path

        providers_path = Path(__file__).parents[2] / "src" / "config" / "providers.json"
        providers = json.loads(providers_path.read_text())
        litellm_entries = [p for p in providers if p.get("type") == "litellm"]
        assert len(litellm_entries) == 1
        assert litellm_entries[0]["active"] is False


class TestLiteLLMAdapterGap4:
    """GAP-4: model_tier property and thinking-token stripping."""

    def test_model_tier_small_for_edge_model(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["gemma-4-e4b-it"])
        assert a.model_tier == "small"

    def test_model_tier_frontier_for_large_model(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["gemma-4-31b-it"])
        assert a.model_tier == "frontier"

    def test_model_tier_frontier_for_gpt4o(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["gpt-4o"])
        assert a.model_tier == "frontier"

    def test_model_tier_small_for_14b(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["qwen3:14b"])
        assert a.model_tier == "small"

    def test_model_tier_medium_for_unknown_model(self):
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["some-unknown-model-xyz"])
        assert a.model_tier == "medium"

    def test_model_tier_respects_context_window(self):
        """A model with no param count but large context window → MEDIUM or LARGE."""
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["mystery-model"])
        # Set context_window dynamically (as production code does via event)
        a.context_window = 200000
        # 200K context → LARGE
        assert a.model_tier == "large"

    def test_thinking_stripping_for_reasoning_model(self):
        """generate() strips <think> tags for qwen3 models."""
        from unittest.mock import patch
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["qwen3:14b"])

        fake_response = {
            "ok": True,
            "provider": "litellm",
            "model": "qwen3:14b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>internal thought</think>Final answer here.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }

        with patch.object(
            a.__class__.__bases__[0],  # OpenAICompatibleAdapter
            "generate",
            return_value=fake_response,
        ):
            result = a.generate([{"role": "user", "content": "hello"}])

        content = result["choices"][0]["message"]["content"]
        assert "<think>" not in content
        assert "Final answer here." in content

    def test_no_stripping_for_non_reasoning_model(self):
        """generate() does NOT modify content for non-reasoning models."""
        from unittest.mock import patch
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["gpt-4o"])
        original_content = "This is a normal response."
        fake_response = {
            "ok": True,
            "provider": "litellm",
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"role": "assistant", "content": original_content},
                    "finish_reason": "stop",
                }
            ],
        }

        with patch.object(
            a.__class__.__bases__[0],
            "generate",
            return_value=fake_response,
        ):
            result = a.generate([{"role": "user", "content": "hi"}])

        assert result["choices"][0]["message"]["content"] == original_content

    def test_no_stripping_in_stream_mode(self):
        """Streaming responses are passed through unchanged."""
        from unittest.mock import patch, MagicMock
        from src.core.inference.adapters.litellm_adapter import LiteLLMAdapter

        a = LiteLLMAdapter(models=["qwen3:14b"])
        stream_sentinel = MagicMock()

        with patch.object(
            a.__class__.__bases__[0],
            "generate",
            return_value=stream_sentinel,
        ):
            result = a.generate([], stream=True)

        # Stream response returned unchanged (no stripping attempted)
        assert result is stream_sentinel
