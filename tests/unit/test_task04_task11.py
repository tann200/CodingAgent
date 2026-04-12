"""tests/unit/test_task04_task11.py — Regression tests for TASK-04 and TASK-11."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# TASK-04 — credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_get_api_key_returns_none_when_not_set(self, tmp_path, monkeypatch):
        """get_api_key returns None when the provider key is absent."""
        import src.core.credentials as creds

        monkeypatch.setattr(creds, "_PREFS_PATH", tmp_path / "prefs.json")
        # Disable keyring
        monkeypatch.setattr(creds, "_keyring_get", lambda _: None)
        assert creds.get_api_key("nonexistent_provider") is None

    def test_set_and_get_via_prefs_fallback(self, tmp_path, monkeypatch):
        """set_api_key falls back to prefs.json when keyring unavailable."""
        import src.core.credentials as creds

        monkeypatch.setattr(creds, "_PREFS_PATH", tmp_path / "prefs.json")
        monkeypatch.setattr(
            creds, "_keyring_set", lambda p, k: False
        )  # keyring unavailable
        monkeypatch.setattr(creds, "_keyring_get", lambda _: None)  # keyring empty

        creds.set_api_key("testprovider", "sk-test-key")
        result = creds.get_api_key("testprovider")
        assert result == "sk-test-key"

    def test_keyring_takes_priority(self, tmp_path, monkeypatch):
        """Keyring result is used when available, prefs.json ignored."""
        import src.core.credentials as creds

        monkeypatch.setattr(creds, "_PREFS_PATH", tmp_path / "prefs.json")
        monkeypatch.setattr(
            creds, "_keyring_get", lambda p: "keychain-value" if p == "myprov" else None
        )
        monkeypatch.setattr(creds, "_prefs_get", lambda _: "prefs-value")

        result = creds.get_api_key("myprov")
        assert result == "keychain-value"

    def test_delete_removes_from_prefs(self, tmp_path, monkeypatch):
        """delete_api_key removes the key from prefs.json."""
        import src.core.credentials as creds

        monkeypatch.setattr(creds, "_PREFS_PATH", tmp_path / "prefs.json")
        monkeypatch.setattr(creds, "_keyring_set", lambda p, k: False)
        monkeypatch.setattr(creds, "_keyring_get", lambda _: None)
        monkeypatch.setattr(creds, "_keyring_delete", lambda _: False)

        creds.set_api_key("delprov", "some-key")
        assert creds.get_api_key("delprov") == "some-key"

        creds.delete_api_key("delprov")
        assert creds.get_api_key("delprov") is None

    def test_empty_key_not_stored(self, tmp_path, monkeypatch):
        """set_api_key refuses to store an empty string."""
        import src.core.credentials as creds

        monkeypatch.setattr(creds, "_PREFS_PATH", tmp_path / "prefs.json")
        monkeypatch.setattr(creds, "_keyring_set", lambda p, k: False)
        monkeypatch.setattr(creds, "_keyring_get", lambda _: None)

        creds.set_api_key("provider", "")
        assert creds.get_api_key("provider") is None

    def test_set_via_keyring(self, monkeypatch):
        """set_api_key uses keyring when available."""
        import src.core.credentials as creds

        stored = {}
        monkeypatch.setattr(
            creds, "_keyring_set", lambda p, k: stored.update({p: k}) or True
        )
        monkeypatch.setattr(creds, "_keyring_get", lambda p: stored.get(p))

        creds.set_api_key("keyprov", "my-secret")
        assert stored.get("keyprov") == "my-secret"


# ---------------------------------------------------------------------------
# TASK-11 — deferred_init
# ---------------------------------------------------------------------------


class TestDeferredInit:
    def test_untrusted_returns_all_false(self):
        """Untrusted workspace: nothing is started."""
        from src.core.orchestration.deferred_init import run_deferred_init

        result = asyncio.run(run_deferred_init(None, trusted=False))
        assert result.mcp_started is False
        assert result.hooks_enabled is False
        assert result.plugins_loaded == 0

    def test_trusted_no_subsystems_is_ok(self):
        """Trusted but orchestrator has no subsystems: no crash."""
        from src.core.orchestration.deferred_init import run_deferred_init

        mock_orch = MagicMock(spec=[])  # no attributes
        result = asyncio.run(run_deferred_init(mock_orch, trusted=True))
        # Should complete without error even with a bare mock
        assert isinstance(result.errors, list)

    def test_trusted_with_hooks_enables_them(self):
        """Trusted with tool_hooks attribute: hooks_enabled=True."""
        from src.core.orchestration.deferred_init import run_deferred_init

        mock_orch = MagicMock()
        mock_orch.tool_hooks = MagicMock()
        mock_orch.mcp_server = None
        mock_orch.plugin_dir = None

        result = asyncio.run(run_deferred_init(mock_orch, trusted=True))
        assert result.hooks_enabled is True

    def test_trusted_with_mcp_server_starts_it(self):
        """Trusted with mcp_server: calls start() and sets mcp_started."""
        from src.core.orchestration.deferred_init import run_deferred_init

        started = []

        async def _start():
            started.append(True)

        mock_orch = MagicMock()
        mock_orch.mcp_server = MagicMock()
        mock_orch.mcp_server.start = _start
        mock_orch.tool_hooks = None
        mock_orch.plugin_dir = None

        result = asyncio.run(run_deferred_init(mock_orch, trusted=True))
        assert result.mcp_started is True
        assert started == [True]

    def test_trusted_with_mcp_manager_starts_it(self):
        """Trusted with mcp_manager: calls start() and sets mcp_started."""
        from src.core.orchestration.deferred_init import run_deferred_init

        started = []

        async def _start_manager():
            started.append(True)

        mock_orch = MagicMock()
        mock_orch.mcp_manager = MagicMock()
        mock_orch.mcp_manager.start = _start_manager
        mock_orch.mcp_server = None
        mock_orch.tool_hooks = None
        mock_orch.plugin_dir = None

        result = asyncio.run(run_deferred_init(mock_orch, trusted=True))
        assert result.mcp_started is True
        assert started == [True]

    def test_is_trusted_env_var(self, monkeypatch):
        """CODINGAGENT_TRUSTED=1 marks workspace as trusted."""
        from src.core.orchestration.deferred_init import is_trusted

        monkeypatch.setenv("CODINGAGENT_TRUSTED", "1")
        assert is_trusted(None) is True

    def test_is_trusted_env_var_false(self, monkeypatch):
        """CODINGAGENT_TRUSTED not set: untrusted by default."""
        from src.core.orchestration.deferred_init import is_trusted

        monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
        assert is_trusted(None) is False

    def test_orchestrator_trusted_attr_overrides_env(self, monkeypatch):
        """orchestrator.trusted attribute takes priority over env var."""
        from src.core.orchestration.deferred_init import is_trusted

        monkeypatch.setenv("CODINGAGENT_TRUSTED", "0")
        mock_orch = MagicMock()
        mock_orch.trusted = True
        assert is_trusted(mock_orch) is True

    def test_mcp_start_error_non_fatal(self):
        """MCP server start error is captured in result.errors, not raised."""
        from src.core.orchestration.deferred_init import run_deferred_init

        async def _bad_start():
            raise RuntimeError("MCP failed")

        mock_orch = MagicMock()
        mock_orch.mcp_server = MagicMock()
        mock_orch.mcp_server.start = _bad_start
        mock_orch.tool_hooks = None
        mock_orch.plugin_dir = None

        result = asyncio.run(run_deferred_init(mock_orch, trusted=True))
        assert result.mcp_started is False
        assert len(result.errors) >= 1

    def test_plugin_loading_from_empty_dir(self, tmp_path):
        """Plugin dir with no .py files: plugins_loaded=0."""
        from src.core.orchestration.deferred_init import run_deferred_init

        mock_orch = MagicMock()
        mock_orch.mcp_server = None
        mock_orch.tool_hooks = None
        mock_orch.plugin_dir = str(tmp_path)

        result = asyncio.run(run_deferred_init(mock_orch, trusted=True))
        assert result.plugins_loaded == 0
