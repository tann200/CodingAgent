"""Tests for the canonical project-config reader (P4.4).

``config_loader.load_project_config`` is the single parser for the workspace
``.codingAgent/config.json`` file.  The OP-5 helper family
(``get_project_tool_overrides`` / ``get_project_deny_write_patterns``) had been
lost from the module, leaving ``task_lifecycle`` importing a name that did not
exist (silently swallowed by a broad ``except``).  These tests pin the restored
behaviour and its consumer.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.core import config_loader


@pytest.fixture(autouse=True)
def _clear_project_config_cache():
    config_loader._PROJECT_CONFIG_CACHE.clear()
    yield
    config_loader._PROJECT_CONFIG_CACHE.clear()


def _write_project_config(workdir, payload: dict) -> None:
    ctx = workdir / ".codingAgent"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "config.json").write_text(json.dumps(payload), encoding="utf-8")


class TestLoadProjectConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        assert config_loader.load_project_config(str(tmp_path)) == {}

    def test_reads_canonical_codingagent_config(self, tmp_path):
        _write_project_config(tmp_path, {"tools": {"bash": False}})
        cfg = config_loader.load_project_config(str(tmp_path))
        assert cfg == {"tools": {"bash": False}}

    def test_corrupt_json_returns_empty(self, tmp_path):
        ctx = tmp_path / ".codingAgent"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "config.json").write_text("{not json", encoding="utf-8")
        assert config_loader.load_project_config(str(tmp_path)) == {}


class TestGetProjectToolOverrides:
    def test_reads_tools_key(self, tmp_path):
        _write_project_config(tmp_path, {"tools": {"bash": False, "glob": True}})
        overrides = config_loader.get_project_tool_overrides(str(tmp_path))
        assert overrides == {"bash": False, "glob": True}

    def test_missing_returns_empty(self, tmp_path):
        assert config_loader.get_project_tool_overrides(str(tmp_path)) == {}

    def test_non_dict_tools_returns_empty(self, tmp_path):
        _write_project_config(tmp_path, {"tools": ["bash"]})
        assert config_loader.get_project_tool_overrides(str(tmp_path)) == {}


class TestGetProjectDenyWritePatterns:
    def test_top_level_deny_write(self, tmp_path):
        _write_project_config(tmp_path, {"deny_write": ["*.env", "db/*"]})
        result = config_loader.get_project_deny_write_patterns(str(tmp_path))
        assert result == ["*.env", "db/*"]

    def test_nested_permissions_deny_write(self, tmp_path):
        _write_project_config(
            tmp_path, {"permissions": {"deny_write": ["secrets/*"]}}
        )
        result = config_loader.get_project_deny_write_patterns(str(tmp_path))
        assert result == ["secrets/*"]

    def test_both_schemas_are_unioned(self, tmp_path):
        _write_project_config(
            tmp_path,
            {"deny_write": ["a"], "permissions": {"deny_write": ["b"]}},
        )
        result = config_loader.get_project_deny_write_patterns(str(tmp_path))
        assert result == ["a", "b"]

    def test_missing_returns_empty(self, tmp_path):
        assert config_loader.get_project_deny_write_patterns(str(tmp_path)) == []


class TestFileIoDelegatesToCanonical:
    def test_file_io_reads_top_level_deny_write(self, tmp_path):
        from src.tools import _file_io

        _write_project_config(tmp_path, {"deny_write": ["*.key"]})
        assert _file_io.get_project_deny_write_patterns(str(tmp_path)) == ["*.key"]


class TestStartNewTaskAppliesToolOverrides:
    def _orch(self, workdir):
        orch = MagicMock()
        orch._current_task_id = "abc12345"
        orch._session_read_files = set()
        orch._session_modified_files = set()
        orch._execution_trace_buffer = []
        orch._current_snapshot_id = None
        orch._step_snapshot_id = None
        orch._plan_approval_event = None
        orch._plan_approved = False
        orch._pending_delegations = []
        orch._session_title = "old"
        orch._agent_mode = "planning"
        orch.working_dir = workdir
        orch.plan_mode = None
        orch.msg_mgr = MagicMock()
        orch.msg_mgr.messages = ["old"]
        orch.tool_registry = MagicMock()
        return orch

    def test_disabled_tool_is_deregistered(self, tmp_path):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        _write_project_config(tmp_path, {"tools": {"bash": False, "glob": True}})
        orch = self._orch(tmp_path)
        start_new_task_impl(orch)
        orch.tool_registry.deregister.assert_called_once_with("bash")

    def test_enabled_tools_are_not_deregistered(self, tmp_path):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        _write_project_config(tmp_path, {"tools": {"glob": True}})
        orch = self._orch(tmp_path)
        start_new_task_impl(orch)
        orch.tool_registry.deregister.assert_not_called()
