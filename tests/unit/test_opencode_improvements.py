"""
Regression tests for opencode-inspired improvements:

  DOOM-1 — Doom loop detection blocks DOOM_LOOP_THRESHOLD consecutive identical tool calls
  DOOM-2 — Doom loop resets when tool or args differ
  DOOM-3 — recent_tool_calls advances after each execution
  TRUNC-1 — Tool output truncation caps large string values at 8000 chars
  TRUNC-2 — Nested result.content is also truncated
  TRUNC-3 — Short results are not truncated
  STATE-1 — recent_tool_calls present in AgentState TypedDict
  STATE-2 — recent_tool_calls initialised in initial_state
"""


# ruff: noqa: E501
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**kwargs) -> Dict[str, Any]:
    defaults = {
        "task": "test task",
        "working_dir": "/tmp",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": None,
        "rounds": 0,
        "system_prompt": "",
        "errors": [],
        "session_id": "test-session",
        "current_plan": None,
        "current_step": 0,
        "tool_call_count": 0,
        "max_tool_calls": 30,
        "tool_last_used": {},
        "files_read": {},
        "original_task": None,
        "task_decomposed": False,
        "step_retry_counts": {},
        "no_plan_fail_count": 0,
        "plan_mode_enabled": False,
        "plan_mode_approved": None,
        "plan_mode_blocked_tool": None,
        "execution_waves": None,
        "plan_dag": None,
        "current_wave": 0,
        "preview_mode_enabled": False,
        "cancel_event": None,
        "recent_tool_calls": [],
        "planned_action": None,
        "replan_required": None,
        "action_failed": None,
        "plan_progress": None,
        "awaiting_user_input": None,
        "preview_confirmed": None,
        "pending_preview_id": None,
    }
    defaults.update(kwargs)
    return defaults


def _make_orchestrator(tool_result: Dict[str, Any] | None = None) -> MagicMock:
    orc = MagicMock()
    orc.tool_registry = MagicMock()
    orc.tool_registry.tools = {}
    orc.tool_registry.get_openai_functions = MagicMock(return_value=[])
    orc.preflight_check = MagicMock(return_value={"ok": True})
    orc.execute_tool = MagicMock(
        return_value=tool_result or {"ok": True, "status": "ok", "result": {}}
    )
    orc._session_read_files = set()
    orc._check_loop_prevention = MagicMock(return_value=False)
    orc._read_execution_trace = MagicMock(return_value=[])
    orc.get_file_lock_manager = MagicMock(return_value=None)
    orc.event_bus = MagicMock()
    orc.msg_mgr = MagicMock()
    orc.plan_mode = None
    orc._plan_mode_approved = None
    orc.begin_step_transaction = MagicMock()
    orc.working_dir = None
    orc.get_provider_capabilities = MagicMock(return_value={})
    orc.preview_service = None
    # cancel_event must be None (not a MagicMock) so is_set() isn't accidentally truthy
    orc.cancel_event = None
    # current_role must be None so role enforcement doesn't block tools in tests
    orc.current_role = None
    return orc


# ---------------------------------------------------------------------------
# DOOM-1 — Doom loop: THRESHOLD consecutive identical calls are blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doom_loop_blocks_after_threshold():
    """3 consecutive identical tool calls trigger the doom loop guard."""
    from src.core.orchestration.graph.nodes.execution_node import execution_node

    tool_name = "read_file"
    tool_args = {"path": "foo.py"}
    fingerprint = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"

    # Simulate 2 previous identical calls already in state
    state = _make_state(
        next_action={"name": tool_name, "arguments": tool_args},
        recent_tool_calls=[fingerprint, fingerprint],  # 2 already; this call = 3rd
    )
    orc = _make_orchestrator()

    config = {"configurable": {"orchestrator": orc}}

    result = await execution_node(state, config)

    assert result["last_result"]["ok"] is False
    assert "DOOM LOOP" in result["last_result"]["error"]
    # execute_tool should NOT have been called
    orc.execute_tool.assert_not_called()


# ---------------------------------------------------------------------------
# DOOM-2 — Doom loop resets when tool name differs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doom_loop_does_not_trigger_for_different_tool():
    """Doom loop only fires for consecutive identical fingerprints; different tool is fine."""
    from src.core.orchestration.graph.nodes.execution_node import execution_node

    # Previous calls were for 'grep', but now we call 'read_file'
    grep_fp = f"grep:{json.dumps({'pattern': 'foo'}, sort_keys=True)}"
    state = _make_state(
        next_action={"name": "read_file", "arguments": {"path": "bar.py"}},
        recent_tool_calls=[grep_fp, grep_fp],
    )
    orc = _make_orchestrator(
        {"ok": True, "status": "ok", "result": {"content": "hello"}}
    )

    config = {"configurable": {"orchestrator": orc}}

    result = await execution_node(state, config)

    # Should have executed (not blocked)
    orc.execute_tool.assert_called_once()
    assert "DOOM LOOP" not in (result.get("last_result") or {}).get("error", "")


# ---------------------------------------------------------------------------
# DOOM-3 — recent_tool_calls is updated after each execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_tool_calls_updated_after_execution():
    """After a successful execution, recent_tool_calls grows by one."""
    from src.core.orchestration.graph.nodes.execution_node import execution_node

    tool_name = "glob"
    tool_args = {"pattern": "**/*.py"}
    state = _make_state(
        next_action={"name": tool_name, "arguments": tool_args},
        recent_tool_calls=[],
    )
    orc = _make_orchestrator({"ok": True, "status": "ok", "result": {"items": []}})

    config = {"configurable": {"orchestrator": orc}}

    result = await execution_node(state, config)

    updated = result.get("recent_tool_calls", [])
    assert len(updated) == 1
    assert tool_name in updated[0]


# ---------------------------------------------------------------------------
# TRUNC-1 — Tool output truncation at 8000 chars
# ---------------------------------------------------------------------------


def test_tool_output_truncation_top_level():
    """Large string values in execute_tool result are capped at 8000 chars."""
    from src.core.orchestration.orchestrator import Orchestrator

    orc = Orchestrator.__new__(Orchestrator)
    # Minimal setup so _normalize_tool_result works
    orc.working_dir = None  # type: ignore[attr-defined]
    orc._session_read_files = set()
    orc._usage_buffer = {}
    orc._step_snapshot_id = None
    orc.rollback_manager = MagicMock()
    orc.event_bus = MagicMock()
    orc.plan_mode = None  # type: ignore[attr-defined]
    orc._plan_mode_approved = None  # type: ignore[attr-defined]
    orc.current_role = None
    orc._tool_executor = None  # type: ignore[attr-defined]

    large_content = "x" * 20_000
    tool_fn = MagicMock(return_value={"ok": True, "output": large_content})
    orc.tool_registry = MagicMock()
    orc.tool_registry.get = MagicMock(
        return_value={
            "fn": tool_fn,
            "side_effects": [],
            "description": "test tool",
        }
    )

    with (
        patch(
            "src.core.orchestration.orchestrator.get_tool_contract", return_value=None
        ),
        patch(
            "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
            return_value=None,
        ),
        patch("src.core.orchestration.orchestrator.PERMISSION_REQUIRED_TOOLS", set()),
        patch("src.core.orchestration.orchestrator.WRITE_TOOLS_REQUIRING_READ", set()),
    ):
        result = orc.execute_tool({"name": "test_tool", "arguments": {}})

    # execute_tool always wraps: {"ok": True, "result": raw_res}
    raw = result.get("result", {})
    assert "output" in raw
    assert len(raw["output"]) <= 8000 + 100  # small slack for truncation notice
    assert "truncated" in raw["output"]


# ---------------------------------------------------------------------------
# TRUNC-2 — Nested result.content is also truncated
# ---------------------------------------------------------------------------


def test_tool_output_truncation_nested_result_content():
    """result.content inside the raw tool dict is also truncated."""
    from src.core.orchestration.orchestrator import Orchestrator

    orc = Orchestrator.__new__(Orchestrator)
    orc.working_dir = None  # type: ignore[attr-defined]
    orc._session_read_files = set()
    orc._usage_buffer = {}
    orc._step_snapshot_id = None
    orc.rollback_manager = MagicMock()
    orc.event_bus = MagicMock()
    orc.plan_mode = None  # type: ignore[attr-defined]
    orc._plan_mode_approved = None  # type: ignore[attr-defined]
    orc.current_role = None
    orc._tool_executor = None  # type: ignore[attr-defined]

    large_content = "y" * 15_000
    # Tool returns {"ok": True, "result": {"content": large, "lines": 500}}
    tool_fn = MagicMock(
        return_value={"ok": True, "result": {"content": large_content, "lines": 500}}
    )
    orc.tool_registry = MagicMock()
    orc.tool_registry.get = MagicMock(
        return_value={
            "fn": tool_fn,
            "side_effects": [],
            "description": "read tool",
        }
    )

    with (
        patch(
            "src.core.orchestration.orchestrator.get_tool_contract", return_value=None
        ),
        patch(
            "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
            return_value=None,
        ),
        patch("src.core.orchestration.orchestrator.PERMISSION_REQUIRED_TOOLS", set()),
        patch("src.core.orchestration.orchestrator.WRITE_TOOLS_REQUIRING_READ", set()),
    ):
        result = orc.execute_tool(
            {"name": "read_file", "arguments": {"path": "big.txt"}}
        )

    # execute_tool wraps: {"ok": True, "result": raw_res}
    # raw_res["result"] = {"content": truncated, "lines": 500}
    raw = result.get("result", {})
    inner = raw.get("result", {})
    assert "content" in inner
    assert len(inner["content"]) <= 8000 + 100
    assert "truncated" in inner["content"]
    # Other fields not truncated
    assert inner.get("lines") == 500


# ---------------------------------------------------------------------------
# TRUNC-3 — Short results are not truncated
# ---------------------------------------------------------------------------


def test_tool_output_not_truncated_when_short():
    """Results under 8000 chars are returned unchanged."""
    from src.core.orchestration.orchestrator import Orchestrator

    orc = Orchestrator.__new__(Orchestrator)
    orc.working_dir = None  # type: ignore[attr-defined]
    orc._session_read_files = set()
    orc._usage_buffer = {}
    orc._step_snapshot_id = None
    orc.rollback_manager = MagicMock()
    orc.event_bus = MagicMock()
    orc.plan_mode = None  # type: ignore[attr-defined]
    orc._plan_mode_approved = None  # type: ignore[attr-defined]
    orc.current_role = None
    orc._tool_executor = None  # type: ignore[attr-defined]

    short_content = "hello world"
    tool_fn = MagicMock(return_value={"ok": True, "output": short_content})
    orc.tool_registry = MagicMock()
    orc.tool_registry.get = MagicMock(
        return_value={
            "fn": tool_fn,
            "side_effects": [],
            "description": "test tool",
        }
    )

    with (
        patch(
            "src.core.orchestration.orchestrator.get_tool_contract", return_value=None
        ),
        patch(
            "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
            return_value=None,
        ),
        patch("src.core.orchestration.orchestrator.PERMISSION_REQUIRED_TOOLS", set()),
        patch(
            "src.core.orchestration.orchestrator.WRITE_TOOLS_REQUIRING_ERROR",
            set(),
            create=True,
        ),
        patch("src.core.orchestration.orchestrator.WRITE_TOOLS_REQUIRING_READ", set()),
    ):
        result = orc.execute_tool({"name": "test_tool", "arguments": {}})

    # No truncation — raw result is unchanged
    raw = result.get("result", {})
    assert raw.get("output") == short_content


# ---------------------------------------------------------------------------
# STATE-1 — recent_tool_calls in AgentState TypedDict
# ---------------------------------------------------------------------------


def test_recent_tool_calls_in_agent_state():
    """AgentState TypedDict includes recent_tool_calls field."""
    from src.core.orchestration.graph.state import AgentState
    import typing

    hints = typing.get_type_hints(AgentState)
    assert "recent_tool_calls" in hints


# ---------------------------------------------------------------------------
# STATE-2 — recent_tool_calls initialised to [] in initial_state source
# ---------------------------------------------------------------------------


def test_recent_tool_calls_in_initial_state_source():
    """The orchestrator source sets recent_tool_calls to [] in the initial_state dict."""
    import inspect
    import src.core.orchestration.inference_loop as _il_mod
    import src.core.orchestration.orchestrator as _orc_mod

    source = inspect.getsource(_orc_mod) + inspect.getsource(_il_mod)
    # Verify the key is set to an empty list in the initial_state assignment block
    assert '"recent_tool_calls": []' in source
