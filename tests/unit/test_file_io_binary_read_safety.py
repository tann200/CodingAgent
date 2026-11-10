from pathlib import Path
from src.tools._file_io import write_file
from src.tools.guardrails import mark_file_read
from src.core.orchestration.rollback_manager import RollbackManager


def test_write_file_overwrites_binary_file_without_crash(tmp_path: Path):
    bin_file = tmp_path / "data.bin"
    # Write arbitrary non-UTF-8 binary bytes
    bin_file.write_bytes(b"\x80\x81\xff\xfe\x00\x01\xfa\xfb")

    # Satisfy read-before-write invariant
    mark_file_read(str(bin_file))

    # Overwrite the binary file with new text content
    res = write_file(path=str(bin_file), content="new text content", workdir=tmp_path)
    assert res.get("status") in ("ok", "success")
    assert bin_file.read_text(encoding="utf-8") == "new text content"


def test_rollback_manager_snapshots_binary_file_without_crash(tmp_path: Path):
    bin_file = tmp_path / "image.png"
    bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00")

    rm = RollbackManager(workdir=tmp_path)
    snap_id = rm.snapshot_files([str(bin_file)])
    assert snap_id is not None
    assert snap_id in rm.snapshots
    file_snaps = rm.snapshots[snap_id]
    assert len(file_snaps) == 1
    assert file_snaps[0].exists is True


def test_rollback_manager_append_to_snapshot_binary_file(tmp_path: Path):
    bin_file = tmp_path / "archive.zip"
    bin_file.write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00")

    rm = RollbackManager(workdir=tmp_path)
    snap_id = "test_snap_001"
    ok = rm.append_to_snapshot(snap_id, str(bin_file))
    assert ok is True
    assert snap_id in rm.snapshots
    assert len(rm.snapshots[snap_id]) == 1
    assert rm.snapshots[snap_id][0].exists is True
