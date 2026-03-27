import pytest
from unittest.mock import MagicMock
from src.core.orchestration.graph.nodes.execution_node import execution_node


def _make_state(working_dir: str = ".", **kwargs) -> dict:
    """Create a minimal state dict for execution_node tests."""
    base = {
        "task": "test task",
        "history": [],
        "verified_reads": [],
        "rounds": 0,
        "working_dir": working_dir,
        "system_prompt": "",
        "next_action": None,
        "last_result": None,
        "errors": [],
        "current_plan": None,
        "current_step": 0,
        "deterministic": False,
        "seed": None,
        "analysis_summary": None,
        "relevant_files": None,
        "key_symbols": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "total_debug_attempts": 0,
        "last_debug_error_type": None,
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
        "analyst_findings": None,
        "plan_resumed": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": None,
        "original_task": None,
        "step_description": None,
        "planned_action": None,
        "plan_validation": None,
        "plan_enforce_warnings": None,
        "plan_strict_mode": None,
        "files_read": {},
        "tool_last_used": {},
    }
    base.update(kwargs)
    return base


class TestReadBeforeWriteEnforcement:
    """Tests for read-before-write enforcement in execution_node."""

    def _make_mock_orchestrator(self, tmp_path):
        """Create a mock orchestrator for testing."""
        orc = MagicMock()
        orc.cancel_event = None
        orc._session_read_files = set()
        orc._check_loop_prevention = MagicMock(return_value=False)
        orc.preflight_check = MagicMock(return_value={"ok": True})
        orc.execute_tool = MagicMock(return_value={"ok": True, "path": "test.py"})
        return orc

    @pytest.mark.asyncio
    async def test_write_to_existing_file_requires_read(self, tmp_path):
        """Writing to an existing file must require prior read."""
        test_file = tmp_path / "existing.py"
        test_file.write_text("old content")

        orc = self._make_mock_orchestrator(tmp_path)
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={
                "name": "write_file",
                "arguments": {"path": "existing.py", "content": "new content"},
            },
            verified_reads=[],  # No prior read
            files_read={},  # No file read tracking
        )

        result = await execution_node(state, config)

        # Should be blocked - file exists but wasn't read first
        assert result is not None
        assert result.get("last_result") is not None
        assert "error" in result["last_result"]
        err_msg = result["last_result"]["error"].lower()
        assert "must read" in err_msg or "security" in err_msg or "violation" in err_msg

    @pytest.mark.asyncio
    async def test_write_to_new_file_allowed_without_read(self, tmp_path):
        """Writing to a NEW file (that doesn't exist) should be allowed without prior read."""
        new_file = tmp_path / "new_file.py"
        assert not new_file.exists()  # Ensure file doesn't exist

        orc = self._make_mock_orchestrator(tmp_path)
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={
                "name": "write_file",
                "arguments": {"path": "new_file.py", "content": "new content"},
            },
            verified_reads=[],  # No prior read
            files_read={},  # No file read tracking
        )

        await execution_node(state, config)

        # Should NOT be blocked - file is new, no read required
        # The tool should have been executed
        orc.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_after_read_allowed(self, tmp_path):
        """Writing after reading should be allowed."""
        test_file = tmp_path / "read_first.py"
        test_file.write_text("original")

        orc = self._make_mock_orchestrator(tmp_path)
        orc._session_read_files = {str(test_file)}  # File in session reads
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={
                "name": "write_file",
                "arguments": {"path": "read_first.py", "content": "modified"},
            },
            verified_reads=[str(test_file)],  # File was read
            files_read={str(test_file): "original"},
        )

        await execution_node(state, config)

        # Should be allowed - file was read
        orc.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_edit_file_to_new_file_allowed(self, tmp_path):
        """edit_file to a non-existent file should be allowed."""
        new_file = tmp_path / "brand_new.py"
        assert not new_file.exists()

        orc = self._make_mock_orchestrator(tmp_path)
        config = {"configurable": {"orchestrator": orc}}

        state = _make_state(
            working_dir=str(tmp_path),
            next_action={
                "name": "edit_file",
                "arguments": {
                    "path": "brand_new.py",
                    "old_string": "",
                    "new_string": "content",
                },
            },
            verified_reads=[],
            files_read={},
        )

        await execution_node(state, config)

        # Should NOT be blocked - new file creation allowed
        orc.execute_tool.assert_called_once()
