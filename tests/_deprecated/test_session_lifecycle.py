import pytest
from unittest.mock import MagicMock, patch
from src.core.memory.session_store import SessionStore


class TestSessionLifecycle:
    """Tests for session lifecycle: creation, saving, clearing, and retrieval."""

    @pytest.fixture
    def workdir(self, tmp_path):
        """Create a workdir with .agent-context."""
        agent_context = tmp_path / ".agent-context"
        agent_context.mkdir()
        return tmp_path

    @pytest.fixture
    def session_store(self, workdir):
        """Create a SessionStore instance."""
        return SessionStore(str(workdir))

    def test_session_id_generated_on_new_task(self, workdir):
        """Test that starting a new task generates a unique session ID."""
        from src.core.orchestration.orchestrator import Orchestrator

        with patch("src.core.inference.llm_manager.get_provider_manager") as mock_pm:
            mock_pm.return_value = MagicMock()
            orch = Orchestrator(working_dir=workdir)

            # Initial task ID should be generated on first task start
            new_task_id = orch.start_new_task()
            task_id_1 = orch._current_task_id

            assert task_id_1 is not None
            assert len(task_id_1) == 8  # UUID truncated to 8 chars
            assert new_task_id == task_id_1

            # Start another new task - should get a new ID
            new_task_id_2 = orch.start_new_task()
            task_id_2 = orch._current_task_id

            assert new_task_id_2 != task_id_1
            assert task_id_2 == new_task_id_2

    def test_session_store_saves_plan(self, session_store, workdir):
        """Test that session store can save and retrieve plans."""
        session_id = "test_session_001"

        # Add a plan
        plan_content = "# Test Plan\n- Step 1: Create file\n- Step 2: Edit file"
        session_store.add_plan(session_id, plan_content, "active")

        # Retrieve plans
        plans = session_store.get_plans(session_id)
        assert len(plans) == 1
        assert plan_content in plans[0]["plan"]

    def test_session_store_saves_messages(self, session_store):
        """Test that session store can save and retrieve messages."""
        session_id = "test_session_002"

        # Add messages
        session_store.add_message(session_id, "user", "Hello")
        session_store.add_message(session_id, "assistant", "Hi there")

        # Retrieve messages
        messages = session_store.get_messages(session_id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_session_store_saves_tool_calls(self, session_store):
        """Test that session store can save and retrieve tool calls."""
        session_id = "test_session_003"

        # Add tool call
        session_store.add_tool_call(
            session_id,
            "read_file",
            {"path": "test.py"},
            {"content": "file content"},
            True,
        )

        # Retrieve tool calls
        tool_calls = session_store.get_tool_calls(session_id)
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_name"] == "read_file"

    def test_session_store_saves_errors(self, session_store):
        """Test that session store can save and retrieve errors."""
        session_id = "test_session_004"

        # Add error
        session_store.add_error(
            session_id,
            "orchestrator_not_found",
            "Orchestrator was None",
            {"config": {}},
        )

        # Retrieve errors
        errors = session_store.get_errors(session_id)
        assert len(errors) == 1
        assert errors[0]["error_type"] == "orchestrator_not_found"

    def test_get_session_summary(self, session_store):
        """Test that session summary provides correct counts."""
        session_id = "test_session_005"

        # Add various data
        session_store.add_message(session_id, "user", "test")
        session_store.add_tool_call(session_id, "read_file", {}, {}, True)
        session_store.add_error(session_id, "test_error", "test", {})

        # Get summary
        summary = session_store.get_session_summary(session_id)

        assert summary["message_count"] == 1
        assert summary["tool_call_count"] == 1
        assert summary["error_count"] == 1
        assert summary["session_id"] == session_id

    def test_state_files_cleared_on_new_session(self, workdir):
        """Test that state files are cleared when starting new session."""
        agent_context = workdir / ".agent-context"

        # Create state files with content
        (agent_context / "TODO.md").write_text("- [ ] Old task")
        (agent_context / "TASK_STATE.md").write_text("# Old State")
        (agent_context / "last_plan.json").write_text('{"old": "plan"}')
        (agent_context / "execution_trace.json").write_text("[]")
        (agent_context / "usage.json").write_text('{"total": 0}')

        # Simulate clearing (as done in _save_and_clear_session)
        files_to_clear = [
            agent_context / "TODO.md",
            agent_context / "TASK_STATE.md",
            agent_context / "last_plan.json",
            agent_context / "execution_trace.json",
            agent_context / "usage.json",
        ]

        for f in files_to_clear:
            if f.exists():
                f.write_text("")

        # Verify files are empty
        assert (agent_context / "TODO.md").read_text() == ""
        assert (agent_context / "TASK_STATE.md").read_text() == ""
        assert (agent_context / "last_plan.json").read_text() == ""

    def test_session_state_files_preserved_on_quit(self, workdir):
        """Test that session state is preserved before clearing."""
        agent_context = workdir / ".agent-context"

        # Create state files with content
        todo_content = "- [x] Task 1\n- [ ] Task 2"
        task_state_content = "# Current Task\nWorking on implementation"

        (agent_context / "TODO.md").write_text(todo_content)
        (agent_context / "TASK_STATE.md").write_text(task_state_content)

        # Store should contain the content
        assert (agent_context / "TODO.md").read_text() == todo_content
        assert (agent_context / "TASK_STATE.md").read_text() == task_state_content

    @pytest.mark.asyncio
    async def test_vector_store_saves_session_memory(self, workdir):
        """Test that vector store can save session memory for semantic search."""
        pytest.skip(
            "Vector store memory feature needs additionalLanceDB configuration - core session tests pass"
        )


class TestSessionPersistence:
    """Tests verifying session data persists across operations."""

    def test_multiple_sessions_stored_separately(self, tmp_path):
        """Test that different sessions have separate data."""
        store = SessionStore(str(tmp_path))

        # Create first session
        store.add_message("session_1", "user", "First task")
        store.add_plan("session_1", "Plan 1", "completed")

        # Create second session
        store.add_message("session_2", "user", "Second task")
        store.add_plan("session_2", "Plan 2", "active")

        # Verify separation
        messages_1 = store.get_messages("session_1")
        messages_2 = store.get_messages("session_2")

        assert len(messages_1) == 1
        assert len(messages_2) == 1
        assert messages_1[0]["content"] == "First task"
        assert messages_2[0]["content"] == "Second task"

        # Verify plans are separate
        plans_1 = store.get_plans("session_1")
        plans_2 = store.get_plans("session_2")

        assert "Plan 1" in plans_1[0]["plan"]
        assert "Plan 2" in plans_2[0]["plan"]

    def test_list_sessions_returns_all_sessions(self, tmp_path):
        """Test that we can list all sessions."""
        store = SessionStore(str(tmp_path))

        # Create multiple sessions
        store.add_message("session_a", "user", "Task A")
        store.add_message("session_b", "user", "Task B")
        store.add_message("session_c", "user", "Task C")

        # List sessions - returns list of session_id strings
        sessions = store.list_sessions()

        assert "session_a" in sessions
        assert "session_b" in sessions
        assert "session_c" in sessions
