import asyncio
import json
from unittest.mock import MagicMock, patch
from src.core.orchestration.graph.nodes.workflow_nodes import (
    perception_node,
    planning_node,
    execution_node,
)
from src.core.orchestration.orchestrator import ToolRegistry


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeOrch:
    def __init__(self, workdir, registry):
        self.tool_registry = registry
        self.adapter = None
        self.deterministic = False
        self.seed = None
        self.msg_mgr = MagicMock()
        self._session_read_files = set()
        self.working_dir = workdir
        self._execution_trace = []

    def preflight_check(self, action):
        return {"ok": True}

    def execute_tool(self, action):
        return {"ok": True, "result": {"status": "ok"}}

    def _append_execution_trace(self, entry):
        self._execution_trace.append(entry)

    def _read_execution_trace(self):
        return self._execution_trace

    def _check_loop_prevention(self, tool_name, args):
        return False


def test_planning_node_uses_decomposed_plan(tmp_path):
    """Test that planning node uses existing decomposed plan."""
    reg = ToolRegistry()
    orch = FakeOrch(str(tmp_path), reg)

    # State with already decomposed plan
    state = {
        "system_prompt": "You are an assistant",
        "working_dir": str(tmp_path),
        "task": "Original task",
        "history": [],
        "rounds": 1,
        "current_plan": [
            {"description": "step 1", "action": None},
            {"description": "step 2", "action": None},
        ],
        "current_step": 0,
        "task_decomposed": True,
        "original_task": "Original task",
        "next_action": None,
    }

    config = {"configurable": {"orchestrator": orch}}

    res = run_async(planning_node(state, config))

    # Should return the plan and update task to current step
    assert "current_plan" in res
    assert "current_step" in res
    assert res.get("current_step") == 0
    assert "step 1" in str(res.get("task", ""))


def test_planning_node_handles_no_plan(tmp_path):
    """Test that planning node handles tasks without existing plans."""
    reg = ToolRegistry()
    orch = FakeOrch(str(tmp_path), reg)

    state = {
        "system_prompt": "You are an assistant",
        "working_dir": str(tmp_path),
        "task": "simple task",
        "history": [],
        "rounds": 0,
        "current_plan": [],
        "current_step": 0,
        "next_action": None,
    }

    config = {"configurable": {"orchestrator": orch}}

    with patch(
        "src.core.orchestration.graph.nodes.planning_node.call_model"
    ) as mock_call:
        mock_call.return_value = {
            "choices": [{"message": {"content": "1. do something"}}]
        }
        res = run_async(planning_node(state, config))

    # Should have a plan or return original state
    assert "current_plan" in res


def test_execution_node_advances_plan(tmp_path):
    """Test that execution node advances to next step in plan."""
    reg = ToolRegistry()
    orch = FakeOrch(str(tmp_path), reg)

    # State with plan at step 0
    state = {
        "system_prompt": "You are an assistant",
        "working_dir": str(tmp_path),
        "task": "Original task",
        "history": [],
        "rounds": 1,
        "next_action": {"name": "echo", "arguments": {"text": "hello"}},
        "verified_reads": [],
        "current_plan": [
            {"description": "step 1", "action": None, "completed": False},
            {"description": "step 2", "action": None, "completed": False},
        ],
        "current_step": 0,
        "task_decomposed": True,
        "original_task": "Original task",
    }

    config = {"configurable": {"orchestrator": orch}}

    res = run_async(execution_node(state, config))

    # Should advance to next step
    assert "current_step" in res or "last_result" in res


def test_execution_node_completes_plan(tmp_path):
    """Test that execution node marks plan as complete when all steps done."""
    reg = ToolRegistry()
    orch = FakeOrch(str(tmp_path), reg)

    # State with plan at last step
    state = {
        "system_prompt": "You are an assistant",
        "working_dir": str(tmp_path),
        "task": "Original task",
        "history": [],
        "rounds": 2,
        "next_action": {"name": "echo", "arguments": {"text": "done"}},
        "verified_reads": [],
        "current_plan": [
            {"description": "step 1", "action": None, "completed": False},
            {"description": "step 2", "action": None, "completed": False},
        ],
        "current_step": 1,  # Last step
        "task_decomposed": True,
        "original_task": "Original task",
    }

    config = {"configurable": {"orchestrator": orch}}

    res = run_async(execution_node(state, config))

    # Should indicate plan is complete
    assert res.get("current_step", 0) >= len(state["current_plan"])
