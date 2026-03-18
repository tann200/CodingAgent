"""
Tests for RollbackManager.
"""

import pytest
import shutil
from pathlib import Path

from src.core.orchestration.rollback_manager import (
    RollbackManager,
    create_rollback_manager,
    FileSnapshot,
)
from src.core.orchestration.orchestrator import Orchestrator


class TestRollbackManager:
    """Tests for RollbackManager."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir."""
        workdir = tmp_path / "rollback_test"
        workdir.mkdir()
        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_rollback_manager_creation(self, temp_workdir):
        """Test creating a RollbackManager."""
        mgr = RollbackManager(temp_workdir)

        assert mgr.workdir == Path(temp_workdir)
        assert mgr.snapshot_dir.exists()

    def test_create_rollback_manager_factory(self, temp_workdir):
        """Test factory function."""
        mgr = create_rollback_manager(temp_workdir)

        assert isinstance(mgr, RollbackManager)

    def test_snapshot_files(self, temp_workdir):
        """Test taking a snapshot of files."""
        # Create test file
        test_file = Path(temp_workdir) / "test.py"
        test_file.write_text("original content")

        mgr = RollbackManager(temp_workdir)
        snapshot_id = mgr.snapshot_files(["test.py"])

        assert snapshot_id is not None
        assert snapshot_id in mgr.snapshots
        assert len(mgr.snapshots[snapshot_id]) == 1

    def test_snapshot_nonexistent_file(self, temp_workdir):
        """Test snapshot with nonexistent file."""
        mgr = RollbackManager(temp_workdir)
        snapshot_id = mgr.snapshot_files(["nonexistent.py"])

        # Should not fail, just skip
        assert snapshot_id is not None
        assert len(mgr.snapshots[snapshot_id]) == 0

    def test_rollback_files(self, temp_workdir):
        """Test rolling back files."""
        # Create and snapshot test file
        test_file = Path(temp_workdir) / "test.py"
        test_file.write_text("original content")

        mgr = RollbackManager(temp_workdir)
        snapshot_id = mgr.snapshot_files(["test.py"])

        # Modify the file
        test_file.write_text("modified content")

        # Rollback
        result = mgr.rollback(snapshot_id)

        assert result["ok"] is True
        assert test_file.read_text() == "original content"

    def test_list_snapshots(self, temp_workdir):
        """Test listing snapshots."""
        # Create snapshots
        test_file = Path(temp_workdir) / "test.py"
        test_file.write_text("content")

        mgr = RollbackManager(temp_workdir)
        mgr.snapshot_files(["test.py"], "snapshot_1")
        mgr.snapshot_files(["test.py"], "snapshot_2")

        snapshots = mgr.list_snapshots()

        assert len(snapshots) >= 2

    def test_delete_snapshot(self, temp_workdir):
        """Test deleting a snapshot."""
        test_file = Path(temp_workdir) / "test.py"
        test_file.write_text("content")

        mgr = RollbackManager(temp_workdir)
        snapshot_id = mgr.snapshot_files(["test.py"])

        result = mgr.delete_snapshot(snapshot_id)

        assert result is True
        assert snapshot_id not in mgr.snapshots

    def test_cleanup_old_snapshots(self, temp_workdir):
        """Test cleaning up old snapshots."""
        test_file = Path(temp_workdir) / "test.py"
        test_file.write_text("content")

        mgr = RollbackManager(temp_workdir)

        # Create 7 snapshots
        for i in range(7):
            mgr.snapshot_files(["test.py"], f"snapshot_{i}")

        # Keep only last 3
        deleted = mgr.cleanup_old_snapshots(keep_last=3)

        assert deleted == 4

    def test_checksum_computation(self, temp_workdir):
        """Test checksum computation."""
        mgr = RollbackManager(temp_workdir)

        checksum1 = mgr._compute_checksum("hello")
        checksum2 = mgr._compute_checksum("hello")
        checksum3 = mgr._compute_checksum("world")

        assert checksum1 == checksum2  # Same content = same checksum
        assert checksum1 != checksum3  # Different content = different checksum


class TestRollbackManagerOrchestratorWiring:
    """Tests verifying RollbackManager is wired into Orchestrator."""

    def test_orchestrator_creates_rollback_manager(self, tmp_path):
        orch = Orchestrator(working_dir=str(tmp_path))
        assert hasattr(orch, "rollback_manager")
        assert isinstance(orch.rollback_manager, RollbackManager)

    def test_snapshot_created_before_write(self, tmp_path):
        orch = Orchestrator(working_dir=str(tmp_path))

        target = tmp_path / "snap_test.txt"
        target.write_text("original\n")

        # Read first so write-guard passes
        orch.execute_tool({"name": "read_file", "arguments": {"path": "snap_test.txt"}})
        orch.execute_tool({
            "name": "write_file",
            "arguments": {"path": "snap_test.txt", "content": "modified\n"},
        })

        # At least one snapshot should have been recorded
        assert len(orch.rollback_manager.snapshots) >= 1

    def test_rollback_restores_file(self, tmp_path):
        orch = Orchestrator(working_dir=str(tmp_path))

        target = tmp_path / "restore_test.txt"
        target.write_text("original\n")

        # Read then write (snapshot created internally)
        orch.execute_tool({"name": "read_file", "arguments": {"path": "restore_test.txt"}})
        orch.execute_tool({
            "name": "write_file",
            "arguments": {"path": "restore_test.txt", "content": "modified\n"},
        })

        snapshot_id = orch._current_snapshot_id
        assert snapshot_id is not None

        result = orch.rollback_manager.rollback(snapshot_id)
        assert result["ok"] is True
        assert target.read_text() == "original\n"


class TestMultiFileAtomicity:
    """Tests for step-level atomic multi-file rollback."""

    def test_append_to_snapshot_adds_new_file(self, tmp_path):
        """append_to_snapshot accumulates a second file into an existing snapshot."""
        mgr = RollbackManager(str(tmp_path))
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("original a")
        f2.write_text("original b")

        snap_id = mgr.snapshot_files(["a.py"])
        result = mgr.append_to_snapshot(snap_id, "b.py")

        assert result is True
        paths = {s.path for s in mgr.snapshots[snap_id]}
        assert "a.py" in paths
        assert "b.py" in paths

    def test_append_to_snapshot_skips_duplicate(self, tmp_path):
        """Appending the same file twice is idempotent."""
        mgr = RollbackManager(str(tmp_path))
        f = tmp_path / "a.py"
        f.write_text("original")

        snap_id = mgr.snapshot_files(["a.py"])
        mgr.append_to_snapshot(snap_id, "a.py")  # duplicate
        assert len(mgr.snapshots[snap_id]) == 1

    def test_append_creates_entry_if_snapshot_not_initialized(self, tmp_path):
        """append_to_snapshot creates snapshot entry even if begin_step_transaction was used."""
        mgr = RollbackManager(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("hello")

        # No prior snapshot_files() call — first append creates the entry
        result = mgr.append_to_snapshot("step_new", "x.py")
        assert result is True
        assert "step_new" in mgr.snapshots

    def test_rollback_restores_all_step_files(self, tmp_path):
        """Rollback of step snapshot restores all accumulated files atomically."""
        mgr = RollbackManager(str(tmp_path))
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f3 = tmp_path / "c.py"
        f1.write_text("orig a")
        f2.write_text("orig b")
        f3.write_text("orig c")

        # Simulate step transaction: start with first file, append others
        snap_id = mgr.snapshot_files(["a.py"], snapshot_id="step_001")
        mgr.append_to_snapshot("step_001", "b.py")
        mgr.append_to_snapshot("step_001", "c.py")

        # Simulate writes
        f1.write_text("broken a")
        f2.write_text("broken b")
        f3.write_text("broken c")

        result = mgr.rollback("step_001")
        assert result["ok"] is True
        assert result["restored_count"] == 3
        assert f1.read_text() == "orig a"
        assert f2.read_text() == "orig b"
        assert f3.read_text() == "orig c"

    def test_orchestrator_begin_step_transaction(self, tmp_path):
        """begin_step_transaction returns a step ID and sets _step_snapshot_id."""
        orch = Orchestrator(working_dir=str(tmp_path))
        step_id = orch.begin_step_transaction()
        assert step_id.startswith("step_")
        assert orch._step_snapshot_id == step_id

    def test_orchestrator_rollback_step_clears_snapshot_id(self, tmp_path):
        """rollback_step_transaction clears _step_snapshot_id after rollback."""
        orch = Orchestrator(working_dir=str(tmp_path))
        f = tmp_path / "target.py"
        f.write_text("original")

        step_id = orch.begin_step_transaction()
        orch.rollback_manager.append_to_snapshot(step_id, "target.py")
        f.write_text("modified")

        result = orch.rollback_step_transaction()
        assert result["ok"] is True
        assert orch._step_snapshot_id is None
        assert f.read_text() == "original"

    def test_orchestrator_rollback_step_no_transaction(self, tmp_path):
        """rollback_step_transaction returns error when no transaction is active."""
        orch = Orchestrator(working_dir=str(tmp_path))
        result = orch.rollback_step_transaction()
        assert result["ok"] is False
        assert "No active" in result["error"]

    def test_execute_tool_uses_step_snapshot_when_active(self, tmp_path):
        """execute_tool appends to step snapshot instead of creating individual ones."""
        orch = Orchestrator(working_dir=str(tmp_path))
        f1 = tmp_path / "file1.py"
        f2 = tmp_path / "file2.py"
        f1.write_text("orig1")
        f2.write_text("orig2")

        # Read both files first (satisfies read-before-edit guard)
        orch.execute_tool({"name": "read_file", "arguments": {"path": "file1.py"}})
        orch.execute_tool({"name": "read_file", "arguments": {"path": "file2.py"}})

        # Begin step transaction
        orch.begin_step_transaction()
        step_id = orch._step_snapshot_id

        # Write both files
        orch.execute_tool({"name": "write_file", "arguments": {"path": "file1.py", "content": "new1"}})
        orch.execute_tool({"name": "write_file", "arguments": {"path": "file2.py", "content": "new2"}})

        # Both should be in the step snapshot
        snap = orch.rollback_manager.snapshots.get(step_id, [])
        paths = {s.path for s in snap}
        assert "file1.py" in paths
        assert "file2.py" in paths

        # Rollback restores both
        result = orch.rollback_step_transaction()
        assert result["ok"] is True
        assert f1.read_text() == "orig1"
        assert f2.read_text() == "orig2"


class TestFileSnapshot:
    """Tests for FileSnapshot dataclass."""

    def test_file_snapshot_creation(self):
        """Test creating a FileSnapshot."""
        snap = FileSnapshot(
            path="test.py",
            content="print('hello')",
            timestamp="2024-01-01T00:00:00",
            checksum="abc123",
        )

        assert snap.path == "test.py"
        assert snap.content == "print('hello')"
