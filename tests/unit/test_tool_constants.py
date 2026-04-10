"""Phase A tests: tool_constants.py — shared tool-classification sets and audit helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.orchestration.tool_constants import (
    WRITE_TOOLS_REQUIRING_READ,
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    _write_permission_audit,
)


class TestWriteToolsRequiringRead:
    def test_contains_core_write_tools(self):
        for tool in ("edit_file", "write_file", "edit_by_line_range", "apply_patch"):
            assert tool in WRITE_TOOLS_REQUIRING_READ

    def test_edit_file_atomic_present(self):
        assert "edit_file_atomic" in WRITE_TOOLS_REQUIRING_READ

    def test_destructive_tools_present(self):
        assert "delete_file" in WRITE_TOOLS_REQUIRING_READ
        assert "rename_file" in WRITE_TOOLS_REQUIRING_READ
        assert "ast_rename" in WRITE_TOOLS_REQUIRING_READ

    def test_read_file_absent(self):
        assert "read_file" not in WRITE_TOOLS_REQUIRING_READ

    def test_is_frozenset(self):
        assert isinstance(WRITE_TOOLS_REQUIRING_READ, frozenset)


class TestDryRunBlockedTools:
    def test_is_superset_of_write_tools(self):
        assert WRITE_TOOLS_REQUIRING_READ <= DRY_RUN_BLOCKED_TOOLS

    def test_bash_tools_present(self):
        for tool in ("bash", "run_bash", "execute_bash"):
            assert tool in DRY_RUN_BLOCKED_TOOLS

    def test_git_destructive_present(self):
        assert "git_commit" in DRY_RUN_BLOCKED_TOOLS
        assert "git_push" in DRY_RUN_BLOCKED_TOOLS

    def test_is_frozenset(self):
        assert isinstance(DRY_RUN_BLOCKED_TOOLS, frozenset)


class TestPermissionRequiredTools:
    def test_delete_file_present(self):
        assert "delete_file" in PERMISSION_REQUIRED_TOOLS

    def test_run_bash_present(self):
        assert "run_bash" in PERMISSION_REQUIRED_TOOLS

    def test_is_frozenset(self):
        assert isinstance(PERMISSION_REQUIRED_TOOLS, frozenset)


class TestWritePermissionAudit:
    def test_creates_audit_file(self, tmp_path):
        _write_permission_audit(str(tmp_path), "read_file", {}, "allow")
        audit = tmp_path / ".agent" / "permission_audit.jsonl"
        assert audit.exists()

    def test_entry_is_valid_json(self, tmp_path):
        _write_permission_audit(str(tmp_path), "write_file", {}, "deny", "blocked")
        audit = tmp_path / ".agent" / "permission_audit.jsonl"
        entry = json.loads(audit.read_text().strip())
        assert entry["tool"] == "write_file"
        assert entry["decision"] == "deny"
        assert entry["reason"] == "blocked"
        assert "ts" in entry

    def test_appends_multiple_entries(self, tmp_path):
        _write_permission_audit(str(tmp_path), "tool_a", {}, "allow")
        _write_permission_audit(str(tmp_path), "tool_b", {}, "deny")
        lines = (
            (tmp_path / ".agent" / "permission_audit.jsonl").read_text().splitlines()
        )
        assert len(lines) == 2

    def test_no_exception_on_bad_path(self):
        # Should silently swallow errors rather than raising
        _write_permission_audit(
            "/nonexistent/path/that/cannot/be/created", "x", {}, "allow"
        )


class TestBackwardCompatReexport:
    """Constants remain importable from orchestrator for existing callers."""

    def test_reexported_from_orchestrator(self):
        from src.core.orchestration import orchestrator as orch

        assert hasattr(orch, "WRITE_TOOLS_REQUIRING_READ")
        assert hasattr(orch, "DRY_RUN_BLOCKED_TOOLS")
        assert hasattr(orch, "PERMISSION_REQUIRED_TOOLS")
        assert hasattr(orch, "_write_permission_audit")

    def test_same_objects(self):
        from src.core.orchestration import orchestrator as orch

        assert orch.WRITE_TOOLS_REQUIRING_READ is WRITE_TOOLS_REQUIRING_READ
        assert orch.DRY_RUN_BLOCKED_TOOLS is DRY_RUN_BLOCKED_TOOLS
        assert orch.PERMISSION_REQUIRED_TOOLS is PERMISSION_REQUIRED_TOOLS
