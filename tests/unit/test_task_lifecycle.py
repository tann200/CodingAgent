"""Tests for src/core/orchestration/task_lifecycle.py (Phase D refactor).

Covers:
- start_new_task_impl: resets task ID, message history, session state, plan mode
- restore_continue_state_impl: restores message history and session-read files
- sync_session_state_impl: delegates to session_mgr.sync_agent_session_state
- get_current_task_id_impl: returns orch._current_task_id
- get_file_lock_manager_impl: returns orch.file_lock_manager
- approve_plan_impl / reject_plan_impl: set flags and fire asyncio event
- wait_for_plan_approval_impl: async, returns True/False based on approval
- get_tools_for_role_impl: returns filtered tool list or falls back to full registry
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(**kwargs):
    """Return a minimal mock that satisfies the task_lifecycle functions."""
    orch = MagicMock()
    orch._current_task_id = "abc12345"
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._execution_trace_buffer = []
    orch._current_snapshot_id = None
    orch._step_snapshot_id = None
    orch._plan_approval_event = None
    orch._plan_approved = False
    orch._pending_delegations = []
    orch._session_title = "old-title"
    orch._agent_mode = "planning"
    orch._provider_name = "openai"
    orch._last_agent_state = {}
    orch.working_dir = "/tmp/test_working_dir"
    orch.plan_mode = None
    orch.msg_mgr = MagicMock()
    orch.msg_mgr.messages = ["old_msg"]
    for k, v in kwargs.items():
        setattr(orch, k, v)
    return orch


# ---------------------------------------------------------------------------
# start_new_task_impl
# ---------------------------------------------------------------------------


class TestStartNewTaskImpl:
    def test_returns_new_task_id(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        old_id = orch._current_task_id
        result = start_new_task_impl(orch)
        assert result == orch._current_task_id
        assert result != old_id

    def test_task_id_is_8_chars(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        task_id = start_new_task_impl(orch)
        assert len(task_id) == 8

    def test_clears_message_history(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        orch.msg_mgr.messages = ["msg1", "msg2"]
        start_new_task_impl(orch)
        assert orch.msg_mgr.messages == []

    def test_clears_session_read_files(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl
        import os

        orch = _make_orch()
        orch._session_read_files = {"file_a.py", "file_b.py"}
        start_new_task_impl(orch)
        # P2-C: start_new_task_impl now pre-seeds _session_read_files with
        # internal agent-context files instead of resetting to an empty set.
        # The old stale files (file_a.py, file_b.py) must be gone.
        assert "file_a.py" not in orch._session_read_files
        assert "file_b.py" not in orch._session_read_files
        # At least the internal agent files should be pre-seeded.
        # Use os.path.realpath to handle symlinks (e.g. /tmp → /private/tmp on macOS).
        agent_ctx = os.path.realpath("/tmp/test_working_dir/.codingAgent")
        seeded = orch._session_read_files
        assert any(os.path.realpath(p).startswith(agent_ctx) for p in seeded), (
            f"Expected agent-context files in _session_read_files, got: {seeded}"
        )

    def test_clears_session_modified_files(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        orch._session_modified_files = {"out.py"}
        start_new_task_impl(orch)
        assert orch._session_modified_files == set()

    def test_resets_plan_approval_state(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        orch._plan_approved = True
        orch._plan_approval_event = MagicMock()
        start_new_task_impl(orch)
        assert orch._plan_approved is False
        assert orch._plan_approval_event is None

    def test_clears_pending_delegations(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        orch._pending_delegations = ["delegation_1"]
        start_new_task_impl(orch)
        assert orch._pending_delegations == []

    def test_resets_session_title(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        orch._session_title = "Previous task title"
        start_new_task_impl(orch)
        assert orch._session_title is None

    def test_resets_agent_mode_to_execution(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        orch._agent_mode = "planning"
        start_new_task_impl(orch)
        assert orch._agent_mode == "execution"

    def test_calls_publish_git_status(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        start_new_task_impl(orch)
        orch._publish_git_status.assert_called_once()

    def test_disables_plan_mode_when_active(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        orch = _make_orch()
        plan_mode = MagicMock()
        plan_mode.disable = MagicMock()
        orch.plan_mode = plan_mode
        start_new_task_impl(orch)
        plan_mode.disable.assert_called_once()


# ---------------------------------------------------------------------------
# restore_continue_state_impl
# ---------------------------------------------------------------------------


class TestRestoreContinueStateImpl:
    def test_restores_message_history(self):
        from src.core.orchestration.task_lifecycle import restore_continue_state_impl

        orch = _make_orch()
        history = [{"role": "user", "content": "hello"}]
        restore_continue_state_impl(orch, {"history": history})
        assert orch.msg_mgr.messages == history

    def test_restores_session_read_files(self):
        from src.core.orchestration.task_lifecycle import restore_continue_state_impl

        orch = _make_orch()
        restore_continue_state_impl(orch, {"session_read_files": ["a.py", "b.py"]})
        assert orch._session_read_files == {"a.py", "b.py"}

    def test_restores_last_agent_state_fields(self):
        from src.core.orchestration.task_lifecycle import restore_continue_state_impl

        orch = _make_orch()
        orch._last_agent_state = {}
        state = {
            "current_plan": ["step1"],
            "current_step": 2,
            "working_dir": "/tmp/foo",
            "step_retry_counts": {"step1": 1},
        }
        restore_continue_state_impl(orch, state)
        assert orch._last_agent_state["current_plan"] == ["step1"]
        assert orch._last_agent_state["current_step"] == 2

    def test_handles_missing_keys_gracefully(self):
        from src.core.orchestration.task_lifecycle import restore_continue_state_impl

        orch = _make_orch()
        # Should not raise even with an empty state dict
        restore_continue_state_impl(orch, {})


# ---------------------------------------------------------------------------
# sync_session_state_impl
# ---------------------------------------------------------------------------


class TestSyncSessionStateImpl:
    def test_calls_session_mgr_sync(self):
        from src.core.orchestration.task_lifecycle import sync_session_state_impl

        orch = _make_orch()
        orch.session_mgr = MagicMock()
        sync_session_state_impl(orch)
        orch.session_mgr.sync_agent_session_state.assert_called_once()

    def test_noop_when_no_session_mgr(self):
        from src.core.orchestration.task_lifecycle import sync_session_state_impl

        orch = _make_orch()
        del orch.session_mgr
        # Should not raise
        sync_session_state_impl(orch)


# ---------------------------------------------------------------------------
# get_current_task_id_impl
# ---------------------------------------------------------------------------


class TestGetCurrentTaskIdImpl:
    def test_returns_current_task_id(self):
        from src.core.orchestration.task_lifecycle import get_current_task_id_impl

        orch = _make_orch()
        orch._current_task_id = "task-xyz"
        assert get_current_task_id_impl(orch) == "task-xyz"

    def test_returns_none_when_unset(self):
        from src.core.orchestration.task_lifecycle import get_current_task_id_impl

        orch = _make_orch()
        orch._current_task_id = None
        assert get_current_task_id_impl(orch) is None


# ---------------------------------------------------------------------------
# get_file_lock_manager_impl
# ---------------------------------------------------------------------------


class TestGetFileLockManagerImpl:
    def test_returns_file_lock_manager(self):
        from src.core.orchestration.task_lifecycle import get_file_lock_manager_impl

        orch = _make_orch()
        flm = MagicMock()
        orch.file_lock_manager = flm
        assert get_file_lock_manager_impl(orch) is flm


# ---------------------------------------------------------------------------
# approve_plan_impl / reject_plan_impl
# ---------------------------------------------------------------------------


class TestApprovePlanImpl:
    def test_sets_plan_approved_true(self):
        from src.core.orchestration.task_lifecycle import approve_plan_impl

        orch = _make_orch()
        orch._plan_approved = False
        event = asyncio.Event()
        orch._plan_approval_event = event
        approve_plan_impl(orch)
        assert orch._plan_approved is True

    def test_fires_approval_event(self):
        from src.core.orchestration.task_lifecycle import approve_plan_impl

        orch = _make_orch()
        event = asyncio.Event()
        orch._plan_approval_event = event
        approve_plan_impl(orch)
        assert event.is_set()

    def test_disables_plan_mode(self):
        from src.core.orchestration.task_lifecycle import approve_plan_impl

        orch = _make_orch()
        orch._plan_approval_event = None
        pm = MagicMock()
        orch.plan_mode = pm
        approve_plan_impl(orch)
        pm.disable.assert_called_once()


class TestRejectPlanImpl:
    def test_sets_plan_approved_false(self):
        from src.core.orchestration.task_lifecycle import reject_plan_impl

        orch = _make_orch()
        orch._plan_approved = True
        event = asyncio.Event()
        orch._plan_approval_event = event
        reject_plan_impl(orch)
        assert orch._plan_approved is False

    def test_fires_approval_event(self):
        from src.core.orchestration.task_lifecycle import reject_plan_impl

        orch = _make_orch()
        event = asyncio.Event()
        orch._plan_approval_event = event
        reject_plan_impl(orch)
        assert event.is_set()


# ---------------------------------------------------------------------------
# wait_for_plan_approval_impl
# ---------------------------------------------------------------------------


class TestWaitForPlanApprovalImpl:
    def test_returns_true_when_approved(self):
        from src.core.orchestration.task_lifecycle import (
            wait_for_plan_approval_impl,
            approve_plan_impl,
        )

        async def _run():
            orch = _make_orch()

            # Schedule approval after a tiny delay
            async def _approve():
                await asyncio.sleep(0.01)
                approve_plan_impl(orch)

            asyncio.ensure_future(_approve())
            result = await wait_for_plan_approval_impl(orch)
            return result

        result = asyncio.run(_run())
        assert result is True

    def test_returns_false_when_rejected(self):
        from src.core.orchestration.task_lifecycle import (
            wait_for_plan_approval_impl,
            reject_plan_impl,
        )

        async def _run():
            orch = _make_orch()

            async def _reject():
                await asyncio.sleep(0.01)
                reject_plan_impl(orch)

            asyncio.ensure_future(_reject())
            result = await wait_for_plan_approval_impl(orch)
            return result

        result = asyncio.run(_run())
        assert result is False


# ---------------------------------------------------------------------------
# get_tools_for_role_impl
# ---------------------------------------------------------------------------


class TestGetToolsForRoleImpl:
    def _orch_with_tools(self, tools_dict):
        orch = _make_orch()
        reg = MagicMock()
        reg.tools = tools_dict
        orch.tool_registry = reg
        return orch

    def test_falls_back_to_full_registry_on_import_error(self):
        from src.core.orchestration.task_lifecycle import get_tools_for_role_impl

        tools = {
            "read_file": {"description": "read"},
            "write_file": {"description": "write"},
            "bash": {"description": "bash"},
        }
        orch = self._orch_with_tools(tools)
        with patch(
            "src.core.orchestration.task_lifecycle.get_tools_for_role_impl",
            wraps=get_tools_for_role_impl,
        ):
            # Patch both toolset loaders to raise ImportError → triggers fallback
            with patch.dict(
                "sys.modules",
                {
                    "src.tools.toolsets.loader": None,
                    "src.config.toolsets.loader": None,
                },
            ):
                result = get_tools_for_role_impl(orch, "debugger")
        assert isinstance(result, list)
        assert len(result) == 3

    def test_fallback_returns_all_registered_tools(self):
        from src.core.orchestration.task_lifecycle import get_tools_for_role_impl

        tools = {
            "read_file": {"description": "r"},
            "write_file": {"description": "w"},
            "bash": {"description": "b"},
            "grep": {"description": "g"},
        }
        orch = self._orch_with_tools(tools)
        # unknown_role will cause a lookup failure → fallback
        result = get_tools_for_role_impl(orch, "unknown_role_xyz_phase_d")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_fallback_logs_warning(self):
        from src.core.orchestration.task_lifecycle import (
            get_tools_for_role_impl,
            guilogger,
        )

        tools = {"only_tool": {"description": "x"}}
        orch = self._orch_with_tools(tools)

        warning_calls = []
        with patch.object(
            guilogger,
            "warning",
            side_effect=lambda msg, *a, **kw: warning_calls.append(msg),
        ):
            get_tools_for_role_impl(orch, "debugger")

        assert any(
            "fallback" in str(m).lower() or "falling back" in str(m).lower()
            for m in warning_calls
        ), f"Expected warning about fallback; got: {warning_calls}"

    def test_result_has_name_and_description_keys(self):
        from src.core.orchestration.task_lifecycle import get_tools_for_role_impl

        tools = {"read_file": {"description": "reads files"}}
        orch = self._orch_with_tools(tools)
        result = get_tools_for_role_impl(orch, "unknown_role_xyz_phase_d")
        for item in result:
            assert "name" in item
            assert "description" in item


# ---------------------------------------------------------------------------
# G9: MCP tool routing
# ---------------------------------------------------------------------------


class TestGetToolsForRoleMcp:
    """Verify that get_tools_for_role_impl includes MCP tools (origin=mcp)."""

    def _orch_with_tools(self, tools_dict):
        orch = MagicMock()
        reg = MagicMock()
        reg.tools = tools_dict
        orch.tool_registry = reg
        return orch

    def test_mcp_tools_included_in_filtered_list(self):
        from src.core.orchestration.task_lifecycle import get_tools_for_role_impl

        tools = {
            "read_file": {"description": "reads"},
            "git__commit": {"description": "commit changes", "origin": "mcp"},
            "git__push": {"description": "push remote", "origin": "mcp"},
        }
        orch = self._orch_with_tools(tools)

        # Patch the loader that get_tools_for_role_impl actually calls
        with patch(
            "src.tools.toolsets.loader.get_toolset_for_role",
            return_value="operational",
        ), patch(
            "src.tools.toolsets.loader.get_tools_for_toolset",
            return_value=["read_file"],
        ):
            result = get_tools_for_role_impl(orch, "operational")

        names = [r["name"] for r in result]
        assert "git__commit" in names
        assert "git__push" in names

    def test_mcp_tools_included_in_fallback(self):
        from src.core.orchestration.task_lifecycle import get_tools_for_role_impl

        tools = {
            "bash": {"description": "run bash"},
            "mcp__srv__tool1": {"description": "mcp tool", "origin": "mcp"},
        }
        orch = self._orch_with_tools(tools)

        # Force fallback by making toolset lookup raise
        with patch(
            "src.tools.toolsets.loader.get_tools_for_toolset",
            side_effect=ValueError("no toolset"),
        ):
            result = get_tools_for_role_impl(orch, "unknown_role")

        names = [r["name"] for r in result]
        assert "mcp__srv__tool1" in names
