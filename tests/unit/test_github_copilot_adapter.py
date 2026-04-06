"""Tests for GitHub Copilot adapter and auth module."""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ── Auth module tests ─────────────────────────────────────────────────────────

from src.core.inference.adapters.github_copilot_auth import (
    DeviceCodeResponse,
    start_device_flow,
    poll_for_token,
    save_token,
    load_token,
    clear_token,
    is_authenticated,
    DeviceCodeExpired,
    AuthCancelled,
    GITHUB_CLIENT_ID,
)
from src.core.inference.adapters.github_copilot_adapter import (
    GithubCopilotAdapter,
    Adapter,
    COPILOT_BASE_URL,
    _DEFAULT_MODELS,
)


class TestStartDeviceFlow:
    def test_returns_device_code_response(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = start_device_flow()
        assert result.device_code == "dev123"
        assert result.user_code == "ABCD-1234"
        assert result.interval == 5
        # client_id sent in request
        body = mock_post.call_args[1]["json"]
        assert body["client_id"] == GITHUB_CLIENT_ID
        assert body["scope"] == "read:user"

    def test_raises_on_missing_field(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"device_code": "x"}  # missing fields
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(ValueError, match="missing field"):
                start_device_flow()

    def test_raises_on_http_error(self):
        import requests as _req

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = _req.HTTPError("401")
        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(_req.HTTPError):
                start_device_flow()


class TestPollForToken:
    def _make_response(self, body: dict) -> MagicMock:
        r = MagicMock()
        r.json.return_value = body
        r.raise_for_status = MagicMock()
        return r

    def test_returns_token_on_success(self):
        responses = [
            self._make_response({"error": "authorization_pending"}),
            self._make_response({"access_token": "ghu_tok123"}),
        ]
        with patch("requests.post", side_effect=responses), patch("time.sleep"):
            token = poll_for_token("dev_code", interval=1, timeout=60)
        assert token == "ghu_tok123"

    def test_slow_down_returns_token_eventually(self):
        """After slow_down the loop continues and eventually returns the token."""
        responses = [
            self._make_response({"error": "slow_down"}),
            self._make_response({"access_token": "ghu_slow_tok"}),
        ]
        with patch("requests.post", side_effect=responses), patch("time.sleep"):
            token = poll_for_token("dev_code", interval=5, timeout=60)
        assert token == "ghu_slow_tok"

    def test_cancel_event_stops_poll(self):
        """Setting cancel_event raises AuthCancelled within the next sleep chunk."""
        import threading as _t

        event = _t.Event()
        event.set()  # already cancelled before polling starts
        with patch("requests.post") as mock_post, patch("time.sleep"):
            with pytest.raises(AuthCancelled):
                poll_for_token("dev_code", interval=1, timeout=60, cancel_event=event)
        mock_post.assert_not_called()  # cancelled before first HTTP request

    def test_expired_token_raises(self):
        with (
            patch(
                "requests.post",
                return_value=self._make_response({"error": "expired_token"}),
            ),
            patch("time.sleep"),
        ):
            with pytest.raises(DeviceCodeExpired):
                poll_for_token("dev_code", interval=1, timeout=60)

    def test_access_denied_raises(self):
        with (
            patch(
                "requests.post",
                return_value=self._make_response({"error": "access_denied"}),
            ),
            patch("time.sleep"),
        ):
            with pytest.raises(AuthCancelled):
                poll_for_token("dev_code", interval=1, timeout=60)

    def test_timeout_raises(self):
        # Use a real short timeout; patch sleep so the loop spins fast
        with (
            patch(
                "requests.post",
                return_value=self._make_response({"error": "authorization_pending"}),
            ),
            patch("time.sleep"),
        ):
            with pytest.raises(TimeoutError):
                poll_for_token("dev_code", interval=0, timeout=0.001)


class TestTokenPersistence:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_test_token")
        assert load_token() == "ghu_test_token"

    def test_clear_token(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_test_token")
        clear_token()
        assert load_token() in (None, "")
        assert not is_authenticated()

    def test_is_authenticated_false_when_no_token(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        assert not is_authenticated()

    def test_is_authenticated_true_when_token_stored(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_abc")
        assert is_authenticated()


# ── Adapter tests ─────────────────────────────────────────────────────────────


class TestGithubCopilotAdapterInit:
    def test_base_url_fixed_to_copilot(self):
        adapter = GithubCopilotAdapter()
        assert adapter.base_url == COPILOT_BASE_URL

    def test_default_models_populated(self):
        adapter = GithubCopilotAdapter()
        assert len(adapter.models) > 0
        assert "gpt-4o" in adapter.models

    def test_custom_models_accepted(self):
        adapter = GithubCopilotAdapter(models=["model-a", "model-b"])
        assert adapter.models == ["model-a", "model-b"]

    def test_adapter_alias(self):
        assert Adapter is GithubCopilotAdapter

    def test_auth_flow_sentinel(self):
        assert GithubCopilotAdapter.AUTH_FLOW == "github_device"

    def test_requires_api_key_false(self):
        assert GithubCopilotAdapter.REQUIRES_API_KEY is False


class TestGithubCopilotAdapterHeaders:
    def test_headers_contain_required_fields(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_header_test")
        adapter = GithubCopilotAdapter()
        headers = adapter._headers()
        assert headers["Authorization"] == "Bearer ghu_header_test"
        assert "Editor-Version" in headers
        assert "Editor-Plugin-Version" in headers
        assert "Copilot-Integration-Id" in headers
        assert headers["Openai-Intent"] == "conversation-edits"
        assert "User-Agent" in headers

    def test_headers_raise_when_not_authenticated(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        # ensure no token
        clear_token()
        adapter = GithubCopilotAdapter()
        with pytest.raises(RuntimeError, match="not authenticated"):
            adapter._headers()

    def test_headers_bearer_uses_stored_token(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("my_special_token")
        adapter = GithubCopilotAdapter()
        assert adapter._headers()["Authorization"] == "Bearer my_special_token"


class TestGithubCopilotAdapterModels:
    def test_get_models_unauthenticated_returns_empty(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        clear_token()
        adapter = GithubCopilotAdapter()
        result = adapter.get_models_from_api()
        assert result == {"models": []}

    def test_get_models_parses_data_array(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_tok")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"id": "gpt-4o", "name": "GPT-4o"},
                {"id": "claude-3.5-sonnet", "name": "Claude 3.5 Sonnet"},
            ]
        }
        with patch("requests.get", return_value=mock_resp):
            result = GithubCopilotAdapter().get_models_from_api()
        names = [m["name"] for m in result["models"]]
        assert "gpt-4o" in names

    def test_get_models_falls_back_to_static_on_empty_response(
        self, tmp_path, monkeypatch
    ):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_tok")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": []}
        with patch("requests.get", return_value=mock_resp):
            result = GithubCopilotAdapter().get_models_from_api()
        assert len(result["models"]) > 0  # fell back to _DEFAULT_MODELS


class TestGithubCopilotAdapterGenerate:
    def test_generate_unauthenticated_returns_error(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        clear_token()
        adapter = GithubCopilotAdapter(models=["gpt-4o"])
        result = adapter.generate([{"role": "user", "content": "hi"}], model="gpt-4o")
        assert result["ok"] is False

    def test_generate_success(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_tok")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model": "gpt-4o",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            adapter = GithubCopilotAdapter(models=["gpt-4o"])
            result = adapter.generate(
                [{"role": "user", "content": "hi"}], model="gpt-4o"
            )
        assert result["ok"] is True
        assert result["choices"][0]["message"]["content"] == "hello"


class TestGithubCopilotValidateConnection:
    def test_validate_connection_false_when_not_authenticated(
        self, tmp_path, monkeypatch
    ):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        clear_token()
        assert GithubCopilotAdapter().validate_connection() is False

    def test_validate_connection_true_when_authenticated(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        save_token("ghu_tok")
        assert GithubCopilotAdapter().validate_connection() is True


# ── ProviderManager integration ───────────────────────────────────────────────


class TestProviderManagerIntegration:
    def test_provider_manager_loads_github_copilot(self, tmp_path):
        providers_json = tmp_path / "providers.json"
        providers_json.write_text(
            json.dumps(
                [
                    {
                        "name": "GitHub Copilot",
                        "type": "github_copilot",
                        "models": ["gpt-4o"],
                        "active": False,
                    }
                ]
            )
        )
        from src.core.inference.llm_manager import ProviderManager
        import asyncio

        pm = ProviderManager(providers_config_path=str(providers_json))
        asyncio.run(pm.initialize())
        assert "github_copilot" in pm.list_providers()
        adapter = pm.get_provider("github_copilot")
        assert isinstance(adapter, GithubCopilotAdapter)

    def test_inactive_copilot_skipped_at_startup(self, tmp_path, monkeypatch):
        prefs_file = tmp_path / "prefs.json"
        monkeypatch.setenv("CODINGAGENT_PREFS", str(prefs_file))
        providers_json = tmp_path / "providers.json"
        providers_json.write_text(
            json.dumps(
                [
                    {
                        "name": "GitHub Copilot",
                        "type": "github_copilot",
                        "models": ["gpt-4o"],
                        "active": False,
                    }
                ]
            )
        )
        from src.core.startup import provider_health_check
        import asyncio
        from src.core.inference.llm_manager import ProviderManager

        pm = ProviderManager(providers_config_path=str(providers_json))
        asyncio.run(pm.initialize())
        # health check should not probe an inactive provider
        results = asyncio.run(
            provider_health_check.__wrapped__(pm)  # type: ignore[attr-defined]
            if hasattr(provider_health_check, "__wrapped__")
            else provider_health_check()
        )
        # github_copilot should either be absent or skipped (not probed)
        if "github_copilot" in results:
            # if present it should not have errored on a network call
            assert results["github_copilot"].get("error") != "no_adapter"


class TestCanonicalProviderAliases:
    def test_copilot_variants_map_to_github_copilot(self):
        from src.core.inference.llm_manager import canonical_provider

        assert canonical_provider("copilot") == "github_copilot"
        assert canonical_provider("GitHub Copilot") == "github_copilot"
        assert canonical_provider("github-copilot") == "github_copilot"
        assert canonical_provider("ghcopilot") == "github_copilot"
        assert canonical_provider("github_copilot") == "github_copilot"


class TestSetProviderActive:
    def test_sets_active_true(self, tmp_path, monkeypatch):
        providers_json = tmp_path / "providers.json"
        providers_json.write_text(
            json.dumps(
                [{"name": "GitHub Copilot", "type": "github_copilot", "active": False}]
            )
        )
        monkeypatch.setattr(
            "src.core.inference.llm_manager.resolve_config_path",
            lambda _: providers_json,
        )
        from src.core.inference.llm_manager import _set_provider_active

        _set_provider_active("github_copilot", active=True)
        data = json.loads(providers_json.read_text())
        assert data[0]["active"] is True

    def test_sets_active_false(self, tmp_path, monkeypatch):
        providers_json = tmp_path / "providers.json"
        providers_json.write_text(
            json.dumps(
                [{"name": "GitHub Copilot", "type": "github_copilot", "active": True}]
            )
        )
        monkeypatch.setattr(
            "src.core.inference.llm_manager.resolve_config_path",
            lambda _: providers_json,
        )
        from src.core.inference.llm_manager import _set_provider_active

        _set_provider_active("github_copilot", active=False)
        data = json.loads(providers_json.read_text())
        assert data[0]["active"] is False

    def test_accepts_canonical_alias(self, tmp_path, monkeypatch):
        providers_json = tmp_path / "providers.json"
        providers_json.write_text(
            json.dumps(
                [{"name": "GitHub Copilot", "type": "github_copilot", "active": False}]
            )
        )
        monkeypatch.setattr(
            "src.core.inference.llm_manager.resolve_config_path",
            lambda _: providers_json,
        )
        from src.core.inference.llm_manager import _set_provider_active

        _set_provider_active("copilot", active=True)  # alias
        data = json.loads(providers_json.read_text())
        assert data[0]["active"] is True


# ── Auth flow detection (migrated from SettingsPanelController — LEGACY-02) ──


class TestSettingsPanelControllerAuthMethods:
    """Migrated from legacy SettingsPanelController auth-method tests.

    The controller shim is removed; tests now exercise adapter attributes and
    the token helpers directly.
    """

    def test_github_copilot_adapter_has_device_auth_flow(self):
        """GithubCopilotAdapter.AUTH_FLOW must be 'github_device'."""
        adapter = GithubCopilotAdapter()
        assert getattr(adapter, "AUTH_FLOW", None) == "github_device"

    def test_adapter_requires_api_key_flag_false_for_github(self):
        """GithubCopilotAdapter does not require a manual API key."""
        adapter = GithubCopilotAdapter()
        assert getattr(adapter, "REQUIRES_API_KEY", True) is False

    def test_is_authenticated_false_when_no_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODINGAGENT_PREFS", str(tmp_path / "prefs.json"))
        clear_token()
        assert not is_authenticated()

    def test_is_authenticated_true_when_token_stored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODINGAGENT_PREFS", str(tmp_path / "prefs.json"))
        save_token("ghu_tok")
        assert is_authenticated()

    def test_clear_token_removes_authentication(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODINGAGENT_PREFS", str(tmp_path / "prefs.json"))
        save_token("ghu_tok")
        clear_token()
        assert not is_authenticated()
