import pytest
from pathlib import Path
from src.core.orchestration.rollback_manager import RollbackManager


def test_rollback_manager_selective_rollback_and_commit(tmp_path):
    mgr = RollbackManager(str(tmp_path))

    file_a = tmp_path / "file_a.py"
    file_b = tmp_path / "file_b.py"

    file_a.write_text("a = 1\n", encoding="utf-8")
    file_b.write_text("b = 1\n", encoding="utf-8")

    # 1. Take snapshot of both files in a step transaction
    snap_id = mgr.snapshot_files([str(file_a), str(file_b)])
    assert snap_id is not None
    assert len(mgr.snapshots[snap_id]) == 2

    # 2. Mutate both files
    file_a.write_text("a = 999\n", encoding="utf-8")
    file_b.write_text("b = 999\n", encoding="utf-8")

    # 3. Surgically roll back ONLY file_b.py (SPEC-P4.1)
    res = mgr.rollback_files([str(file_b)], snapshot_id=snap_id)
    assert res["ok"] is True
    assert "file_b.py" in res["restored_files"][0]

    # file_b.py is restored
    assert file_b.read_text(encoding="utf-8") == "b = 1\n"
    # file_a.py remains modified
    assert file_a.read_text(encoding="utf-8") == "a = 999\n"

    # Snapshot retains file_a.py
    assert len(mgr.snapshots[snap_id]) == 1
    assert "file_a.py" in mgr.snapshots[snap_id][0].path

    # 4. Commit file_a.py
    committed = mgr.commit_files([str(file_a)], snapshot_id=snap_id)
    assert len(committed) == 1
    assert "file_a.py" in committed[0]

    # Snapshot is fully settled and removed
    assert snap_id not in mgr.snapshots
    assert file_a.read_text(encoding="utf-8") == "a = 999\n"
