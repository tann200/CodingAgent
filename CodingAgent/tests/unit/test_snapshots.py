"""P3-T5: Tests for _capture_snapshot in execution_helpers."""
from pathlib import Path


def test_snapshot_captures_content(tmp_path):
    from src.core.orchestration.graph.nodes.execution_helpers import _capture_snapshot

    target = tmp_path / "foo.py"
    target.write_text("original content")
    snap = _capture_snapshot("foo.py", str(tmp_path))
    assert snap is not None
    assert Path(snap).read_text() == "original content"


def test_snapshot_returns_none_for_missing_file(tmp_path):
    from src.core.orchestration.graph.nodes.execution_helpers import _capture_snapshot

    assert _capture_snapshot("nonexistent.py", str(tmp_path)) is None


def test_snapshot_stored_in_codingagent_snapshots_dir(tmp_path):
    from src.core.orchestration.graph.nodes.execution_helpers import _capture_snapshot

    target = tmp_path / "bar.py"
    target.write_text("hello")
    snap = _capture_snapshot("bar.py", str(tmp_path))
    snap_path = Path(snap)
    assert ".codingAgent" in snap_path.parts
    assert "snapshots" in snap_path.parts


def test_snapshot_multiple_writes_produce_different_files(tmp_path):
    from src.core.orchestration.graph.nodes.execution_helpers import _capture_snapshot
    import time

    target = tmp_path / "baz.py"
    target.write_text("v1")
    snap1 = _capture_snapshot("baz.py", str(tmp_path))
    time.sleep(0.01)
    target.write_text("v2")
    snap2 = _capture_snapshot("baz.py", str(tmp_path))
    assert snap1 != snap2
    assert Path(snap1).read_text() == "v1"
    assert Path(snap2).read_text() == "v2"


def test_snapshot_with_nested_path(tmp_path):
    from src.core.orchestration.graph.nodes.execution_helpers import _capture_snapshot

    nested = tmp_path / "src" / "module.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested content")
    snap = _capture_snapshot("src/module.py", str(tmp_path))
    assert snap is not None
    assert Path(snap).read_bytes() == b"nested content"
