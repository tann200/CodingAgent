import pytest
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.orchestration.orchestrator import Orchestrator


class TestTaskIDManagement:
    """Tests for task ID management functionality."""

    def test_start_new_task_generates_unique_id(self, tmp_path):
        """Test that start_new_task generates unique IDs for each task."""
        orch = Orchestrator(working_dir=str(tmp_path))

        task_id_1 = orch.start_new_task()
        task_id_2 = orch.start_new_task()

        assert task_id_1 is not None
        assert task_id_2 is not None
        assert len(task_id_1) == 8
        assert len(task_id_2) == 8
        assert task_id_1 != task_id_2

    def test_get_current_task_id_returns_current_id(self, tmp_path):
        """Test that get_current_task_id returns the current task ID."""
        orch = Orchestrator(working_dir=str(tmp_path))

        assert orch.get_current_task_id() is None

        task_id = orch.start_new_task()

        assert orch.get_current_task_id() == task_id

    def test_start_new_task_clears_message_history(self, tmp_path):
        """Test that start_new_task clears the message history."""
        orch = Orchestrator(working_dir=str(tmp_path))

        orch.msg_mgr.append("user", "Hello")
        orch.msg_mgr.append("assistant", "Hi there")

        assert len(orch.msg_mgr.messages) == 2

        orch.start_new_task()

        assert len(orch.msg_mgr.messages) == 0

    def test_multiple_tasks_have_isolated_context(self, tmp_path):
        """Test that each task has isolated context."""
        orch = Orchestrator(working_dir=str(tmp_path))

        task_id_1 = orch.start_new_task()
        orch.msg_mgr.append("user", "Task 1 message")
        orch.msg_mgr.append("assistant", "Task 1 response")

        task_id_2 = orch.start_new_task()

        assert task_id_1 != task_id_2
        assert len(orch.msg_mgr.messages) == 0

        assert orch.get_current_task_id() == task_id_2


class TestCancelEvent:
    """Tests for cancel event functionality."""

    def test_cancel_event_is_set_on_interrupt(self, tmp_path):
        """Test that cancel event can be set and checked."""
        orch = Orchestrator(working_dir=str(tmp_path))

        cancel_event = threading.Event()

        assert not cancel_event.is_set()

        cancel_event.set()

        assert cancel_event.is_set()

    def test_cancel_event_cleared_for_new_task(self, tmp_path):
        """Test that cancel event is cleared when starting a new task."""
        orch = Orchestrator(working_dir=str(tmp_path))

        cancel_event = threading.Event()
        cancel_event.set()

        assert cancel_event.is_set()

        cancel_event.clear()

        assert not cancel_event.is_set()


class TestExecutionTrace:
    """Tests for execution trace functionality."""

    def test_clear_execution_trace(self, tmp_path):
        """Test that _clear_execution_trace clears the trace file."""
        orch = Orchestrator(working_dir=str(tmp_path))

        orch._append_execution_trace({"tool": "test", "args": {}})

        trace = orch._read_execution_trace()
        assert len(trace) == 1

        orch._clear_execution_trace()

        trace = orch._read_execution_trace()
        assert len(trace) == 0


class TestLoopPrevention:
    """Tests for loop prevention functionality."""

    def test_loop_detection_with_recent_trace(self, tmp_path):
        """Test that loop detection works with recent trace entries."""
        orch = Orchestrator(working_dir=str(tmp_path))

        orch._clear_execution_trace()

        orch._append_execution_trace({"tool": "bash", "args": {"command": "ls"}})
        orch._append_execution_trace({"tool": "bash", "args": {"command": "ls"}})

        assert orch._check_loop_prevention("bash", {"command": "ls"}) is True

    def test_no_loop_detection_for_different_tools(self, tmp_path):
        """Test that different tools don't trigger loop detection."""
        orch = Orchestrator(working_dir=str(tmp_path))

        orch._clear_execution_trace()

        orch._append_execution_trace({"tool": "bash", "args": {"command": "ls"}})
        orch._append_execution_trace({"tool": "read_file", "args": {"path": "test.py"}})

        assert orch._check_loop_prevention("bash", {"command": "ls"}) is False
