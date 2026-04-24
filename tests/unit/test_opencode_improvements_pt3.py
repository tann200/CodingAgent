"""
Regression tests for third batch of opencode-inspired improvements:

  OVERFLOW-1 — context.overflow event fired when prompt_tokens >= budget - reserve
  OVERFLOW-2 — _budget_compaction / _should_distill set in perception return when overflow
  OVERFLOW-3 — no overflow flag when prompt_tokens is within budget
  DELEGATE-1 — delegation result text truncated at 2000 chars
  DELEGATE-2 — delegation.complete event fired after injection
  EXPLORE-1  — explore_mode=True blocks write tools in execute_tool
  EXPLORE-2  — explore_mode=False allows write tools through
  EXPLORE-3  — explore_mode=True allows read tools through
"""


# ruff: noqa: E501
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orc_for_execute(tool_result=None) -> MagicMock:
    orc = MagicMock()
    orc.tool_registry = MagicMock()
    orc.tool_registry.get = MagicMock(return_value=None)
    orc.preflight_check = MagicMock(return_value={"ok": True})
    orc.execute_tool = MagicMock(return_value=tool_result or {"ok": True, "result": {}})
    orc._session_read_files = set()
    orc.event_bus = MagicMock()
    orc.plan_mode = None
    orc._plan_mode_approved = None
    orc.current_role = None
    orc.cancel_event = None
    orc.explore_mode = False
    return orc


# ---------------------------------------------------------------------------
# OVERFLOW-1/2/3 — context overflow detection in perception_node
# ---------------------------------------------------------------------------


def _make_perception_state(**kwargs) -> Dict[str, Any]:
    base = {
        "task": "test",
        "working_dir": "/tmp",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": None,
        "rounds": 0,
        "system_prompt": "",
        "errors": [],
        "session_id": "s1",
        "current_plan": None,
        "current_step": 0,
        "tool_call_count": 0,
        "empty_response_count": 0,
        "cancel_event": None,
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_context_overflow_sets_compaction_flag():
    """When prompt_tokens >= budget - reserve, _budget_compaction and _should_distill are set."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    events: list = []

    orc = MagicMock()
    orc.adapter = MagicMock()
    orc.cancel_event = None
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))
    orc.msg_mgr = MagicMock()
    orc.token_monitor = None

    # Simulate a response where prompt_tokens is very high
    _BUDGET = 8000
    _RESERVE = 4096
    _HIGH_TOKENS = _BUDGET - _RESERVE + 100  # just over the limit

    fake_response = {
        "choices": [
            {"message": {"content": "name: read_file\narguments:\n  path: foo.py"}}
        ],
        "prompt_tokens": _HIGH_TOKENS,
        "completion_tokens": 50,
        "total_tokens": _HIGH_TOKENS + 50,
    }

    state = _make_perception_state()
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            return_value=fake_response,
        ),
        patch("src.core.orchestration.graph.nodes.perception_node.ContextBuilder"),
        patch(
            "src.core.inference.provider_context.get_actual_context_window",
            return_value=_BUDGET,
        ),
    ):
        result = await perception_node(state, config)

    assert result.get("_budget_compaction") is True
    assert result.get("_should_distill") is True


@pytest.mark.asyncio
async def test_context_overflow_fires_event():
    """context.overflow event is published when overflow is detected."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    events: list = []

    orc = MagicMock()
    orc.adapter = MagicMock()
    orc.cancel_event = None
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))
    orc.msg_mgr = MagicMock()
    orc.token_monitor = None

    _BUDGET = 8000
    _RESERVE = 4096
    _HIGH_TOKENS = _BUDGET - _RESERVE + 1

    fake_response = {
        "choices": [
            {"message": {"content": "name: read_file\narguments:\n  path: x.py"}}
        ],
        "prompt_tokens": _HIGH_TOKENS,
        "completion_tokens": 10,
        "total_tokens": _HIGH_TOKENS + 10,
    }

    state = _make_perception_state()
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            return_value=fake_response,
        ),
        patch("src.core.orchestration.graph.nodes.perception_node.ContextBuilder"),
        patch(
            "src.core.inference.provider_context.get_actual_context_window",
            return_value=_BUDGET,
        ),
    ):
        await perception_node(state, config)

    overflow_events = [p for (e, p) in events if e == "context.overflow"]
    assert overflow_events, "context.overflow event not fired"


@pytest.mark.asyncio
async def test_no_overflow_when_within_budget():
    """No compaction flag when prompt_tokens is safely within budget."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    orc = MagicMock()
    orc.adapter = MagicMock()
    orc.cancel_event = None
    orc.event_bus = MagicMock()
    orc.event_bus.publish = MagicMock()
    orc.msg_mgr = MagicMock()
    orc.token_monitor = None

    _BUDGET = 8000
    _LOW_TOKENS = 1000  # well within budget

    fake_response = {
        "choices": [
            {"message": {"content": "name: glob\narguments:\n  pattern: '**/*.py'"}}
        ],
        "prompt_tokens": _LOW_TOKENS,
        "completion_tokens": 20,
        "total_tokens": 1020,
    }

    state = _make_perception_state()
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            return_value=fake_response,
        ),
        patch("src.core.orchestration.graph.nodes.perception_node.ContextBuilder"),
        patch(
            "src.core.inference.provider_context.get_actual_context_window",
            return_value=_BUDGET,
        ),
    ):
        result = await perception_node(state, config)

    assert not result.get("_budget_compaction")
    assert not result.get("_should_distill")


# ---------------------------------------------------------------------------
# DELEGATE-1/2 — delegation result injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegation_result_truncated_at_2000_chars():
    """Delegation results longer than 2000 chars are truncated with a notice."""
    from src.core.orchestration.graph.nodes.delegation_node import delegation_node

    long_result = "x" * 5000
    fake_delegations = [
        {"role": "analyst", "task": "analyze", "result_key": "analysis"}
    ]

    orc = MagicMock()
    orc.event_bus = MagicMock()
    orc.event_bus.publish = MagicMock()

    state: Dict[str, Any] = {
        "delegations": fake_delegations,
        "working_dir": "/tmp",
        "session_id": "s1",
        "delegation_depth": 0,
        "_file_lock_manager": None,
    }

    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            return_value=long_result,
        ),
        patch(
            "src.core.orchestration.graph.nodes.delegation_node._resolve_orchestrator",
            return_value=orc,
        ),
    ):
        result = await delegation_node(state, config)

    history = result.get("history", [])
    assert history, "No history injected"
    content = history[0]["content"]
    assert "truncated" in content
    # The injected text must be well under 2500 chars (2000 result + overhead)
    assert len(content) < 3000


@pytest.mark.asyncio
async def test_delegation_complete_event_published():
    """delegation.complete event is published after successful injection."""
    from src.core.orchestration.graph.nodes.delegation_node import delegation_node

    events: list = []
    orc = MagicMock()
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))

    state: Dict[str, Any] = {
        "delegations": [
            {"role": "analyst", "task": "find files", "result_key": "files"}
        ],
        "working_dir": "/tmp",
        "session_id": "s2",
        "delegation_depth": 0,
        "_file_lock_manager": None,
    }

    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(
            "src.core.orchestration.graph.nodes.delegation_node.delegate_task_async",
            return_value="found: foo.py bar.py",
        ),
        patch(
            "src.core.orchestration.graph.nodes.delegation_node._resolve_orchestrator",
            return_value=orc,
        ),
    ):
        await delegation_node(state, config)

    event_names = [e for (e, _) in events]
    assert "delegation.complete" in event_names


# ---------------------------------------------------------------------------
# EXPLORE-1/2/3 — explore mode in orchestrator.execute_tool
# ---------------------------------------------------------------------------


def _make_bare_orchestrator() -> Any:
    """Return a minimal Orchestrator instance with explore_mode support."""
    from src.core.orchestration.orchestrator import Orchestrator

    orc = Orchestrator.__new__(Orchestrator)
    orc.working_dir = None  # type: ignore[attr-defined]
    orc._session_read_files = set()  # type: ignore[misc]
    orc._usage_buffer = {}  # type: ignore[attr-defined]
    orc._step_snapshot_id = None  # type: ignore[attr-defined]
    orc.rollback_manager = MagicMock()  # type: ignore[attr-defined]
    orc.event_bus = MagicMock()
    orc.plan_mode = None  # type: ignore[attr-defined]
    orc._plan_mode_approved = None  # type: ignore[attr-defined]
    orc.current_role = None  # type: ignore[attr-defined]
    orc._tool_executor = None  # type: ignore[attr-defined]
    orc._permission_gate = None  # type: ignore[attr-defined]
    orc._permission_granted = False  # type: ignore[attr-defined]
    orc.explore_mode = False  # type: ignore[attr-defined]
    return orc


def test_explore_mode_blocks_write_tool():
    """explore_mode=True rejects write tools before execution."""
    orc = _make_bare_orchestrator()
    orc.explore_mode = True

    with (
        patch("src.core.orchestration.orchestrator.PERMISSION_REQUIRED_TOOLS", set()),
        patch("src.core.orchestration.orchestrator.WRITE_TOOLS_REQUIRING_READ", set()),
    ):
        result = orc.execute_tool(
            {"name": "write_file", "arguments": {"path": "x.py", "content": ""}}
        )

    assert result["ok"] is False
    assert "explore mode" in result["error"].lower()


def test_explore_mode_off_does_not_block():
    """explore_mode=False lets tools pass through to normal execution."""
    orc = _make_bare_orchestrator()
    orc.explore_mode = False

    tool_fn = MagicMock(return_value={"ok": True, "result": {}})
    orc.tool_registry = MagicMock()
    orc.tool_registry.get = MagicMock(
        return_value={
            "fn": tool_fn,
            "side_effects": [],
            "description": "write tool",
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
        result = orc.execute_tool({"name": "write_file", "arguments": {}})

    assert result.get("ok") is True


def test_explore_mode_allows_read_tool():
    """explore_mode=True allows read_file through."""
    orc = _make_bare_orchestrator()
    orc.explore_mode = True

    tool_fn = MagicMock(return_value={"ok": True, "result": {"content": "hello"}})
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
            {"name": "read_file", "arguments": {"path": "foo.py"}}
        )

    assert result.get("ok") is True
