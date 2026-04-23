"""
Regression tests for second batch of opencode-inspired improvements:

  EXPLORE-1 — analyst role maps to 'analysis' toolset (not 'planning')
  EXPLORE-2 — analysis.yaml exists and lists only read-only tools
  EXPLORE-3 — analyst role denies write tools (edit_file, write_file, delete_file)
  EXPLORE-4 — analyst role allows read tools (glob, grep, bash, read_file)
  DEBUGGER-1 — debugger role allows edit_file and run_tests
  DEBUGGER-2 — debugger role denies delete_file
  STEP-1 — step.start event is published before tool execution
  STEP-2 — step.finish event is published after tool execution with ok + elapsed_ms
"""


# ruff: noqa: E501
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# EXPLORE-1 — analyst toolset mapping
# ---------------------------------------------------------------------------

def test_analyst_maps_to_analysis_toolset():
    from src.config.toolsets.loader import get_toolset_for_role
    assert get_toolset_for_role("analyst") == "analysis"


# ---------------------------------------------------------------------------
# EXPLORE-2 — analysis.yaml exists and has no write/exec tools
# ---------------------------------------------------------------------------

def test_analysis_toolset_file_exists():
    path = Path(__file__).parents[2] / "src/config/toolsets/analysis.yaml"
    assert path.exists(), "analysis.yaml not found"


def test_analysis_toolset_contains_no_write_tools():
    from src.config.toolsets.loader import get_tools_for_toolset
    tools = get_tools_for_toolset("analysis")
    assert tools, "analysis toolset is empty"
    FORBIDDEN = {
        "write_file", "edit_file", "edit_file_atomic", "edit_by_line_range",
        "delete_file", "apply_patch", "run_tests", "run_js_tests",
        "run_linter", "run_ts_check", "delegate_task",
    }
    overlap = set(tools) & FORBIDDEN
    assert not overlap, f"analysis toolset contains write/exec tools: {overlap}"


def test_analysis_toolset_contains_read_tools():
    from src.config.toolsets.loader import get_tools_for_toolset
    tools = set(get_tools_for_toolset("analysis"))
    required = {"read_file", "glob", "grep", "find_symbol", "bash_readonly"}
    missing = required - tools
    assert not missing, f"analysis toolset missing expected read tools: {missing}"


# ---------------------------------------------------------------------------
# EXPLORE-3/4 — analyst role_config denies write, allows reads
# ---------------------------------------------------------------------------

def test_analyst_role_denies_write_tools():
    from src.core.orchestration.role_config import is_tool_allowed_for_role
    for tool in ("write_file", "edit_file", "delete_file", "apply_patch", "run_tests"):
        assert not is_tool_allowed_for_role(tool, "analyst"), \
            f"analyst should deny '{tool}'"


def test_analyst_role_allows_read_tools():
    from src.core.orchestration.role_config import is_tool_allowed_for_role
    for tool in ("read_file", "glob", "grep", "find_symbol", "bash", "list_files"):
        assert is_tool_allowed_for_role(tool, "analyst"), \
            f"analyst should allow '{tool}'"


# ---------------------------------------------------------------------------
# DEBUGGER-1/2 — debugger role config
# ---------------------------------------------------------------------------

def test_debugger_role_allows_edit_and_run_tests():
    from src.core.orchestration.role_config import is_tool_allowed_for_role
    for tool in ("edit_file", "edit_file_atomic", "run_tests", "run_linter"):
        assert is_tool_allowed_for_role(tool, "debugger"), \
            f"debugger should allow '{tool}'"


def test_debugger_role_denies_delete_and_delegate():
    from src.core.orchestration.role_config import is_tool_allowed_for_role
    for tool in ("delete_file", "delegate_task"):
        assert not is_tool_allowed_for_role(tool, "debugger"), \
            f"debugger should deny '{tool}'"


# ---------------------------------------------------------------------------
# STEP-1/2 — step boundary events published around tool execution
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> Dict[str, Any]:
    defaults = {
        "task": "test", "working_dir": "/tmp", "history": [],
        "verified_reads": [], "next_action": None, "last_result": None,
        "rounds": 0, "system_prompt": "", "errors": [], "session_id": "s1",
        "current_plan": None, "current_step": 0, "tool_call_count": 0,
        "max_tool_calls": 30, "tool_last_used": {}, "files_read": {},
        "original_task": None, "task_decomposed": False, "step_retry_counts": {},
        "no_plan_fail_count": 0, "plan_mode_enabled": False,
        "plan_mode_approved": None, "plan_mode_blocked_tool": None,
        "execution_waves": None, "plan_dag": None, "current_wave": 0,
        "preview_mode_enabled": False, "cancel_event": None,
        "recent_tool_calls": [], "planned_action": None,
        "replan_required": None, "action_failed": None,
        "plan_progress": None, "awaiting_user_input": None,
        "preview_confirmed": None, "pending_preview_id": None,
    }
    defaults.update(kwargs)
    return defaults


@pytest.mark.asyncio
async def test_step_start_event_published():
    """step.start event is published before tool execution."""
    from src.core.orchestration.graph.nodes.execution_node import execution_node

    events: list = []

    orc = MagicMock()
    orc.tool_registry = MagicMock()
    orc.tool_registry.tools = {}
    orc.tool_registry.get_openai_functions = MagicMock(return_value=[])
    orc.preflight_check = MagicMock(return_value={"ok": True})
    orc.execute_tool = MagicMock(return_value={"ok": True, "status": "ok", "result": {}})
    orc._session_read_files = set()
    orc._check_loop_prevention = MagicMock(return_value=False)
    orc._read_execution_trace = MagicMock(return_value=[])
    orc.get_file_lock_manager = MagicMock(return_value=None)
    orc.msg_mgr = MagicMock()
    orc.plan_mode = None
    orc._plan_mode_approved = None
    orc.begin_step_transaction = MagicMock()
    orc.working_dir = None
    orc.get_provider_capabilities = MagicMock(return_value={})
    orc.preview_service = None
    orc.cancel_event = None
    orc.current_role = None

    # Capture events via event_bus
    def _capture(event: str, payload: Any) -> None:
        events.append((event, payload))

    orc.event_bus = MagicMock()
    orc.event_bus.publish = _capture

    state = _make_state(
        next_action={"name": "glob", "arguments": {"pattern": "**/*.py"}},
        current_plan=[{"description": "find python files", "completed": False}],
        current_step=0,
    )

    config = {"configurable": {"orchestrator": orc}}
    await execution_node(state, config)

    event_names = [e[0] for e in events]
    assert "step.start" in event_names, f"step.start not fired; got: {event_names}"


@pytest.mark.asyncio
async def test_step_finish_event_has_ok_and_elapsed():
    """step.finish event carries ok and elapsed_ms fields."""
    from src.core.orchestration.graph.nodes.execution_node import execution_node

    events: list = []

    orc = MagicMock()
    orc.tool_registry = MagicMock()
    orc.tool_registry.tools = {}
    orc.tool_registry.get_openai_functions = MagicMock(return_value=[])
    orc.preflight_check = MagicMock(return_value={"ok": True})
    orc.execute_tool = MagicMock(return_value={"ok": True, "status": "ok", "result": {}})
    orc._session_read_files = set()
    orc._check_loop_prevention = MagicMock(return_value=False)
    orc._read_execution_trace = MagicMock(return_value=[])
    orc.get_file_lock_manager = MagicMock(return_value=None)
    orc.msg_mgr = MagicMock()
    orc.plan_mode = None
    orc._plan_mode_approved = None
    orc.begin_step_transaction = MagicMock()
    orc.working_dir = None
    orc.get_provider_capabilities = MagicMock(return_value={})
    orc.preview_service = None
    orc.cancel_event = None
    orc.current_role = None

    def _capture(event: str, payload: Any) -> None:
        events.append((event, payload))

    orc.event_bus = MagicMock()
    orc.event_bus.publish = _capture

    state = _make_state(
        next_action={"name": "read_file", "arguments": {"path": "foo.py"}},
    )

    config = {"configurable": {"orchestrator": orc}}
    await execution_node(state, config)

    finish_events = [p for (e, p) in events if e == "step.finish"]
    assert finish_events, "step.finish not fired"
    fe = finish_events[0]
    assert "ok" in fe
    assert "elapsed_ms" in fe
    assert isinstance(fe.get("elapsed_ms"), int)
