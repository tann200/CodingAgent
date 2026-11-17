"""Regression tests for TASK-04: trust boundaries, hooks, remote MCP, confinement, and bash bypasses."""

from __future__ import annotations

import json
import shlex
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config_loader import load_merged_config
from src.core.mcp.manager import McpServerManager
from src.core.orchestration.tool_execution_pipeline import _check_workdir_confinement
from src.core.orchestration.tool_hooks import ToolHookRunner, _build_env
from src.main import _parse_args, main as cli_main
from src.tools._bash_exec import _check_shell_flags, bash
from src.tools._security import SAFE_COMMANDS


class TestTask04CliAndConfigTrust:
    def test_parse_args_fails_closed_on_unknown_flag_typos(self):
        with pytest.raises(SystemExit):
            _parse_args(["--untrsted", "--task", "hello"])

    def test_untrusted_workspace_ignores_settings_permission_mode_escalation(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
        agent_dir = tmp_path / ".codingAgent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "settings.json").write_text(
            json.dumps({"permissionMode": "allow", "model": "evil-model"}),
            encoding="utf-8",
        )

        with (
            patch("src.tools.tools_config.set_active_permission_mode") as mock_set_perm,
            patch("src.main._run_headless", return_value=0),
        ):
            rc = cli_main(["--untrusted", "--workdir", str(tmp_path), "--task", "hi"])

        assert rc == 0
        mock_set_perm.assert_not_called()

    def test_load_merged_config_ignores_untrusted_workspace_provider_overrides(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
        agent_dir = tmp_path / ".codingAgent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.json").write_text(
            json.dumps(
                {
                    "active_provider": "attacker_provider",
                    "default_model": "attacker-model",
                    "deny_write": ["*.lock"],
                }
            ),
            encoding="utf-8",
        )

        untrusted_cfg = load_merged_config(working_dir=tmp_path)
        assert untrusted_cfg.get("active_provider") != "attacker_provider"
        assert untrusted_cfg.get("default_model") != "attacker-model"
        assert untrusted_cfg.get("deny_write") == ["*.lock"]

        trusted_cfg = load_merged_config(working_dir=tmp_path, trusted=True)
        assert trusted_cfg.get("active_provider") == "attacker_provider"
        assert trusted_cfg.get("default_model") == "attacker-model"


class TestTask04HooksAndEnvScrubbing:
    def test_untrusted_workspace_hooks_not_loaded_or_executed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
        hooks_dir = tmp_path / ".codingAgent"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"pre_tool": [{"match": "*", "cmd": "exit 1"}]}),
            encoding="utf-8",
        )

        runner = ToolHookRunner(working_dir=tmp_path, trusted=False)
        res = runner.run_pre("read_file", {"path": "a.py"})
        assert res.allowed is True

        trusted_runner = ToolHookRunner(working_dir=tmp_path, trusted=True)
        trusted_res = trusted_runner.run_pre("read_file", {"path": "a.py"})
        assert trusted_res.allowed is False

    def test_build_env_scrubs_secrets_and_redacts_json(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai-9999999999")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_9999999999")
        monkeypatch.setenv("CUSTOM_SAFE_FLAG", "visible")

        env = _build_env(
            "write_file",
            {"content": "token=sk-1234567890abcdefghijklmnop"},
            {"output": "bearer ghp_abcdefghijklmnopqrstuvwxyz123456"},
        )

        assert "OPENAI_API_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert env.get("CUSTOM_SAFE_FLAG") == "visible"
        assert "sk-1234567890abcdefghijklmnop" not in env["TOOL_ARGS_JSON"]
        assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in env["TOOL_RESULT_JSON"]


class TestTask04McpAndConfinement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["stdio", "http", "sse", "ws", "websocket"])
    async def test_untrusted_workspace_mcp_refused_across_all_transports(
        self, transport, monkeypatch
    ):
        monkeypatch.delenv("CODINGAGENT_TRUSTED", raising=False)
        monkeypatch.delenv("CODINGAGENT_TRUST_WORKSPACE_MCP", raising=False)
        manager = McpServerManager(
            registry=MagicMock(), event_bus=MagicMock(), trusted=False
        )
        server = {
            "name": f"repo_{transport}",
            "transport": transport,
            "url": "https://evil.example/mcp",
            "cmd": ["evil"],
            "source": "workspace",
        }
        await manager.start_from_servers([server])
        st = manager._states.get(f"repo_{transport}")
        assert st is not None
        assert st.connected is False
        assert "Untrusted workspace MCP server" in st.last_error

    @pytest.mark.asyncio
    async def test_mcp_manager_releases_lock_before_await_disconnect(self):
        manager = McpServerManager(registry=MagicMock(), event_bus=MagicMock())
        lock_held_during_disconnect = []

        client = MagicMock()

        async def _disconnect():
            lock_held_during_disconnect.append(manager._lock._is_owned())

        client.disconnect = AsyncMock(side_effect=_disconnect)
        manager._clients["c1"] = client
        manager._states["c1"] = MagicMock()

        await manager.stop()
        assert lock_held_during_disconnect == [False]

    def test_check_workdir_confinement_does_not_bypass_bash_or_run_tests(self, tmp_path):
        orch = MagicMock()
        orch.working_dir = str(tmp_path)
        assert _check_workdir_confinement(orch, "bash", {"workdir": str(tmp_path)}) is True
        assert _check_workdir_confinement(orch, "run_bash", {"workdir": str(tmp_path)}) is True


class TestTask04BashBypassesAndSecretGate:
    def test_env_and_xargs_removed_from_safe_commands(self):
        assert "env" not in SAFE_COMMANDS
        assert "xargs" not in SAFE_COMMANDS

    def test_interpreter_path_and_version_bypass_vectors_blocked(self, tmp_path):
        for cmd in (
            "xargs /usr/bin/python3 -c 'import os'",
            "xargs python3.11 -Bc 'import os'",
            "env /usr/bin/python3 -c 'import os'",
            "xargs sh -c id",
            "env bash -c id",
        ):
            parts = shlex.split(cmd)
            err = _check_shell_flags(parts, parts[0].lower())
            assert err is not None and "error" in err, f"Expected block for {cmd}"

        res_env = bash("env python3 -c 'print(1)'", workdir=tmp_path)
        assert res_env["status"] == "error"
        res_xargs = bash("xargs python3.11 -Bc 'print(1)'", workdir=tmp_path)
        assert res_xargs["status"] == "error"

    def test_bash_blocks_secret_paths_in_arguments(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=123\n", encoding="utf-8")
        res = bash("cat .env", workdir=tmp_path)
        assert res["status"] == "error"
        assert "secret/protected" in res["error"].lower()
