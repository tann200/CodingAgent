"""Phase A tests: tool_constants.py — shared tool-classification sets and audit helper."""

# ruff: noqa: E501

from __future__ import annotations

import json


from src.core.orchestration.tool_constants import (
    WRITE_TOOLS_REQUIRING_READ,
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    _write_permission_audit,
)


class TestWriteToolsRequiringRead:
    # NOTE: Reduced from 6 tests to 2 - individual membership tests removed
    
    def test_contains_critical_write_tools(self):
        """Verify critical write tools are present."""
        critical_tools = {
            "edit_file", "write_file", "edit_by_line_range", "apply_patch",
            "edit_file_atomic", "delete_file", "rename_file", "ast_rename"
        }
        assert critical_tools <= WRITE_TOOLS_REQUIRING_READ

    def test_is_frozenset(self):
        assert isinstance(WRITE_TOOLS_REQUIRING_READ, frozenset)


class TestDryRunBlockedTools:
    # NOTE: Reduced from 4 tests to 2 - individual membership tests consolidated
    
    def test_contains_critical_blocked_tools(self):
        """Verify critical dry-run-blocked tools are present."""
        # Must include all write tools
        assert WRITE_TOOLS_REQUIRING_READ <= DRY_RUN_BLOCKED_TOOLS
        
        # Plus bash and git tools
        critical_tools = {
            "bash", "run_bash", "execute_bash",
            "git_commit", "git_push"
        }
        assert critical_tools <= DRY_RUN_BLOCKED_TOOLS

    def test_is_frozenset(self):
        assert isinstance(DRY_RUN_BLOCKED_TOOLS, frozenset)


class TestPermissionRequiredTools:
    # NOTE: Reduced from 3 tests to 2 - individual membership tests consolidated
    
    def test_contains_critical_permission_tools(self):
        """Verify critical permission-required tools are present."""
        critical_tools = {"delete_file", "run_bash"}
        assert critical_tools <= PERMISSION_REQUIRED_TOOLS

    def test_is_frozenset(self):
        assert isinstance(PERMISSION_REQUIRED_TOOLS, frozenset)


class TestWritePermissionAudit:
    def test_creates_audit_file(self, tmp_path):
        _write_permission_audit(str(tmp_path), "read_file", {}, "allow")
        audit = tmp_path / ".codingAgent" / "permission_audit.jsonl"
        assert audit.exists()

    def test_entry_is_valid_json(self, tmp_path):
        _write_permission_audit(str(tmp_path), "write_file", {}, "deny", "blocked")
        audit = tmp_path / ".codingAgent" / "permission_audit.jsonl"
        entry = json.loads(audit.read_text().strip())
        assert entry["tool"] == "write_file"
        assert entry["decision"] == "deny"
        assert entry["reason"] == "blocked"
        assert "ts" in entry

    def test_appends_multiple_entries(self, tmp_path):
        _write_permission_audit(str(tmp_path), "tool_a", {}, "allow")
        _write_permission_audit(str(tmp_path), "tool_b", {}, "deny")
        lines = (
            (tmp_path / ".codingAgent" / "permission_audit.jsonl")
            .read_text()
            .splitlines()
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
