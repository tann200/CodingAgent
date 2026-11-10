from pathlib import Path
from src.core.orchestration.rollback_manager import RollbackManager


def test_rollback_manager_cleans_up_multiple_created_files(tmp_path: Path):
    rm = RollbackManager(workdir=tmp_path)
    snap_id = "test_multi_step_snap"

    # Define 3 new files that do not exist initially
    file1 = tmp_path / "src" / "module_a.py"
    file2 = tmp_path / "src" / "module_b.py"
    file3 = tmp_path / "tests" / "test_ab.py"

    # Also an existing file that gets modified
    existing_file = tmp_path / "README.md"
    existing_file.write_text("# Initial Readme\n", encoding="utf-8")

    # Append non-existent files to snapshot before they are created
    rm.append_to_snapshot(snap_id, str(file1))
    rm.append_to_snapshot(snap_id, str(file2))
    rm.append_to_snapshot(snap_id, str(file3))
    rm.append_to_snapshot(snap_id, str(existing_file))

    # Verify that non-existent files are tracked with exists=False
    snaps = {s.path: s for s in rm.snapshots[snap_id]}
    assert snaps["src/module_a.py"].exists is False
    assert snaps["src/module_b.py"].exists is False
    assert snaps["tests/test_ab.py"].exists is False
    assert snaps["README.md"].exists is True

    # Now create the 3 files and modify the existing one
    file1.parent.mkdir(parents=True, exist_ok=True)
    file1.write_text("def a(): pass\n", encoding="utf-8")
    file2.write_text("def b(): pass\n", encoding="utf-8")
    file3.parent.mkdir(parents=True, exist_ok=True)
    file3.write_text("def test_ab(): assert True\n", encoding="utf-8")
    existing_file.write_text("# Corrupted Readme\n", encoding="utf-8")

    assert file1.exists()
    assert file2.exists()
    assert file3.exists()

    # Step verification fails -> trigger rollback
    result = rm.rollback(snap_id)
    assert result.get("ok") is True

    # All newly created files must be cleanly deleted (no orphaned files)
    assert not file1.exists()
    assert not file2.exists()
    assert not file3.exists()

    # The existing file must be restored to its original baseline
    assert existing_file.read_text(encoding="utf-8") == "# Initial Readme\n"
