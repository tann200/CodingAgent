"""
Integration tests for SessionStore wiring in Orchestrator, planning_node, and debug_node.
"""

import pytest
import json
from pathlib import Path
from src.core.memory.session_store import SessionStore
from src.core.orchestration.orchestrator import Orchestrator


class TestSessionStoreDirectAPI:
    """Tests for SessionStore's public API."""

    def test_add_and_retrieve_tool_call(self, tmp_path):
        store = SessionStore(workdir=str(tmp_path))
        store.add_tool_call(
            session_id="s1",
            tool_name="read_file",
            args={"path": "foo.py"},
            result={"status": "ok"},
            success=True,
        )
        summary = store.get_session_summary("s1")
        assert summary["tool_call_count"] >= 1

    def test_add_and_retrieve_plan(self, tmp_path):
        store = SessionStore(workdir=str(tmp_path))
        plan = json.dumps([{"description": "step 1"}, {"description": "step 2"}])
        store.add_plan(session_id="s2", plan=plan, status="created")

        # Verify it doesn't raise and db file exists
        assert (tmp_path / ".agent-context" / "session.db").exists()

    def test_add_and_retrieve_error(self, tmp_path):
        store = SessionStore(workdir=str(tmp_path))
        store.add_error(
            session_id="s3",
            error_type="import_error",
            error_message="No module named foo",
            context={"attempt": 1},
        )
        summary = store.get_session_summary("s3")
        assert summary["error_count"] >= 1

    def test_db_file_created(self, tmp_path):
        SessionStore(workdir=str(tmp_path))
        assert (tmp_path / ".agent-context" / "session.db").exists()

    def test_separate_sessions_isolated(self, tmp_path):
        store = SessionStore(workdir=str(tmp_path))
        store.add_tool_call("session_A", "write_file", {}, {}, True)
        store.add_error("session_B", "runtime_error", "oops", {})

        summary_a = store.get_session_summary("session_A")
        summary_b = store.get_session_summary("session_B")

        assert summary_a["tool_call_count"] == 1
        assert summary_a["error_count"] == 0
        assert summary_b["tool_call_count"] == 0
        assert summary_b["error_count"] == 1


class TestSessionStoreOrchestratorWiring:
    """Tests verifying SessionStore is wired into Orchestrator.execute_tool."""

    def test_orchestrator_has_session_store(self, tmp_path):
        orch = Orchestrator(working_dir=str(tmp_path))
        assert hasattr(orch, "session_store")
        assert isinstance(orch.session_store, SessionStore)

    def test_tool_call_logged_on_success(self, tmp_path):
        orch = Orchestrator(working_dir=str(tmp_path))
        orch._current_task_id = "test_session"

        target = tmp_path / "hello.txt"
        target.write_text("hi\n")

        orch.execute_tool({"name": "read_file", "arguments": {"path": "hello.txt"}})

        summary = orch.session_store.get_session_summary("test_session")
        assert summary["tool_call_count"] >= 1

    def test_tool_call_logged_on_failure(self, tmp_path):
        orch = Orchestrator(working_dir=str(tmp_path))
        orch._current_task_id = "fail_session"

        # read_file on a missing path returns a failed result (no exception raised)
        orch.execute_tool({
            "name": "read_file",
            "arguments": {"path": "does_not_exist.txt"},
        })

        summary = orch.session_store.get_session_summary("fail_session")
        # Logged regardless of success flag
        assert summary["tool_call_count"] >= 1
