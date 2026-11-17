"""Regression tests for TASK-06: sandbox process-group termination, fail-closed fallbacks, and env scrubbing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration.tool_execution_pipeline import _run_sandbox_and_snapshot
from src.tools import lint_dispatch, sandbox as sbox, verification_tools


class TestTask06SandboxProcessGroupsAndEnv:
    def test_sandbox_level_off_rejected_when_enforcement_or_autonomous_active(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sbox, "_REQUIRE_ENFORCEMENT", True)
        with pytest.raises(RuntimeError, match="sandbox_level='off' is forbidden"):
            sbox.run_sandboxed(["echo", "hi"], cwd=tmp_path, sandbox_level="off")

        monkeypatch.setattr(sbox, "_REQUIRE_ENFORCEMENT", False)
        with patch.object(sbox, "_autonomous_mode", return_value=True):
            with pytest.raises(RuntimeError, match="sandbox_level='off' is forbidden"):
                sbox.run_sandboxed(["echo", "hi"], cwd=tmp_path, sandbox_level="off")

    def test_default_env_scrubs_secrets_and_explicit_empty_env_stays_empty(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret-token")
        monkeypatch.setenv("MY_DB_PASSWORD", "pw-secret")
        monkeypatch.setenv("VISIBLE_FLAG", "hello")
        monkeypatch.setenv("CODINGAGENT_SANDBOX_ALLOW_UNENFORCED", "1")
        monkeypatch.setattr(sbox, "_bwrap_available", lambda: False)
        monkeypatch.setattr(sbox, "_sandbox_exec_available", lambda: False)

        res_default = sbox.run_sandboxed(
            [
                sys.executable,
                "-c",
                "import os, json; print(json.dumps(dict(os.environ)))",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert res_default.returncode == 0
        assert "OPENAI_API_KEY" not in res_default.stdout
        assert "GITHUB_TOKEN" not in res_default.stdout
        assert "MY_DB_PASSWORD" not in res_default.stdout
        assert "VISIBLE_FLAG" in res_default.stdout

        res_empty = sbox.run_sandboxed(
            [
                sys.executable,
                "-c",
                "import os, json; print(json.dumps(dict(os.environ)))",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={},
        )
        assert res_empty.returncode == 0
        assert "VISIBLE_FLAG" not in res_empty.stdout

    def test_proc_group_kills_on_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODINGAGENT_SANDBOX_ALLOW_UNENFORCED", "1")
        monkeypatch.setattr(sbox, "_bwrap_available", lambda: False)
        monkeypatch.setattr(sbox, "_sandbox_exec_available", lambda: False)

        with patch("os.killpg") as mock_killpg:
            with pytest.raises(subprocess.TimeoutExpired):
                sbox.run_sandboxed(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    cwd=tmp_path,
                    timeout=0.05,
                    capture_output=True,
                )
        if os.name == "posix":
            mock_killpg.assert_called()

    def test_bwrap_binds_resolved_extra_dir_and_sbpl_network_true(self, tmp_path):
        sub = tmp_path / "real_sub"
        sub.mkdir()
        link = tmp_path / "link_sub"
        link.symlink_to(sub)

        args = sbox._build_bwrap_args(
            tmp_path, "workspace", extra_writable_dirs=[str(link)]
        )
        resolved_str = str(sub.resolve())
        assert resolved_str in args
        assert str(link) not in args

        profile_net = sbox._build_sandbox_exc_profile(
            tmp_path, "workspace", network=True
        )
        assert "(deny network*)" not in profile_net
        profile_no_net = sbox._build_sandbox_exc_profile(
            tmp_path, "workspace", network=False
        )
        assert "(deny network*)" in profile_no_net


class TestTask06NoUnsandboxedFallbacks:
    def test_lint_dispatch_fails_closed_without_unsandboxed_fallback(self, tmp_path):
        with patch(
            "src.tools.sandbox.run_sandboxed",
            side_effect=RuntimeError("sandbox blocked"),
        ):
            with pytest.raises(RuntimeError, match="sandbox blocked"):
                lint_dispatch._run_lint_cmd(["echo", "x"], cwd=tmp_path, timeout=5)

    def test_verification_tools_fails_closed_without_unsandboxed_fallback(
        self, tmp_path
    ):
        with patch(
            "src.tools.sandbox.run_sandboxed",
            side_effect=RuntimeError("sandbox blocked"),
        ):
            with pytest.raises(RuntimeError, match="sandbox blocked"):
                verification_tools._run_subprocess(["echo", "x"], cwd=tmp_path)

    def test_pipeline_js_syntax_check_routes_through_run_sandboxed(self, tmp_path):
        orch = MagicMock()
        orch.working_dir = str(tmp_path)
        tool = {"name": "write_file", "side_effects": ["write"]}
        args = {"path": str(tmp_path / "ok.js"), "content": "const x = 1;\n"}

        calls = []

        def _fake_sandboxed(cmd, **kwargs):
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = "v20.0.0"
            m.stderr = ""
            return m

        with patch("src.tools.sandbox.run_sandboxed", side_effect=_fake_sandboxed):
            err = _run_sandbox_and_snapshot(
                orch, "write_file", args, tool, str(tmp_path / "ok.js")
            )
        assert err is None
        assert any("node" in c[0] for c in calls)
