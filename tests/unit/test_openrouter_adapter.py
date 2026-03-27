"""Unit tests for the OpenRouter adapter and OpenAICompatibleAdapter base class.

Tests are network-free — all HTTP calls are monkeypatched.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, json_data: dict):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    r.text = json.dumps(json_data)
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return r


# ---------------------------------------------------------------------------
# OpenAICompatibleAdapter base class
# ---------------------------------------------------------------------------


class TestOpenAICompatibleAdapterBase:
    def test_headers_no_key(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        assert a._headers() == {"Content-Type": "application/json"}

    def test_headers_with_key(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(
            base_url="http://host/v1", api_key="sk-abc", name="test"
        )
        h = a._headers()
        assert h["Authorization"] == "Bearer sk-abc"

    def test_compose_with_v1_in_url(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        assert a._compose("models") == "http://host/v1/models"
        assert a._compose("chat/completions") == "http://host/v1/chat/completions"

    def test_compose_bare_host(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host", name="test")
        assert a._compose("models") == "http://host/api/v1/models"

    def test_models_endpoints_v1_suffix(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        eps = a._models_endpoints()
        assert eps == ["http://host/v1/models"]

    def test_models_endpoints_bare(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host", name="test")
        eps = a._models_endpoints()
        assert "http://host/v1/models" in eps

    def test_get_models_from_api_data_key(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        resp = _mock_response(
            200, {"data": [{"id": "vendor/model-7b", "name": "model-7b"}]}
        )
        with patch("requests.get", return_value=resp):
            result = a.get_models_from_api()
        assert len(result["models"]) == 1
        assert result["models"][0]["id"] == "vendor/model-7b"

    def test_get_models_from_api_models_key(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        resp = _mock_response(200, {"models": [{"id": "m1"}, {"id": "m2"}]})
        with patch("requests.get", return_value=resp):
            result = a.get_models_from_api()
        assert {m["id"] for m in result["models"]} == {"m1", "m2"}

    def test_get_models_from_api_empty_on_error(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )
        import requests as _req

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        with patch(
            "requests.get", side_effect=_req.exceptions.ConnectionError("refused")
        ):
            result = a.get_models_from_api()
        assert result == {"models": []}

    def test_generate_normalizes_response(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(
            base_url="http://host/v1", api_key="k", name="myname", models=["vendor/m1"]
        )
        raw = {
            "model": "vendor/m1",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        resp = _mock_response(200, raw)
        with patch("requests.post", return_value=resp):
            out = a.generate([{"role": "user", "content": "hi"}])
        assert out["ok"] is True
        assert out["provider"] == "myname"
        assert out["choices"][0]["message"]["content"] == "Hello"
        assert out["prompt_tokens"] == 10
        assert out["completion_tokens"] == 5

    def test_generate_error_on_no_model(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        out = a.generate([{"role": "user", "content": "hi"}])
        assert out["ok"] is False

    def test_extract_tool_calls_openai_format(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(name="test")
        resp = {"tool_calls": [{"name": "read_file", "arguments": {"path": "/foo"}}]}
        calls = a.extract_tool_calls(resp)
        assert calls == [{"name": "read_file", "args": {"path": "/foo"}}]

    def test_resolve_model_name_identity(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(name="test")
        assert (
            a.resolve_model_name("anthropic/claude-3.5-sonnet")
            == "anthropic/claude-3.5-sonnet"
        )
        assert a.resolve_model_name("some-model") == "some-model"

    def test_validate_connection_ok(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(base_url="http://host/v1", name="test")
        resp = _mock_response(200, {"data": []})
        with patch("requests.get", return_value=resp):
            assert a.validate_connection() is True

    def test_validate_connection_no_url(self):
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        a = OpenAICompatibleAdapter(name="test")
        assert a.validate_connection() is False


# ---------------------------------------------------------------------------
# LmStudioAdapter — still works correctly after refactor
# ---------------------------------------------------------------------------


class TestLmStudioAdapterRefactored:
    def test_extends_openai_compat(self):
        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        assert issubclass(LmStudioAdapter, OpenAICompatibleAdapter)

    def test_resolve_model_name_short(self):
        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

        a = LmStudioAdapter(
            base_url="http://localhost:1234/v1",
            models=["qwen/qwen3.5-9b"],
        )
        assert a.resolve_model_name("qwen3.5-9b") == "qwen/qwen3.5-9b"

    def test_resolve_model_name_full_passthrough(self):
        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

        a = LmStudioAdapter(base_url="http://localhost:1234/v1")
        assert a.resolve_model_name("vendor/full-id") == "vendor/full-id"

    def test_generate_provider_field(self):
        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

        a = LmStudioAdapter(
            base_url="http://localhost:1234/v1",
            models=["qwen/qwen3.5-9b"],
            name="lm_studio",
        )
        raw = {
            "model": "qwen/qwen3.5-9b",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        resp = _mock_response(200, raw)
        with patch("requests.post", return_value=resp):
            out = a.generate([{"role": "user", "content": "hi"}])
        assert out["provider"] == "lm_studio"
        assert out["ok"] is True

    def test_env_var_fallback(self, monkeypatch, tmp_path):
        """Env vars are used when providers.json has no lm_studio entry."""
        monkeypatch.setenv("LM_STUDIO_URL", "http://envhost:1234/v1")
        monkeypatch.setenv("LM_STUDIO_MODEL", "env-model")
        # Point providers_config_path at an empty JSON array so no lm_studio entry
        empty_cfg = tmp_path / "providers.json"
        empty_cfg.write_text("[]", encoding="utf-8")
        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

        a = LmStudioAdapter(providers_config_path=str(empty_cfg))
        assert a.base_url == "http://envhost:1234/v1"
        assert a.default_model == "env-model"


# ---------------------------------------------------------------------------
# OpenRouterAdapter
# ---------------------------------------------------------------------------


class TestOpenRouterAdapter:
    def test_extends_openai_compat(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter
        from src.core.inference.adapters.openai_compat_adapter import (
            OpenAICompatibleAdapter,
        )

        assert issubclass(OpenRouterAdapter, OpenAICompatibleAdapter)

    def test_base_url_hardcoded(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test")
        assert a.base_url == "https://openrouter.ai/api/v1"

    def test_base_url_arg_ignored(self):
        """ProviderManager may pass base_url=None from providers.json — must not override BASE_URL."""
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test", base_url="http://wrong/v1")
        assert a.base_url == "https://openrouter.ai/api/v1"

    def test_name_default(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test")
        assert a.name == "openrouter"

    def test_requires_api_key_flag(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        assert OpenRouterAdapter.REQUIRES_API_KEY is True

    def test_default_model(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test")
        assert a.default_model == OpenRouterAdapter.DEFAULT_MODEL

    def test_custom_models(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(
            api_key="sk-test", models=["openai/gpt-4o", "anthropic/claude-3-opus"]
        )
        assert a.models == ["openai/gpt-4o", "anthropic/claude-3-opus"]
        assert a.default_model == "openai/gpt-4o"

    def test_headers_include_referer(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test")
        h = a._headers()
        assert "HTTP-Referer" in h
        assert "X-Title" in h
        assert h["Authorization"] == "Bearer sk-test"

    def test_headers_no_key_still_has_referer(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        with patch("src.core.user_prefs.UserPrefs.load") as mock_load:
            mock_load.return_value.get_provider_key.return_value = None
            a = OpenRouterAdapter()
        h = a._headers()
        assert "HTTP-Referer" in h
        assert "Authorization" not in h

    def test_api_key_from_user_prefs(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        with patch("src.core.user_prefs.UserPrefs.load") as mock_load:
            mock_load.return_value.get_provider_key.return_value = "sk-from-prefs"
            a = OpenRouterAdapter()
        assert a.api_key == "sk-from-prefs"

    def test_explicit_key_overrides_prefs(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        with patch("src.core.user_prefs.UserPrefs.load") as mock_load:
            mock_load.return_value.get_provider_key.return_value = "sk-prefs"
            a = OpenRouterAdapter(api_key="sk-explicit")
        assert a.api_key == "sk-explicit"

    def test_get_models_from_api(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test")
        payload = {
            "data": [
                {"id": "anthropic/claude-3.5-sonnet", "name": "Claude 3.5 Sonnet"},
                {"id": "openai/gpt-4o", "name": "GPT-4o"},
            ]
        }
        resp = _mock_response(200, payload)
        with patch("requests.get", return_value=resp):
            result = a.get_models_from_api()
        ids = [m["id"] for m in result["models"]]
        assert "anthropic/claude-3.5-sonnet" in ids
        assert "openai/gpt-4o" in ids

    def test_get_models_fallback_on_error(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter
        import requests as _req

        a = OpenRouterAdapter(api_key="sk-test")
        with patch(
            "requests.get", side_effect=_req.exceptions.ConnectionError("refused")
        ):
            result = a.get_models_from_api()
        # Should return at least the default model
        assert len(result["models"]) >= 1
        assert result["models"][0]["id"] == OpenRouterAdapter.DEFAULT_MODEL

    def test_generate_sends_to_openrouter(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test", models=["anthropic/claude-3.5-sonnet"])
        raw = {
            "model": "anthropic/claude-3.5-sonnet",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        }
        resp = _mock_response(200, raw)
        with patch("requests.post", return_value=resp) as mock_post:
            out = a.generate([{"role": "user", "content": "hello"}])
        assert out["ok"] is True
        assert out["provider"] == "openrouter"
        assert out["choices"][0]["message"]["content"] == "Hi"
        # Verify correct endpoint was called
        call_url = mock_post.call_args[0][0]
        assert "openrouter.ai" in call_url
        assert "chat/completions" in call_url

    def test_generate_includes_referer_header(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test", models=["anthropic/claude-3.5-sonnet"])
        raw = {
            "model": "anthropic/claude-3.5-sonnet",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        resp = _mock_response(200, raw)
        with patch("requests.post", return_value=resp) as mock_post:
            a.generate([{"role": "user", "content": "hi"}])
        called_headers = (
            mock_post.call_args[1].get("headers") or mock_post.call_args[0][1]
        )
        assert "HTTP-Referer" in called_headers

    def test_validate_connection_ok(self):
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter(api_key="sk-test")
        resp = _mock_response(200, {"data": []})
        with patch("requests.get", return_value=resp):
            assert a.validate_connection() is True

    def test_validate_connection_no_key_needed(self):
        """validate_connection uses the public /models endpoint — no API key required."""
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        a = OpenRouterAdapter()  # no api_key
        resp = _mock_response(200, {"data": []})
        with patch("requests.get", return_value=resp) as mock_get:
            result = a.validate_connection()
        # Should not include Authorization header
        called_headers = (
            mock_get.call_args[1].get("headers", {}) if mock_get.call_args else {}
        )
        assert "Authorization" not in called_headers
        assert result is True

    def test_module_aliases(self):
        """ProviderManager convention: module exposes Adapter and OpenrouterAdapter aliases."""
        import src.core.inference.adapters.openrouter_adapter as mod
        from src.core.inference.adapters.openrouter_adapter import OpenRouterAdapter

        assert mod.Adapter is OpenRouterAdapter
        assert mod.OpenrouterAdapter is OpenRouterAdapter


# ---------------------------------------------------------------------------
# SettingsPanelController — API key methods
# ---------------------------------------------------------------------------


class TestSettingsPanelApiKey:
    def test_provider_requires_api_key_true(self):
        from src.ui.views.settings_panel import SettingsPanelController

        ctrl = SettingsPanelController.__new__(SettingsPanelController)
        mock_pm = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter.REQUIRES_API_KEY = True
        mock_pm.get_provider.return_value = mock_adapter
        ctrl.pm = mock_pm
        ctrl.event_bus = None
        assert ctrl.provider_requires_api_key("openrouter") is True

    def test_provider_requires_api_key_false(self):
        from src.ui.views.settings_panel import SettingsPanelController

        ctrl = SettingsPanelController.__new__(SettingsPanelController)
        mock_pm = MagicMock()
        mock_adapter = MagicMock(spec=[])  # no REQUIRES_API_KEY attr
        mock_pm.get_provider.return_value = mock_adapter
        ctrl.pm = mock_pm
        ctrl.event_bus = None
        assert ctrl.provider_requires_api_key("lm_studio") is False

    def test_save_api_key_persists_to_prefs(self, tmp_path):
        from src.ui.views.settings_panel import SettingsPanelController

        ctrl = SettingsPanelController.__new__(SettingsPanelController)
        mock_pm = MagicMock()
        mock_pm.get_provider.return_value = MagicMock()
        ctrl.pm = mock_pm
        ctrl.event_bus = None
        with patch("src.ui.views.settings_panel.UserPrefs") as MockPrefs:
            mock_prefs_instance = MagicMock()
            MockPrefs.load.return_value = mock_prefs_instance
            result = ctrl.save_api_key("openrouter", "sk-test-key")
        assert result is True
        mock_prefs_instance.set_provider_setting.assert_called_once_with(
            "openrouter", "api_key", "sk-test-key"
        )
        mock_prefs_instance.save.assert_called_once()

    def test_save_api_key_injects_into_adapter(self):
        from src.ui.views.settings_panel import SettingsPanelController

        ctrl = SettingsPanelController.__new__(SettingsPanelController)
        mock_adapter = MagicMock()
        mock_pm = MagicMock()
        mock_pm.get_provider.return_value = mock_adapter
        ctrl.pm = mock_pm
        ctrl.event_bus = None
        with patch("src.ui.views.settings_panel.UserPrefs") as MockPrefs:
            MockPrefs.load.return_value = MagicMock()
            ctrl.save_api_key("openrouter", "sk-live")
        assert mock_adapter.api_key == "sk-live"

    def test_get_current_api_key(self):
        from src.ui.views.settings_panel import SettingsPanelController

        ctrl = SettingsPanelController.__new__(SettingsPanelController)
        ctrl.pm = MagicMock()
        ctrl.event_bus = None
        with patch("src.ui.views.settings_panel.UserPrefs") as MockPrefs:
            MockPrefs.load.return_value.get_provider_key.return_value = "sk-stored"
            key = ctrl.get_current_api_key("openrouter")
        assert key == "sk-stored"


# ---------------------------------------------------------------------------
# TUI: API key section presence in SettingsModal source
# ---------------------------------------------------------------------------


class TestSettingsModalApiKeyUI:
    def _src(self):
        import src.ui.textual_app_impl as m
        import inspect

        return inspect.getsource(m)

    def test_apikey_section_in_compose(self):
        src_text = self._src()
        assert "apikey_section" in src_text, "API key section container must exist"
        assert "key_input" in src_text, "API key Input widget must exist"

    def test_save_and_cancel_buttons(self):
        src_text = self._src()
        assert "btn_save_key" in src_text, "Save key button must exist"
        assert "btn_cancel_key" in src_text, "Cancel key button must exist"

    def test_input_is_password_masked(self):
        src_text = self._src()
        assert "password=True" in src_text, (
            "API key input must use password=True for masking"
        )

    def test_hint_mentions_prefs_path(self):
        src_text = self._src()
        assert "prefs.json" in src_text, "Modal must show where the key is stored"

    def test_save_calls_controller_save_api_key(self):
        src_text = self._src()
        assert "save_api_key" in src_text, (
            "_save_api_key must call controller.save_api_key()"
        )

    def test_visibility_toggled_on_provider_change(self):
        src_text = self._src()
        assert "_update_apikey_visibility" in src_text, (
            "API key section visibility must update when provider changes"
        )

    def test_needs_api_key_delegates_to_controller(self):
        src_text = self._src()
        assert "provider_requires_api_key" in src_text, (
            "Modal must ask controller whether provider needs a key"
        )
