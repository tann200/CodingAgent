"""Regression tests for TASK-07: pre-write guardrails, binary/atomic writes, subagent isolation, SSRF, and binary rollback."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration.remote_skills import _fetch_text, fetch_remote_skills
from src.core.orchestration.rollback_manager import RollbackManager
from src.core.orchestration.tool_execution_pipeline import (
    _check_explore_mode_guard,
    _check_permission_mode_guard,
    _check_plan_mode_guard,
    _check_read_before_write,
    _check_workspace_scope_guard,
)
from src.core.orchestration.workspace_guard import WorkspaceGuard
from src.tools import _file_io, ast_tools, git_tools, patch_tools, web_tools
from src.tools.guardrails import mark_file_read
from src.tools.subagent_payloads import DELEGATION_DENYLIST


class TestTask07PreWriteGuardrailsAndBinary:
    def test_workspace_guard_anchors_protected_filenames(self, tmp_path):
        guard = WorkspaceGuard(workspace_root=tmp_path)
        assert guard.is_protected("setup.py") is True
        assert guard.is_protected("pkg/setup.py") is True
        assert guard.is_protected("my_setup.py") is False

    def test_write_file_and_generate_patch_reject_binary_files(self, tmp_path):
        bin_file = tmp_path / "logo.png"
        bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
        mark_file_read(str(bin_file.resolve()))

        res_write = _file_io.write_file("logo.png", "overwrite", workdir=tmp_path)
        assert res_write["status"] == "error"
        assert "binary" in res_write["error"].lower()

        res_patch = patch_tools.generate_patch("logo.png", "new", workdir=tmp_path)
        assert res_patch["status"] == "error"
        assert "binary" in res_patch["error"].lower()

    def test_create_directory_uses_workdir_guard_and_deny_write(self, tmp_path):
        cfg_dir = tmp_path / ".codingAgent"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"deny_write": ["locked/*"]}), encoding="utf-8"
        )
        res = _file_io.create_directory("locked/sub", workdir=tmp_path)
        assert res["status"] == "error"
        assert "deny_write" in res["error"]

    def test_ast_rename_enforces_deny_write_permission_error_cleanly(self, tmp_path):
        cfg_dir = tmp_path / ".codingAgent"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"deny_write": ["frozen.py"]}), encoding="utf-8"
        )
        target = tmp_path / "frozen.py"
        target.write_text("old_var = 1\n", encoding="utf-8")
        mark_file_read(str(target.resolve()))

        res = ast_tools.ast_rename("frozen.py", "old_var", "new_var", workdir=str(tmp_path))
        assert res["status"] == "error"
        assert "deny_write" in res["error"]
        assert target.read_text(encoding="utf-8") == "old_var = 1\n"

    def test_git_restore_inserts_double_dash_separator(self, tmp_path):
        with patch("src.tools.git_tools._run_git", return_value={"status": "ok", "output": ""}) as mock_git:
            res = git_tools.git_restore("--staged-injection.py", workdir=tmp_path)
        assert res["status"] == "ok"
        args = mock_git.call_args[0][0]
        assert "--" in args
        assert args.index("--") < args.index("--staged-injection.py")

    def test_pipeline_security_gates_fail_closed_on_internal_error(self, tmp_path):
        orch = MagicMock()
        orch.working_dir = str(tmp_path)
        orch._affected_files = ["a.py"]
        orch.explore_mode = True
        orch.plan_mode = MagicMock(enabled=True)
        orch._plan_mode_approved = False

        with patch("src.tools.guardrails._get_ctx_read_files", side_effect=RuntimeError("lock broken")):
            res_rbw = _check_read_before_write(
                orch, "write_file", {"path": "a.py"}, frozenset({"write_file"})
            )
            assert res_rbw is not None and res_rbw["ok"] is False

        with patch.object(Path, "resolve", side_effect=RuntimeError("resolve broken")):
            res_scope = _check_workspace_scope_guard(
                orch, "write_file", {"path": "a.py"}, frozenset({"write_file"})
            )
            assert res_scope is not None and res_scope["ok"] is False

        class _BrokenPM:
            @property
            def enabled(self):
                raise RuntimeError("plan broken")

        orch.plan_mode = _BrokenPM()
        res_plan = _check_plan_mode_guard(orch, "write_file")
        assert res_plan is not None and res_plan["ok"] is False

        with patch(
            "src.core.orchestration.role_config.is_tool_allowed_for_role",
            side_effect=RuntimeError("role broken"),
        ):
            res_explore = _check_explore_mode_guard(orch, "write_file")
            assert res_explore is not None and res_explore["ok"] is False

        with patch(
            "src.tools.tools_config.get_active_permission_mode",
            side_effect=RuntimeError("perm broken"),
        ):
            res_perm = _check_permission_mode_guard(orch, "write_file")
            assert res_perm is not None and res_perm["ok"] is False


class TestTask07SubagentsAndSsrfAndBinaryRollback:
    def test_delegation_denylist_includes_delegate_alias(self):
        assert "delegate" in DELEGATION_DENYLIST
        assert "delegate_task" in DELEGATION_DENYLIST

    def test_delegate_task_does_not_mutate_parent_active_agent(self, tmp_path):
        from src.tools import subagent_tools

        sentinel_parent = MagicMock()
        sentinel_parent.working_dir = str(tmp_path)
        sentinel_parent.active_agent = None
        sentinel_parent.adapter = MagicMock()
        sentinel_parent.adapter.model = "mock-model"
        sentinel_parent.tool_registry = MagicMock()

        def _fake_graph(*args, **kwargs):
            g = MagicMock()
            g.ainvoke = None

            async def _ainvoke(state, config=None):
                # Parent orchestrator's active_agent must remain untouched!
                assert sentinel_parent.active_agent is None
                return {"history": [{"role": "assistant", "content": "done"}], "errors": []}

            g.ainvoke = _ainvoke
            return g

        token = subagent_tools._PARENT_ORCHESTRATOR_VAR.set(sentinel_parent)
        try:
            with patch(
                "src.tools.subagent_tools._resolve_subagent_graph",
                side_effect=_fake_graph,
            ):
                out = subagent_tools.delegate_task(
                    role="reviewer",
                    subtask_description="Check code",
                    allowed_tools=["read_file"],
                )
        finally:
            subagent_tools._PARENT_ORCHESTRATOR_VAR.reset(token)

        assert sentinel_parent.active_agent is None
        assert "done" in str(out).lower() or isinstance(out, str)

    def test_ssrf_blocks_octal_hex_ips_and_dns_gaierror(self, monkeypatch):
        assert web_tools._is_ip_blocked("0177.0.0.1") is True
        assert web_tools._is_ip_blocked("0x7f.0.0.1") is True
        assert web_tools._is_ip_blocked("0x7f000001") is True

        def _fail_dns(host, *args, **kwargs):
            raise socket.gaierror("DNS failed")

        monkeypatch.setattr(web_tools.socket, "getaddrinfo", _fail_dns)
        assert web_tools._is_url_blocked("https://nonexistent.invalid/") is True
        with pytest.raises(PermissionError, match="DNS resolution failed"):
            web_tools._is_ssrf_blocked("https://nonexistent.invalid/")

    def test_ssrf_allowlist_blocks_bare_wildcard_and_single_tld(self, monkeypatch):
        monkeypatch.setattr(
            web_tools.socket,
            "getaddrinfo",
            lambda h, *a, **k: [(0, 0, 0, "", ("127.0.0.1", 0))],
        )
        with patch(
            "src.core.config_loader.load_merged_config",
            return_value={"web": {"ssrf_allowlist": ["*", "com"]}},
        ):
            with pytest.raises(PermissionError):
                web_tools._is_ssrf_blocked("https://evil.com/")

    def test_remote_skills_validates_redirects_and_sanitizes_skill_names(
        self, tmp_path, monkeypatch
    ):
        # Redirect to loopback must be blocked cleanly
        monkeypatch.setattr(
            web_tools.socket,
            "getaddrinfo",
            lambda host, *a, **k: [
                (0, 0, 0, "", ("127.0.0.1" if "127.0.0.1" in host else "93.184.216.34", 0))
            ],
        )
        redir_resp = MagicMock()
        redir_resp.status_code = 302
        redir_resp.headers = {"Location": "http://127.0.0.1/secret"}
        redir_resp.raw = None

        with patch("requests.get", return_value=redir_resp) as mock_get:
            assert _fetch_text("https://example.com/index.json") is None
            assert mock_get.call_args.kwargs.get("allow_redirects") is False

        # Traversal skill name must stay inside cache_dir
        monkeypatch.setattr(
            "src.core.orchestration.remote_skills._cache_dir_for_url",
            lambda u: tmp_path / "cache",
        )
        index_json = json.dumps([{"name": "../../escape", "file": "a.md"}])
        with patch(
            "src.core.orchestration.remote_skills._fetch_text",
            side_effect=[index_json, "# Skill body"],
        ):
            skills = fetch_remote_skills("https://example.com/skills/")
        assert skills
        assert not (tmp_path / "escape.md").exists()

    def test_rollback_manager_preserves_binary_bytes(self, tmp_path):
        bin_path = tmp_path / "asset.bin"
        original_bytes = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe\x80\x90"
        bin_path.write_bytes(original_bytes)

        rm = RollbackManager(str(tmp_path))
        snap_id = rm.snapshot_files(["asset.bin"])
        bin_path.write_bytes(b"CORRUPTED")

        # Force reload from JSON on disk to verify persistence format too
        rm.snapshots.clear()
        res = rm.rollback(snap_id)
        assert res["ok"] is True
        assert bin_path.read_bytes() == original_bytes
