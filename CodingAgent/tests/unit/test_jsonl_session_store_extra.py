import json
from pathlib import Path
from unittest.mock import patch

from src.core.memory.session_store import SessionStore
from src.core.memory.jsonl_session_store import JsonlSessionStore


def test_tool_call_and_retrieval(tmp_path: Path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.add_tool_call("s1", "read_file", {"path": "/tmp/x"}, {"ok": True})
    calls = store.get_tool_calls("s1")
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "read_file"


def test_save_and_load_session_state(tmp_path: Path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    state = {"history": [{"role": "user", "content": "hi"}]}
    store.save_session_state("sess1", state, role="analyst", task="test")
    loaded = store.load_session_state("sess1")
    assert isinstance(loaded, dict)
    assert loaded["history"][0]["content"] == "hi"


def test_register_child_and_tree(tmp_path: Path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    store.register_child_session("root", "child-a", "analyst", "task A")
    store.register_child_session("child-a", "grandchild-1", "reviewer", "review")
    children = store.get_child_sessions("root")
    assert len(children) == 1
    tree = store.get_session_tree("root")
    assert tree["session_id"] == "root"
    assert tree["children"][0]["session_id"] == "child-a"


def test_write_with_retry_diagnostic_written(tmp_path: Path):
    store = SessionStore(workdir=str(tmp_path))

    class FakeConnAlwaysLocked:
        def execute(self, *a, **k):
            raise Exception("database is locked")

        def commit(self):
            raise Exception("database is locked")

    ok = store._write_with_retry(
        FakeConnAlwaysLocked(),
        "INSERT INTO x VALUES (?)",
        ("s1",),
        session_id="s1",
        attempts=1,
        base_backoff=0.001,
    )
    assert ok is False
    diag_dir = tmp_path / ".codingAgent"
    assert diag_dir.exists()
    files = list(diag_dir.glob("session_store_write_failure_*.json"))
    assert len(files) >= 1
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    assert data.get("session_id") == "s1"


def test_save_snapshot_uses_fallback_aware_sidecar_writer(tmp_path: Path):
    store = JsonlSessionStore(workdir=str(tmp_path))

    with patch(
        "src.core.memory.jsonl_sidecar_io.atomic_write_json",
        return_value=False,
        create=True,
    ):
        snap_id = store.save_snapshot("s1", '{"state": true}')

    assert snap_id
    snapshot_payload = store.get_snapshot("s1", snap_id)
    assert snapshot_payload is not None
    decoded = json.loads(snapshot_payload)
    assert decoded["state_json"] == '{"state": true}'


def test_revert_session_refuses_snapshot_target_outside_session_directory(
    tmp_path: Path,
):
    store = JsonlSessionStore(workdir=str(tmp_path))
    outside = tmp_path / "outside.jsonl"
    outside.write_text("must remain intact\n", encoding="utf-8")
    malicious_snapshot = json.dumps({"_file": str(outside), "_offset": 0})

    with patch.object(store, "get_snapshot", return_value=malicious_snapshot):
        store.revert_session("s1", "snap")

    assert outside.read_text(encoding="utf-8") == "must remain intact\n"


def test_revert_session_handles_target_removed_before_open(tmp_path: Path):
    store = JsonlSessionStore(workdir=str(tmp_path))
    sessions_dir = store._get_sessions_dir()
    target = sessions_dir / "s1.jsonl"
    snapshot = json.dumps({"_file": str(target), "_offset": 0})

    with patch.object(store, "get_snapshot", return_value=snapshot):
        store.revert_session("s1", "snap")
