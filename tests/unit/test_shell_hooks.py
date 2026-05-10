"""Unit tests for src/core/orchestration/shell_hooks.py (CP-7).

Tests mirror the scenarios from hooks.rs in claw-code:
- exit 0  → allow
- exit 2  → deny
- other   → warn (allow, log message)
- missing settings → no hooks executed
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration.shell_hooks import (
    HookResult,
    ShellHookRunner,
    _build_payload,
    _extract_tool_output,
    _load_hooks_config,
    _run_commands,
    _run_one_command,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_settings(tmpdir: Path, pre_cmds: list[str], post_cmds: list[str]) -> Path:
    """Write a .codingAgent/settings.json with the given hook commands."""
    agent_dir = tmpdir / ".codingAgent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    settings = {"hooks": {"PreToolUse": pre_cmds, "PostToolUse": post_cmds}}
    settings_path = agent_dir / "settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    return settings_path


# On Windows, use `cmd /C` snippets; on POSIX use `sh -c` snippets.
if sys.platform == "win32":
    _EXIT0 = "exit 0"
    _EXIT2 = "exit 2"
    _EXIT1 = "exit 1"
    _PRINT_OK = "echo ok"
    _PRINT_DENY = "echo denied && exit 2"
    _PRINT_WARN = "echo warn && exit 1"
else:
    _EXIT0 = "exit 0"
    _EXIT2 = "exit 2"
    _EXIT1 = "exit 1"
    _PRINT_OK = "printf ok"
    _PRINT_DENY = "printf 'blocked by hook'; exit 2"
    _PRINT_WARN = "printf 'hook warning'; exit 1"


# ---------------------------------------------------------------------------
# _load_hooks_config
# ---------------------------------------------------------------------------


class TestLoadHooksConfig:
    def test_returns_empty_when_no_settings_file(self, tmp_path):
        config = _load_hooks_config(tmp_path)
        assert config == {"PreToolUse": [], "PostToolUse": []}

    def test_loads_pre_and_post_commands(self, tmp_path):
        # TASK-8c: _load_hooks_config now returns normalised dicts with matcher/command.
        _write_settings(tmp_path, ["cmd_a"], ["cmd_b"])
        config = _load_hooks_config(tmp_path)
        assert config["PreToolUse"] == [{"matcher": "*", "command": "cmd_a"}]
        assert config["PostToolUse"] == [{"matcher": "*", "command": "cmd_b"}]

    def test_missing_hooks_key_returns_empty(self, tmp_path):
        agent_dir = tmp_path / ".codingAgent"
        agent_dir.mkdir()
        (agent_dir / "settings.json").write_text('{"other": 1}')
        config = _load_hooks_config(tmp_path)
        assert config == {"PreToolUse": [], "PostToolUse": []}

    def test_malformed_json_returns_empty(self, tmp_path):
        agent_dir = tmp_path / ".codingAgent"
        agent_dir.mkdir()
        (agent_dir / "settings.json").write_text("not-json")
        config = _load_hooks_config(tmp_path)
        assert config == {"PreToolUse": [], "PostToolUse": []}

    def test_hooks_not_dict_returns_empty(self, tmp_path):
        agent_dir = tmp_path / ".codingAgent"
        agent_dir.mkdir()
        (agent_dir / "settings.json").write_text('{"hooks": "invalid"}')
        config = _load_hooks_config(tmp_path)
        assert config == {"PreToolUse": [], "PostToolUse": []}


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_contains_required_fields(self):
        payload = _build_payload(
            "PreToolUse", "read_file", {"path": "foo.py"}, None, False
        )
        data = json.loads(payload)
        assert data["hook_event_name"] == "PreToolUse"
        assert data["tool_name"] == "read_file"
        assert data["tool_input"] == {"path": "foo.py"}
        assert data["tool_result_is_error"] is False

    def test_tool_input_json_is_string(self):
        payload = _build_payload("PostToolUse", "run", {"cmd": "ls"}, "output", True)
        data = json.loads(payload)
        assert isinstance(data["tool_input_json"], str)
        assert data["tool_output"] == "output"
        assert data["tool_result_is_error"] is True


# ---------------------------------------------------------------------------
# _run_one_command (integration — actual subprocess)
# ---------------------------------------------------------------------------


class TestRunOneCommand:
    def test_exit_zero_allows(self):
        outcome, msg = _run_one_command(
            _EXIT0, "PreToolUse", "read_file", {}, None, False, "{}"
        )
        assert outcome == "allow"

    def test_exit_two_denies(self):
        outcome, msg = _run_one_command(
            _EXIT2, "PreToolUse", "write_file", {}, None, False, "{}"
        )
        assert outcome == "deny"

    def test_exit_one_warns(self):
        outcome, msg = _run_one_command(
            _EXIT1, "PreToolUse", "delete", {}, None, False, "{}"
        )
        assert outcome == "warn"
        assert msg is not None
        assert "allowing tool execution to continue" in msg

    def test_captures_stdout_on_allow(self):
        outcome, msg = _run_one_command(
            _PRINT_OK, "PreToolUse", "read_file", {}, None, False, "{}"
        )
        assert outcome == "allow"
        assert msg is not None
        assert "ok" in msg

    def test_captures_stdout_on_deny(self):
        outcome, msg = _run_one_command(
            _PRINT_DENY, "PreToolUse", "bash", {}, None, False, "{}"
        )
        assert outcome == "deny"
        assert msg is not None

    def test_invalid_command_warns(self):
        outcome, msg = _run_one_command(
            "__nonexistent_command__", "PreToolUse", "tool", {}, None, False, "{}"
        )
        assert outcome == "warn"


# ---------------------------------------------------------------------------
# _run_commands
# ---------------------------------------------------------------------------


class TestRunCommands:
    def test_empty_commands_returns_allow(self):
        result = _run_commands("PreToolUse", [], "tool", {}, None, False)
        assert result.denied is False
        assert result.messages == []

    def test_all_zero_returns_allow(self):
        result = _run_commands("PreToolUse", [_EXIT0, _EXIT0], "tool", {}, None, False)
        assert result.denied is False

    def test_deny_stops_at_first_deny(self):
        """Once one command exits 2, the rest should not be executed."""
        # Exit 2 followed by exit 0 — result should be denied
        result = _run_commands("PreToolUse", [_EXIT2, _EXIT0], "tool", {}, None, False)
        assert result.denied is True

    def test_warn_continues(self):
        result = _run_commands("PreToolUse", [_EXIT1, _EXIT0], "tool", {}, None, False)
        assert result.denied is False

    def test_messages_collected(self):
        result = _run_commands("PreToolUse", [_PRINT_OK], "tool", {}, None, False)
        assert any("ok" in m for m in result.messages)


# ---------------------------------------------------------------------------
# ShellHookRunner — no hooks configured
# ---------------------------------------------------------------------------


class TestShellHookRunnerNoHooks:
    def test_run_pre_allows_when_no_settings(self, tmp_path):
        runner = ShellHookRunner(workdir=tmp_path)
        result = runner.run_pre("read_file", {"path": "foo.py"})
        assert result.denied is False

    def test_run_post_allows_when_no_settings(self, tmp_path):
        runner = ShellHookRunner(workdir=tmp_path)
        result = runner.run_post("read_file", {}, {"ok": True, "output": "data"})
        assert result.denied is False

    @pytest.mark.asyncio
    async def test_async_run_post_allows_when_no_settings(self, tmp_path):
        runner = ShellHookRunner(workdir=tmp_path)
        result = await runner.async_run_post("read_file", {}, {"ok": True})
        assert result.denied is False


# ---------------------------------------------------------------------------
# ShellHookRunner — with real hooks
# ---------------------------------------------------------------------------


class TestShellHookRunnerWithHooks:
    def test_pre_hook_allow(self, tmp_path):
        _write_settings(tmp_path, [_EXIT0], [])
        runner = ShellHookRunner(workdir=tmp_path)
        result = runner.run_pre("read_file", {"path": "foo.py"})
        assert result.denied is False

    def test_pre_hook_deny(self, tmp_path):
        _write_settings(tmp_path, [_EXIT2], [])
        runner = ShellHookRunner(workdir=tmp_path)
        result = runner.run_pre("bash", {"command": "rm -rf /"})
        assert result.denied is True

    def test_post_hook_deny(self, tmp_path):
        _write_settings(tmp_path, [], [_EXIT2])
        runner = ShellHookRunner(workdir=tmp_path)
        result = runner.run_post("bash", {}, {"ok": True, "output": "done"})
        assert result.denied is True

    def test_post_hook_allow(self, tmp_path):
        _write_settings(tmp_path, [], [_EXIT0])
        runner = ShellHookRunner(workdir=tmp_path)
        result = runner.run_post("read_file", {}, {"ok": True})
        assert result.denied is False

    def test_reload_clears_cache(self, tmp_path):
        _write_settings(tmp_path, [], [])
        runner = ShellHookRunner(workdir=tmp_path)
        # Prime cache
        _ = runner.run_pre("tool", {})
        # Now add a pre-hook command
        _write_settings(tmp_path, [_EXIT2], [])
        # Without reload, old (empty) config is used
        result_before = runner.run_pre("tool", {})
        # After reload, new config is picked up
        runner.reload()
        result_after = runner.run_pre("tool", {})
        assert result_before.denied is False
        assert result_after.denied is True

    @pytest.mark.asyncio
    async def test_async_run_post_deny(self, tmp_path):
        _write_settings(tmp_path, [], [_EXIT2])
        runner = ShellHookRunner(workdir=tmp_path)
        result = await runner.async_run_post("bash", {}, {"ok": True, "output": "x"})
        assert result.denied is True

    def test_event_bus_receives_hook_message(self, tmp_path):
        _write_settings(tmp_path, [_PRINT_OK], [])
        mock_bus = MagicMock()
        runner = ShellHookRunner(workdir=tmp_path, event_bus=mock_bus)
        runner.run_pre("read_file", {})
        mock_bus.publish.assert_called_once()
        call_args = mock_bus.publish.call_args
        assert call_args[0][0] == "hook.message"


# ---------------------------------------------------------------------------
# HookResult
# ---------------------------------------------------------------------------


class TestHookResult:
    def test_allow_factory(self):
        r = HookResult.allow(["msg"])
        assert r.denied is False
        assert r.messages == ["msg"]

    def test_deny_factory(self):
        r = HookResult.deny(["blocked"])
        assert r.denied is True
        assert r.messages == ["blocked"]

    def test_allow_default_empty_messages(self):
        r = HookResult.allow()
        assert r.messages == []


# ---------------------------------------------------------------------------
# _extract_tool_output
# ---------------------------------------------------------------------------


class TestExtractToolOutput:
    def test_prefers_output_key(self):
        assert "hello" in _extract_tool_output({"output": "hello", "result": "x"})

    def test_falls_back_to_result(self):
        assert "res" in _extract_tool_output({"result": "res"})

    def test_falls_back_to_json_dump(self):
        out = _extract_tool_output({"ok": True})
        assert "ok" in out

    def test_truncated_at_4096(self):
        out = _extract_tool_output({"output": "x" * 10000})
        assert len(out) <= 4096


# ---------------------------------------------------------------------------
# ToolExecutionService integration — pre-hook deny wires through
# ---------------------------------------------------------------------------


class TestToolExecutionServiceHookIntegration:
    """Verify that ToolExecutionService.pre_execute() respects hook deny."""

    @pytest.mark.asyncio
    async def test_pre_execute_blocks_on_hook_deny(self, tmp_path):
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        _write_settings(tmp_path, [_EXIT2], [])
        runner = ShellHookRunner(workdir=tmp_path)
        svc = ToolExecutionService(
            registry=MagicMock(),
            event_bus=MagicMock(),
            hook_runner=runner,
        )
        # Patch out all the earlier checks so only the hook is tested
        with (
            patch.object(
                svc, "_check_permission_gate", return_value=MagicMock(blocked=False)
            ),
            patch.object(
                svc.__class__,
                "_check_permission_mode",
                staticmethod(lambda n: MagicMock(blocked=False)),
            ),
            patch.object(
                svc.__class__,
                "_check_explore_mode",
                staticmethod(lambda n, o: MagicMock(blocked=False)),
            ),
            patch.object(
                svc.__class__,
                "_check_plan_mode",
                staticmethod(lambda n, o: MagicMock(blocked=False)),
            ),
        ):
            verdict = await svc.pre_execute("bash", {"command": "rm -rf /"})

        assert verdict.blocked is True
        assert verdict.result is not None
        assert "denied" in verdict.result.get(
            "error", ""
        ).lower() or not verdict.result.get("ok", True)

    @pytest.mark.asyncio
    async def test_post_execute_async_marks_error_on_deny(self, tmp_path):
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        _write_settings(tmp_path, [], [_EXIT2])
        runner = ShellHookRunner(workdir=tmp_path)
        svc = ToolExecutionService(
            registry=MagicMock(),
            event_bus=MagicMock(),
            hook_runner=runner,
        )
        result: dict[str, Any] = {"ok": True, "output": "data"}
        await svc.post_execute_async("bash", {}, result)
        assert result.get("ok") is False
        assert result.get("is_error") is True
