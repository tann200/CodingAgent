"""TASK-24 regression tests: single SQLite SessionStore, thin JSONL IO,
single RollbackManager, on-demand CompactionEngine, opt-in VectorStore,
and zero eager background LLM calls on short tasks.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_retired_legacy_modules_are_deleted() -> None:
    """TASK-24 retired jsonl_session_store, snapshot_manager, and advanced_features."""
    assert not (REPO_ROOT / "src/core/memory/jsonl_session_store.py").exists()
    assert not (REPO_ROOT / "src/core/orchestration/snapshot_manager.py").exists()
    assert not (REPO_ROOT / "src/core/memory/advanced_features.py").exists()


def test_session_store_single_sqlite_lifecycle_and_jsonl_io(tmp_path: Path) -> None:
    from src.core.memory.session_store import SessionStore, SqliteSessionStore, get_session_store

    # Legacy backend="jsonl" resolves cleanly to canonical SqliteSessionStore
    raw = get_session_store(workdir=str(tmp_path), backend="jsonl")
    assert isinstance(raw, SqliteSessionStore)
    raw.close()

    store = SessionStore(workdir=str(tmp_path))
    try:
        sid = "task24-sess"
        store.add_message(sid, "user", "initial prompt")
        store.add_message(sid, "assistant", "initial reply")
        store.add_tool_call(
            sid,
            "read_file",
            args={"path": "app.py"},
            result={"status": "ok", "content": "print(1)"},
        )

        # Snapshot + revert
        snap_id = store.save_snapshot(sid, json.dumps({"turn": 1}))
        assert snap_id is not None
        store.add_message(sid, "user", "after snapshot")
        assert len(store.get_messages(sid)) == 3
        rev = store.revert_session(sid, snap_id)
        assert isinstance(rev, dict) and rev.get("ok") is True
        assert [m["content"] for m in store.get_messages(sid)] == [
            "initial prompt",
            "initial reply",
        ]

        # Fork session
        forked_id = store.fork_session(sid)
        assert forked_id and forked_id != sid
        assert [m["content"] for m in store.get_messages(forked_id)] == [
            "initial prompt",
            "initial reply",
        ]

        # FTS5 search
        hits = store.search("initial")
        assert any(sid in str(h) for h in hits)

        # Portable JSONL export (with secret redaction) + import round-trip
        store.add_message(sid, "user", "secret ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD")
        export_path = tmp_path / "exported.jsonl"
        written = store.export_jsonl(sid, export_path)
        assert written >= 3
        raw_text = export_path.read_text(encoding="utf-8")
        assert "ghp_0123456789" not in raw_text

        imported_sid = store.import_jsonl(export_path, session_id="imported-24")
        assert imported_sid == "imported-24"
        imported_msgs = store.get_messages("imported-24")
        assert len(imported_msgs) == 3
        assert imported_msgs[0]["content"] == "initial prompt"

        # Delete cleans up cleanly
        assert store.delete_session(sid) >= 1
        assert sid not in store.list_sessions()
    finally:
        store.close()


def test_compaction_engine_on_demand_and_turn_group_atomic(tmp_path: Path) -> None:
    from src.core.memory.compaction_engine import CompactionEngine, CompactionResult
    from src.core.memory.compaction_service import CompactionService

    short_history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    engine = CompactionEngine(
        short_history,
        working_dir=tmp_path,
        prefer_deterministic=True,
    )
    assert engine.should_compact(token_budget=10000) is False

    # Build a history large enough to exceed a small token budget with an atomic tool turn
    long_history = [
        {"role": "user", "content": "turn 1 " + ("alpha " * 80)},
        {"role": "assistant", "content": "resp 1 " + ("beta " * 80)},
        {"role": "user", "content": "turn 2 " + ("gamma " * 80)},
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
    ]
    events: list[object] = []

    class _Bus:
        def publish_typed(self, evt: object) -> None:
            events.append(evt)

    big_engine = CompactionEngine(
        long_history,
        working_dir=tmp_path,
        event_bus=_Bus(),
        prefer_deterministic=True,
    )
    assert big_engine.should_compact(token_budget=100) is True
    res = big_engine.compact()
    assert isinstance(res, CompactionResult)
    assert res.success is True
    assert res.method == "deterministic"
    assert res.compacted_history
    assert any(type(evt).__name__ == "ContextCompacted" for evt in events)

    # CompactionService is a thin subclass delegating to CompactionEngine
    svc = CompactionService(long_history, working_dir=tmp_path, prefer_deterministic=True)
    assert isinstance(svc, CompactionEngine)
    svc_res = svc.compact()
    assert svc_res.success is True


def test_vector_store_is_strictly_opt_in(tmp_path: Path, monkeypatch) -> None:
    from src.core.indexing.vector_store import is_vector_store_enabled

    monkeypatch.delenv("CODINGAGENT_ENABLE_VECTOR_STORE", raising=False)
    assert is_vector_store_enabled(tmp_path) is False

    monkeypatch.setenv("CODINGAGENT_ENABLE_VECTOR_STORE", "1")
    assert is_vector_store_enabled(tmp_path) is True

    monkeypatch.setenv("CODINGAGENT_ENABLE_VECTOR_STORE", "0")
    assert is_vector_store_enabled(tmp_path) is False


def test_short_task_makes_zero_background_llm_calls_and_no_vectorstore_stubs(
    tmp_path: Path, monkeypatch
) -> None:
    from src.core.orchestration.orchestrator import Orchestrator
    from src.core.orchestration.orchestrator_event_subscriptions import _init_event_subscriptions

    monkeypatch.delenv("CODINGAGENT_ENABLE_VECTOR_STORE", raising=False)
    monkeypatch.delenv("CODINGAGENT_LLM_SESSION_TITLES", raising=False)

    mock_adapter = MagicMock()
    mock_adapter.provider = {"name": "mock", "type": "mock"}
    mock_adapter.default_model = "mock-model"
    mock_adapter.models = ["mock-model"]
    mock_adapter.get_models_from_api.return_value = {"models": [{"name": "mock-model"}]}

    orch = Orchestrator(adapter=mock_adapter, working_dir=str(tmp_path))
    try:
        # 1. Verify scheduler context_distillation handler skips short sessions
        distill_mock = MagicMock()
        orch.msg_mgr.messages = [
            {"role": "user", "content": "fix bug"},
            {"role": "assistant", "content": "done"},
        ]
        with patch("src.core.memory.distiller.distill_context", distill_mock):
            orch.event_bus.publish("scheduler.distill_request", {"source": "scheduler"})
        distill_mock.assert_not_called()

        # 2. Verify no .codingAgent/vectorstore directory was created
        assert not (tmp_path / ".codingAgent" / "vectorstore").exists()
        assert not (tmp_path / ".agent-context" / "vectorstore").exists()
        # Ensure _init_event_subscriptions is importable & wired
        assert callable(_init_event_subscriptions)
    finally:
        orch.close()


def test_deterministic_session_title_skips_background_llm_call(
    tmp_path: Path, monkeypatch
) -> None:
    """First turn derives a deterministic session title without calling generate_session_title."""
    from src.core.orchestration.inference_loop import run_agent_once_impl
    from src.core.orchestration.orchestrator import Orchestrator

    monkeypatch.delenv("CODINGAGENT_LLM_SESSION_TITLES", raising=False)
    monkeypatch.delenv("CODINGAGENT_ENABLE_VECTOR_STORE", raising=False)

    mock_adapter = MagicMock()
    mock_adapter.provider = {"name": "mock", "type": "mock"}
    mock_adapter.default_model = "mock-model"
    mock_adapter.models = ["mock-model"]
    mock_adapter.get_models_from_api.return_value = {"models": [{"name": "mock-model"}]}

    orch = Orchestrator(adapter=mock_adapter, working_dir=str(tmp_path))
    try:
        gen_title_mock = MagicMock()

        def _fake_round(*args, **kwargs):
            return {
                "last_result": {"ok": True},
                "history": [{"role": "assistant", "content": "done"}],
                "next_action": None,
                "errors": [],
                "verified_reads": [],
            }

        with (
            patch("src.core.memory.distiller.generate_session_title", gen_title_mock),
            patch("src.core.orchestration.inference_loop._execute_graph_round", side_effect=_fake_round),
        ):
            run_agent_once_impl(
                orch,
                None,
                [{"role": "user", "content": "Fix the off-by-one bug in parser.py"}],
                {},
            )

        gen_title_mock.assert_not_called()
        assert getattr(orch, "_session_title", "") == "Fix the off-by-one bug in parser.py"
        assert getattr(orch, "_session_title_thread", None) is None
    finally:
        orch.close()


def test_rollback_manager_text_and_binary_roundtrip(tmp_path: Path) -> None:
    from src.core.orchestration.rollback_manager import RollbackManager

    text_file = tmp_path / "notes.txt"
    bin_file = tmp_path / "blob.bin"
    original_text = "hello world\nline 2\n"
    original_bytes = b"\x00\x01\x02\xfe\xffBINARY\x00PAYLOAD"
    text_file.write_text(original_text, encoding="utf-8")
    bin_file.write_bytes(original_bytes)

    mgr = RollbackManager(str(tmp_path))
    snap_id = mgr.snapshot_files(["notes.txt", "blob.bin"], snapshot_id="step-1")
    assert snap_id == "step-1"

    # Mutate both files
    text_file.write_text("corrupted", encoding="utf-8")
    bin_file.write_bytes(b"corrupted")

    res = mgr.rollback(snap_id)
    assert res["ok"] is True
    assert text_file.read_text(encoding="utf-8") == original_text
    assert bin_file.read_bytes() == original_bytes
