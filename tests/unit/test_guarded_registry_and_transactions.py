"""Unit tests for guarded ToolRegistry execution and transactional mutations.

Phase 2 Item 1:
- Route every tool invocation through guarded execution.
- Make all mutations transactional (including non-existent files and directories).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.orchestration.orchestrator import Orchestrator
from src.core.orchestration.rollback_manager import RollbackManager
from src.tools._registry import ToolRegistry, build_registry
from src.tools.rollback_tools import revert_last_tool


class TestGuardedToolRegistryCall:
    """Tests verifying ToolRegistry.call routes through guarded execution."""

    def test_bound_registry_enforces_read_before_write(self, tmp_path: Path):
        """ToolRegistry.call enforces read-before-write when bound to an orchestrator."""
        file_path = tmp_path / "target.py"
        file_path.write_text("initial = True\n")

        reg = build_registry(working_dir=str(tmp_path), include_echo=True)
        orch = Orchestrator(adapter=None, tool_registry=reg, working_dir=str(tmp_path), allow_external_working_dir=True)

        # Direct write without reading should be blocked by read-before-write guard
        result = reg.call("write_file", path="target.py", content="initial = False\n")
        assert isinstance(result, dict)
        assert result.get("ok") is False or result.get("status") == "error"
        assert "read" in (result.get("error") or "").lower()

        # Read the file first
        read_res = reg.call("read_file", path="target.py")
        assert read_res.get("status") == "ok"

        # Now write should succeed
        write_res = reg.call("write_file", path="target.py", content="initial = False\n")
        assert write_res.get("status") == "ok"
        assert file_path.read_text() == "initial = False\n"

        # Verify trace was recorded in orchestrator
        traces = [t["tool"] for t in getattr(orch, "_execution_trace_buffer", [])]
        assert "read_file" in traces
        assert "write_file" in traces

    def test_standalone_registry_enforces_read_before_write(self, tmp_path: Path):
        """Standalone ToolRegistry.call (no prior orchestrator) lazily creates guarded orchestrator."""
        file_path = tmp_path / "standalone.py"
        file_path.write_text("a = 1\n")

        reg = build_registry(working_dir=str(tmp_path), include_echo=True)

        # Direct write without reading should be blocked
        result = reg.call("write_file", path="standalone.py", content="a = 2\n")
        assert isinstance(result, dict)
        assert result.get("ok") is False or result.get("status") == "error"
        assert "read" in (result.get("error") or "").lower()

        # Read the file first
        reg.call("read_file", path="standalone.py")

        # Now write should succeed
        write_res = reg.call("write_file", path="standalone.py", content="a = 2\n")
        assert write_res.get("status") == "ok"
        assert file_path.read_text() == "a = 2\n"

    def test_registry_blocks_secret_file_access(self, tmp_path: Path):
        """Guarded execution blocks reading protected secret files."""
        secret_file = tmp_path / ".env"
        secret_file.write_text("API_KEY=super_secret_12345\n")

        reg = build_registry(working_dir=str(tmp_path))
        orch = Orchestrator(adapter=None, tool_registry=reg, working_dir=str(tmp_path), allow_external_working_dir=True)

        result = reg.call("read_file", path=".env")
        assert isinstance(result, dict)
        assert result.get("status") == "error"
        assert "protected secret" in (result.get("error") or "").lower()

    def test_orchestration_tool_registry_has_call_method(self, tmp_path: Path):
        """src.core.orchestration.tool_registry.ToolRegistry also supports guarded call()."""
        from src.core.orchestration.tool_registry import ToolRegistry as CoreTR

        core_reg = CoreTR()
        core_reg.register("sample", lambda x: {"val": x * 2})
        orch = Orchestrator(adapter=None, tool_registry=core_reg, working_dir=str(tmp_path), allow_external_working_dir=True)

        res = core_reg.call("sample", x=21)
        assert res == {"val": 42}


class TestTransactionalMutations:
    """Tests verifying rollback works for all mutations including newly created files/directories."""

    def test_rollback_deletes_newly_created_file(self, tmp_path: Path):
        """Rolling back a write_file that created a brand new file unlinks the new file."""
        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)

        new_file = tmp_path / "brand_new.txt"
        assert not new_file.exists()

        # Write new file (does not require prior read since it doesn't exist yet)
        res = orch.execute_tool(
            {"name": "write_file", "arguments": {"path": "brand_new.txt", "content": "created!"}}
        )
        assert res.get("ok") is True
        assert new_file.exists()
        assert new_file.read_text() == "created!"

        # Rollback via revert_last_tool
        revert_res = orch.execute_tool({"name": "revert_last_tool", "arguments": {}})
        assert revert_res.get("ok") is True
        assert not new_file.exists(), "Newly created file must be unlinked on rollback"

    def test_rollback_deletes_newly_created_directory(self, tmp_path: Path):
        """Rolling back create_directory deletes the newly created directory."""
        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)

        new_dir = tmp_path / "subdir_alpha"
        assert not new_dir.exists()

        res = orch.execute_tool(
            {"name": "create_directory", "arguments": {"path": "subdir_alpha"}}
        )
        assert res.get("ok") is True
        assert new_dir.exists() and new_dir.is_dir()

        revert_res = orch.execute_tool({"name": "revert_last_tool", "arguments": {}})
        assert revert_res.get("ok") is True
        assert not new_dir.exists(), "Newly created directory must be removed on rollback"

    def test_rollback_rename_file_restores_source_and_unlinks_destination(self, tmp_path: Path):
        """Rolling back rename_file restores the source file and deletes the destination file."""
        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)

        src_file = tmp_path / "source.txt"
        dst_file = tmp_path / "dest.txt"
        src_file.write_text("vital data\n")

        # Read source file first so read-before-write guard passes for destructive rename
        orch.execute_tool({"name": "read_file", "arguments": {"path": "source.txt"}})

        # Execute rename
        res = orch.execute_tool(
            {"name": "rename_file", "arguments": {"path": "source.txt", "new_path": "dest.txt"}}
        )
        assert res.get("ok") is True
        assert not src_file.exists()
        assert dst_file.exists()
        assert dst_file.read_text() == "vital data\n"

        # Revert rename
        revert_res = orch.execute_tool({"name": "revert_last_tool", "arguments": {}})
        assert revert_res.get("ok") is True

        assert src_file.exists(), "Source file must be restored"
        assert src_file.read_text() == "vital data\n"
        assert not dst_file.exists(), "Destination file must be removed"

    def test_atomic_step_transaction_with_create_and_modify(self, tmp_path: Path):
        """Multi-file atomic step rollback handles existing modified files and newly created files."""
        orch = Orchestrator(working_dir=str(tmp_path), allow_external_working_dir=True)

        existing = tmp_path / "existing.py"
        existing.write_text("x = 10\n")
        new_file = tmp_path / "new_module.py"

        # Read existing file
        orch.execute_tool({"name": "read_file", "arguments": {"path": "existing.py"}})

        # Begin step transaction
        step_id = orch.begin_step_transaction()

        # Modify existing file
        orch.execute_tool(
            {"name": "write_file", "arguments": {"path": "existing.py", "content": "x = 99\n"}}
        )
        # Create new file
        orch.execute_tool(
            {"name": "write_file", "arguments": {"path": "new_module.py", "content": "y = 42\n"}}
        )

        assert existing.read_text() == "x = 99\n"
        assert new_file.exists()

        # Rollback the whole step transaction
        rollback_res = orch.rollback_step_transaction()
        assert rollback_res.get("ok") is True

        # Existing restored, new file deleted
        assert existing.read_text() == "x = 10\n"
        assert not new_file.exists()
