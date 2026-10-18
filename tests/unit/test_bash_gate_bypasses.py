"""Unit tests for SPEC-09: Shell Gate 3 combined-flag bypasses, git allowlist abuse, and arbitrary PID probing."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from src.tools._bash_exec import (
    _SPAWNED_BG_PIDS,
    bash,
    bash_readonly,
    check_background_task,
)


@pytest.fixture(autouse=True)
def clean_spawned_pids():
    _SPAWNED_BG_PIDS.clear()
    yield
    _SPAWNED_BG_PIDS.clear()


class TestGate3CombinedFlags:
    """Gate 3 / 4 must block combined inline execution flags across interpreters."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c 'print(1)'",
            "python3 -Bc 'print(1)'",
            "python3 -Ic 'print(1)'",
            "python3 -uc 'print(1)'",
            "python -c 'print(1)'",
            "python3 -",
            "node -e 'console.log(1)'",
            "node -e'console.log(1)'",
            "node --eval='console.log(1)'",
            "node --eval 'console.log(1)'",
            "ruby -e 'puts 1'",
            "ruby -e'puts 1'",
            "php -r 'echo 1'",
        ],
    )
    def test_combined_code_exec_flags_blocked_in_bash(self, cmd: str, tmp_path: Path):
        res = bash(cmd, workdir=tmp_path)
        assert res["status"] == "error"
        assert "inline code execution flags are blocked" in res["error"]

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -Bc 'print(1)'",
            "node --eval='1+1'",
            "node -e'1+1'",
            "php -r 'echo 1'",
        ],
    )
    def test_combined_code_exec_flags_blocked_in_bash_readonly(
        self, cmd: str, tmp_path: Path
    ):
        res = bash_readonly(cmd, workdir=tmp_path)
        assert res["status"] == "error"
        assert (
            "inline code execution flags are blocked" in res["error"]
            or "not allowed in read-only mode" in res["error"]
        )


class TestGitSubcommandSafety:
    """Git flag injection and mutating subcommands must be caught."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c core.pager=id log",
            "git -c core.pager='sh -c id' status",
            "git --exec-path=/tmp status",
            "git --git-dir=/etc/.git log",
            "git --work-tree=/ status",
            "git --upload-pack='id' fetch",
            "git --receive-pack='id' push",
        ],
    )
    def test_dangerous_git_flags_blocked(self, cmd: str, tmp_path: Path):
        # In both bash and bash_readonly, dangerous flags must be blocked with an error
        res_bash = bash(cmd, workdir=tmp_path)
        assert res_bash["status"] == "error"
        assert "Dangerous git flag" in res_bash["error"]

        res_ro = bash_readonly(cmd, workdir=tmp_path)
        assert res_ro["status"] == "error"
        assert "Dangerous git flag" in res_ro["error"]

    def test_git_branch_mutation_requires_approval(self, tmp_path: Path):
        # git branch new_branch mutates branches -> requires approval in bash
        res = bash("git branch new_branch", workdir=tmp_path)
        assert res["status"] == "error"
        assert res.get("requires_approval") is True
        assert "creates or modifies branches" in res["error"]

        # in bash_readonly, mutating branch is hard-blocked
        res_ro = bash_readonly("git branch new_branch", workdir=tmp_path)
        assert res_ro["status"] == "error"
        assert "creates or modifies branches" in res_ro["error"]

        # git branch -D is also blocked by security analysis
        res_d = bash("git branch -D main", workdir=tmp_path)
        assert res_d["status"] == "error"

    def test_git_branch_listing_is_allowed(self, tmp_path: Path):
        # Plain git branch or git branch -a is read-only
        res = bash("git branch", workdir=tmp_path)
        # Should not be blocked by gate (OS error or git error is fine)
        if res.get("status") == "error":
            assert not res.get("requires_approval")
            assert "Dangerous git flag" not in res.get("error", "")

    def test_bash_readonly_denylist_and_quote_stripping(self, tmp_path: Path):
        # nc is blocked in bash_readonly
        res_nc = bash_readonly("nc -l 1234", workdir=tmp_path)
        assert res_nc["status"] == "error"
        assert (
            "blocked by the CodingAgent tool policy" in res_nc["error"]
            or "Command blocked by security analysis" in res_nc["error"]
        )

        # quote stripping in Gate 1
        res_rm = bash_readonly("rm '-rf' /", workdir=tmp_path)
        assert res_rm["status"] == "error"
        assert "dangerous pattern" in res_rm["error"]


class TestBackgroundTaskOriginCheck:
    """check_background_task must reject unspawned PIDs."""

    def test_untracked_pid_rejected(self, tmp_path: Path):
        # PID 1 is system init — probing it must be blocked
        res = check_background_task("1", workdir=tmp_path)
        assert res["status"] == "error"
        assert "Unknown or untracked background task ID" in res["error"]

    def test_arbitrary_pid_rejected(self, tmp_path: Path):
        res = check_background_task(str(os.getpid()), workdir=tmp_path)
        assert res["status"] == "error"
        assert "Unknown or untracked background task ID" in res["error"]

    def test_spawned_pid_allowed(self, tmp_path: Path):
        script = tmp_path / "spin.py"
        script.write_text("import time; time.sleep(2)\n", encoding="utf-8")

        res = bash(f"python3 {script}", workdir=tmp_path, run_in_background=True)
        assert res["status"] == "ok"
        bg_id = res["background_task_id"]

        # Should be tracked in _SPAWNED_BG_PIDS
        assert int(bg_id) in _SPAWNED_BG_PIDS

        poll_res = check_background_task(bg_id, workdir=tmp_path)
        assert poll_res["status"] == "ok"
        assert poll_res["running"] is True
        assert poll_res["pid"] == int(bg_id)
