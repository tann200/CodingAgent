"""Unit tests for src.core.orchestration.session_manager.SessionManager.

Covers:
  SM-1   start_new_task() returns a non-empty string and resets file sets
  SM-2   record_read / record_modified populate the respective sets
  SM-3   reset_read_files() clears only the read set
  SM-4   log_tool_call delegates to SessionStore.add_tool_call
  SM-5   log_message delegates to SessionStore.add_message
  SM-6   create_snapshot calls lifecycle_manager.create_snapshot and save_snapshot
  SM-7   create_snapshot is a no-op when task_id is None
  SM-8   publish_files_changed publishes event with correct payload
  SM-9   publish_files_changed is silent when no files are modified
  SM-10  sync_agent_session_state delegates to AgentSessionManager
  SM-11  Property shims on Orchestrator delegate to session_mgr
  SM-12  Property shims fall back gracefully when session_mgr absent (object.__new__)
"""


# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sm(tmp_path: Path):
    """Return a SessionManager with mocked dependencies."""
    from src.core.orchestration.session_manager import SessionManager

    session_store = MagicMock()
    lifecycle_manager = MagicMock()
    event_bus = MagicMock()
    msg_mgr = MagicMock()
    msg_mgr.messages = [{"role": "user", "content": "hello"}]

    sm = SessionManager(
        working_dir=tmp_path,
        session_store=session_store,
        lifecycle_manager=lifecycle_manager,
        event_bus=event_bus,
        msg_mgr=msg_mgr,
    )
    return sm, session_store, lifecycle_manager, event_bus, msg_mgr


# ---------------------------------------------------------------------------
# SM-1  start_new_task
# ---------------------------------------------------------------------------


class TestStartNewTask:
    def test_sm1_returns_non_empty_string(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        tid = sm.start_new_task()
        assert isinstance(tid, str) and len(tid) > 0

    def test_sm1_sets_task_id(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        tid = sm.start_new_task()
        assert sm.task_id == tid

    def test_sm1_resets_file_sets(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm._read_files = {"/old/file.py"}
        sm._modified_files = {"/old/write.py"}
        sm.start_new_task()
        assert sm.read_files == set()
        assert sm.modified_files == set()

    def test_sm1_different_id_each_call(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        tid1 = sm.start_new_task()
        tid2 = sm.start_new_task()
        assert tid1 != tid2


# ---------------------------------------------------------------------------
# SM-2  record_read / record_modified
# ---------------------------------------------------------------------------


class TestRecordFiles:
    def test_sm2_record_read_adds_path(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm.record_read("/a/b.py")
        assert "/a/b.py" in sm.read_files

    def test_sm2_record_modified_adds_path(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm.record_modified("/a/c.py")
        assert "/a/c.py" in sm.modified_files

    def test_sm2_sets_are_independent(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm.record_read("/r.py")
        sm.record_modified("/w.py")
        assert "/w.py" not in sm.read_files
        assert "/r.py" not in sm.modified_files


# ---------------------------------------------------------------------------
# SM-3  reset_read_files
# ---------------------------------------------------------------------------


class TestResetReadFiles:
    def test_sm3_clears_read_set(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm.record_read("/x.py")
        sm.reset_read_files()
        assert sm.read_files == set()

    def test_sm3_does_not_clear_modified(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm.record_modified("/x.py")
        sm.reset_read_files()
        assert "/x.py" in sm.modified_files


# ---------------------------------------------------------------------------
# SM-4  log_tool_call
# ---------------------------------------------------------------------------


class TestLogToolCall:
    def test_sm4_delegates_to_session_store(self, tmp_path):
        sm, session_store, *_ = _make_sm(tmp_path)
        sm._task_id = "abc123"
        sm.log_tool_call("read_file", {"path": "/x"}, "content", success=True)
        session_store.add_tool_call.assert_called_once_with(
            session_id="abc123",
            tool_name="read_file",
            args={"path": "/x"},
            result="content",
            success=True,
        )

    def test_sm4_uses_unknown_when_no_task_id(self, tmp_path):
        sm, session_store, *_ = _make_sm(tmp_path)
        sm._task_id = None
        sm.log_tool_call("bash", {}, "ok")
        assert session_store.add_tool_call.call_args[1]["session_id"] == "unknown"

    def test_sm4_silent_on_store_error(self, tmp_path):
        sm, session_store, *_ = _make_sm(tmp_path)
        session_store.add_tool_call.side_effect = RuntimeError("db error")
        # should not raise
        sm.log_tool_call("bash", {}, "ok")


# ---------------------------------------------------------------------------
# SM-5  log_message
# ---------------------------------------------------------------------------


class TestLogMessage:
    def test_sm5_delegates_to_session_store(self, tmp_path):
        sm, session_store, *_ = _make_sm(tmp_path)
        sm._task_id = "t1"
        sm.log_message("user", "hello")
        session_store.add_message.assert_called_once_with(
            session_id="t1", role="user", content="hello"
        )

    def test_sm5_silent_on_error(self, tmp_path):
        sm, session_store, *_ = _make_sm(tmp_path)
        session_store.add_message.side_effect = Exception("fail")
        sm.log_message("assistant", "hi")  # must not raise


# ---------------------------------------------------------------------------
# SM-6 / SM-7  create_snapshot
# ---------------------------------------------------------------------------


class TestCreateSnapshot:
    def test_sm6_calls_lifecycle_manager(self, tmp_path):
        sm, _, lifecycle_manager, *_ = _make_sm(tmp_path)
        sm._task_id = "snap1"
        lifecycle_manager.create_snapshot.return_value = {"id": "snap1"}
        sm.create_snapshot(usage_buffer={"bash": 3})
        lifecycle_manager.create_snapshot.assert_called_once()
        lifecycle_manager.save_snapshot.assert_called_once()

    def test_sm6_snapshot_contains_task_id(self, tmp_path):
        sm, _, lifecycle_manager, *_ = _make_sm(tmp_path)
        sm._task_id = "snap2"
        lifecycle_manager.create_snapshot.return_value = {}
        sm.create_snapshot()
        kwargs = lifecycle_manager.create_snapshot.call_args[1]
        assert kwargs["session_id"] == "snap2"

    def test_sm7_noop_when_task_id_none(self, tmp_path):
        sm, _, lifecycle_manager, *_ = _make_sm(tmp_path)
        sm._task_id = None
        sm.create_snapshot()
        lifecycle_manager.create_snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# SM-8 / SM-9  publish_files_changed
# ---------------------------------------------------------------------------


class TestPublishFilesChanged:
    def test_sm8_publishes_event(self, tmp_path):
        sm, _, _, event_bus, _ = _make_sm(tmp_path)
        sm._modified_files = {str(tmp_path / "x.py")}
        sm.publish_files_changed()
        event_bus.publish.assert_called_once()
        topic, payload = event_bus.publish.call_args[0]
        assert topic == "session.files_changed"
        assert "files" in payload

    def test_sm9_silent_when_no_modified_files(self, tmp_path):
        sm, _, _, event_bus, _ = _make_sm(tmp_path)
        sm._modified_files = set()
        sm.publish_files_changed()
        event_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# SM-10  sync_agent_session_state
# ---------------------------------------------------------------------------


class TestSyncAgentSessionState:
    def test_sm10_delegates_to_agent_session_manager(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm._task_id = "t99"
        fake_asm = MagicMock()
        with patch(
            "src.core.orchestration.agent_session_manager.get_agent_session_manager",
            return_value=fake_asm,
        ):
            sm.sync_agent_session_state()
            fake_asm.update_session_state.assert_called_once()

    def test_sm10_falls_back_to_provider_manager_when_adapter_none(self, tmp_path):
        """When adapter is None, SessionManager should try ProviderManager.get_active_adapter()."""
        sm, *_ = _make_sm(tmp_path)
        sm._task_id = "t-fallback"

        # Fake adapter returned by ProviderManager (use SimpleNamespace for typing)
        from types import SimpleNamespace

        fake_adapter = SimpleNamespace(
            provider={"name": "acme_provider", "type": "acme"},
            default_model="acme-model-1",
        )

        # Fake ProviderManager with get_active_adapter
        fake_pm = MagicMock()
        fake_pm.get_active_adapter.return_value = fake_adapter
        fake_pm.get_provider_capabilities.return_value = {
            "provider_name": "acme_provider",
            "model": "acme-model-1",
        }

        fake_asm = MagicMock()

        with (
            patch(
                "src.core.orchestration.agent_session_manager.get_agent_session_manager",
                return_value=fake_asm,
            ),
            patch(
                "src.core.inference.llm_manager.get_provider_manager",
                return_value=fake_pm,
            ),
        ):
            sm.sync_agent_session_state(
                adapter=None, task="do fallback", current_plan=["p1"], current_step=1
            )

            # Ensure ProviderManager.get_active_adapter was queried
            fake_pm.get_active_adapter.assert_called()

            # And AgentSessionManager received provider/model derived from fake adapter
            fake_asm.update_session_state.assert_called_once()
            kwargs = fake_asm.update_session_state.call_args[1]
            assert kwargs["provider"] == "acme_provider"
            assert kwargs["model"] == "acme-model-1"

    def test_sm10_forwards_provider_model_and_files(self, tmp_path):
        """Ensure provider/model, message history, and file sets are forwarded."""
        sm, *_ = _make_sm(tmp_path)
        sm._task_id = "t-forward"
        sm._read_files = {"/a/read.py"}
        sm._modified_files = {"/b/changed.py"}

        from types import SimpleNamespace

        adapter = SimpleNamespace(
            provider={"name": "provX", "type": "provx"},
            default_model="provx-1",
        )

        fake_asm = MagicMock()
        with patch(
            "src.core.orchestration.agent_session_manager.get_agent_session_manager",
            return_value=fake_asm,
        ):
            sm.sync_agent_session_state(
                adapter=adapter, task="do work", current_plan=["p"], current_step=0
            )

            fake_asm.update_session_state.assert_called_once()
            kwargs = fake_asm.update_session_state.call_args[1]
            assert kwargs["provider"] == "provX"
            assert kwargs["model"] == "provx-1"
            assert set(kwargs["files_read"]) == {"/a/read.py"}
            assert set(kwargs["files_modified"]) == {"/b/changed.py"}

    def test_sm10_handles_adapter_attribute_error(self, tmp_path):
        """If adapter attribute access raises, sync must still call update_session_state."""
        sm, *_ = _make_sm(tmp_path)
        sm._task_id = "t-error"

        class BadAdapter:
            @property
            def provider(self):
                raise RuntimeError("boom")

            @property
            def default_model(self):
                raise RuntimeError("boom2")

        bad = BadAdapter()
        fake_asm = MagicMock()
        with patch(
            "src.core.orchestration.agent_session_manager.get_agent_session_manager",
            return_value=fake_asm,
        ):
            # must not raise
            sm.sync_agent_session_state(adapter=bad, task="error case")
            fake_asm.update_session_state.assert_called_once()

    def test_sm10_uses_default_session_id_when_none(self, tmp_path):
        sm, *_ = _make_sm(tmp_path)
        sm._task_id = None
        fake_asm = MagicMock()
        with patch(
            "src.core.orchestration.agent_session_manager.get_agent_session_manager",
            return_value=fake_asm,
        ):
            sm.sync_agent_session_state(adapter=None, task="no task id")
            fake_asm.update_session_state.assert_called_once()
            kwargs = fake_asm.update_session_state.call_args[1]
            assert kwargs["session_id"] == "default"


# ---------------------------------------------------------------------------
# SM-11 / SM-12  Orchestrator property shims
# ---------------------------------------------------------------------------


class TestOrchestratorPropertyShims:
    def test_sm11_session_read_files_property(self, tmp_path):
        """Property getter/setter on Orchestrator delegate to session_mgr."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        # Inject a minimal session_mgr
        from src.core.orchestration.session_manager import SessionManager

        orch.session_mgr = SessionManager(
            working_dir=tmp_path,
            session_store=MagicMock(),
            lifecycle_manager=MagicMock(),
            event_bus=MagicMock(),
        )
        orch._session_read_files = {"/a.py"}
        assert orch._session_read_files == {"/a.py"}
        assert orch.session_mgr.read_files == {"/a.py"}

    def test_sm11_session_modified_files_property(self, tmp_path):
        from src.core.orchestration.orchestrator import Orchestrator
        from src.core.orchestration.session_manager import SessionManager

        orch = object.__new__(Orchestrator)
        orch.session_mgr = SessionManager(
            working_dir=tmp_path,
            session_store=MagicMock(),
            lifecycle_manager=MagicMock(),
            event_bus=MagicMock(),
        )
        orch._session_modified_files = {"/b.py"}
        assert orch._session_modified_files == {"/b.py"}

    def test_sm11_current_task_id_property(self, tmp_path):
        from src.core.orchestration.orchestrator import Orchestrator
        from src.core.orchestration.session_manager import SessionManager

        orch = object.__new__(Orchestrator)
        orch.session_mgr = SessionManager(
            working_dir=tmp_path,
            session_store=MagicMock(),
            lifecycle_manager=MagicMock(),
            event_bus=MagicMock(),
        )
        orch._current_task_id = "task-abc"
        assert orch._current_task_id == "task-abc"
        assert orch.session_mgr.task_id == "task-abc"

    def test_sm12_fallback_when_no_session_mgr(self):
        """Property setter must not raise when session_mgr is absent."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        # No session_mgr — should use __dict__ fallback
        orch._session_read_files = {"/fallback.py"}
        assert "/fallback.py" in orch._session_read_files

        orch._session_modified_files = {"/mod.py"}
        assert "/mod.py" in orch._session_modified_files

        orch._current_task_id = "fallback-tid"
        assert orch._current_task_id == "fallback-tid"
