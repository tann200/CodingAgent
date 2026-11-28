"""Regression tests for REM-03: shell newline differential, GTFOBins, bg sandbox, fail-closed pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration.pipeline.permissions import _run_permission_gate
from src.core.orchestration.pipeline.preflight import _run_preflight_and_lookup
from src.tools import _bash_exec
from src.tools._bash_safety import _check_git_command_safety, _check_shell_flags
from src.tools._security import SAFE_COMMANDS
from src.tools.bash_security import split_shell_segments
from src.tools.permission_context import ToolPermissionContext


class TestShellParserAndGTFOBins:
    def test_unquoted_newline_splits_as_operator_and_is_blocked_in_bash(self, tmp_path: Path) -> None:
        segs, ops = split_shell_segments("echo ok | cat\nwhoami")
        assert "\n" in ops
        assert segs == [["echo", "ok"], ["cat"], ["whoami"]]

        res = _bash_exec.bash("echo ok | cat\nwhoami", workdir=tmp_path)
        assert res["status"] == "error"
        assert "dangerous pattern" in res["error"].lower()

    def test_quoted_newline_stays_inside_token(self) -> None:
        segs, ops = split_shell_segments('echo "hello\nworld"')
        assert ops == []
        assert segs == [["echo", "hello\nworld"]]

    def test_gtfobins_hardening_in_safe_commands_and_flags(self, tmp_path: Path) -> None:
        assert "awk" not in SAFE_COMMANDS
        assert "zip" not in SAFE_COMMANDS

        # find -okdir / -delete / -exec rm / -exec python3.11 -Bc blocked
        assert _check_shell_flags(["find", ".", "-delete"], "find") is not None
        assert _check_shell_flags(["find", ".", "-okdir", "sh", "-c", "id", ";"], "find") is not None
        assert _check_shell_flags(["find", ".", "-exec", "rm", "{}", "+"], "find") is not None
        assert _check_shell_flags(["find", ".", "-exec", "python3.11", "-Bc", "import os", ";"], "find") is not None

        # bash_readonly blocks find -exec / -delete
        ro_res = _bash_exec.bash_readonly("find . -exec cat {} +", workdir=tmp_path)
        assert ro_res["status"] == "error"

        # tar old-style xf/cf & --checkpoint-action blocked
        assert _check_shell_flags(["tar", "xf", "a.tar"], "tar") is not None
        assert _check_shell_flags(["tar", "cf", "a.tar", "."], "tar") is not None
        assert _check_shell_flags(["tar", "-tf", "a.tar", "--checkpoint-action=exec=sh"], "tar") is not None
        assert _check_shell_flags(["tar", "-tf", "a.tar"], "tar") is None

        # GNU sed e / w commands blocked while benign sed -n passes
        assert _check_shell_flags(["sed", "s/a/b/e", "f.txt"], "sed") is not None
        assert _check_shell_flags(["sed", "w /tmp/out", "f.txt"], "sed") is not None
        assert _check_shell_flags(["sed", "-n", "1,5p", "f.txt"], "sed") is None

        # git attached -c and --output / --ext-diff blocked
        ok1, _ = _check_git_command_safety(["git", "-ccore.pager=evil", "status"])
        assert ok1 is False
        ok2, _ = _check_git_command_safety(["git", "diff", "--output=pwn.txt"])
        assert ok2 is False
        ok3, _ = _check_git_command_safety(["git", "diff", "--ext-diff"])
        assert ok3 is False

    def test_background_bash_does_not_double_append_cmd_parts_and_scrubs_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-1234567890")
        captured: dict = {}

        class _FakeProc:
            pid = 424242

            def poll(self):
                return None

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(kwargs.get("env") or {})
            captured["start_new_session"] = kwargs.get("start_new_session")
            return _FakeProc()

        with (
            patch.object(
                _bash_exec,
                "_build_background_sandbox",
                return_value=["/usr/bin/sandbox-exec", "-f", "/tmp/test.sb", "echo", "hi"],
            ),
            patch("subprocess.Popen", side_effect=_fake_popen),
        ):
            res = _bash_exec.bash("echo hi", workdir=tmp_path, run_in_background=True)

        assert res["status"] == "ok"
        assert captured["cmd"] == ["/usr/bin/sandbox-exec", "-f", "/tmp/test.sb", "echo", "hi"]
        assert captured["start_new_session"] is True
        assert "OPENAI_API_KEY" not in captured["env"]
        assert _bash_exec._SPAWNED_BG_PROCS[424242][1] == "/tmp/test.sb"
        _bash_exec._SPAWNED_BG_PIDS.discard(424242)
        _bash_exec._SPAWNED_BG_PROCS.pop(424242, None)


class TestFailClosedPipelineGates:
    def test_preflight_exception_fails_closed(self) -> None:
        orch = MagicMock()
        orch.preflight_check.side_effect = RuntimeError("boom")
        err, tool = _run_preflight_and_lookup(orch, "bash", {}, {"name": "bash"}, "tid1")
        assert tool is None
        assert err is not None and err["ok"] is False

    def test_missing_gate_event_fails_closed(self) -> None:
        orch = MagicMock()
        with (
            patch("src.core.orchestration.pipeline.permissions.register_tool_gate", return_value=None),
            patch("src.core.orchestration.tool_execution_pipeline.register_tool_gate", return_value=None),
        ):
            err = _run_permission_gate(orch, "bash", {"command": "ls"}, "tid2", needs_gate=True)
        assert err is not None
        assert err["ok"] is False

    def test_permission_context_filter_registry_fails_closed_on_error(self) -> None:
        ctx = ToolPermissionContext(deny_names=frozenset({"bash"}))

        class _BrokenRegistry:
            def list(self):
                raise RuntimeError("corrupt registry")

        with pytest.raises(RuntimeError, match="failed closed"):
            ctx.filter_registry(_BrokenRegistry())
