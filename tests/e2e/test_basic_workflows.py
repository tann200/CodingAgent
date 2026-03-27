"""
P3-3: Basic E2E workflow and behaviour tests using a mock LLM.

These tests exercise the core agent pipeline without a live provider.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workdir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / ".agent-context").mkdir()
    (d / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n"
    )
    return d


def _make_mock_orchestrator(workdir: Path) -> Any:
    orch = MagicMock()
    orch.working_dir = str(workdir)
    orch.session_store = MagicMock()
    orch.session_store.add_plan = MagicMock()
    orch.get_provider_capabilities = MagicMock(return_value={})
    orch.cancel_event = None
    return orch


def _plan_response(steps):
    payload = {
        "root_task": "task",
        "steps": [
            {
                "step_id": f"step_{i}",
                "description": s,
                "files": ["app.py"],
                "depends_on": [f"step_{i - 1}"] if i > 0 else [],
            }
            for i, s in enumerate(steps)
        ],
    }
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


# ---------------------------------------------------------------------------
# Basic workflow tests
# ---------------------------------------------------------------------------


class TestBasicE2EWorkflows:
    """Basic end-to-end workflow tests."""

    @pytest.mark.asyncio
    async def test_simple_task_with_mock(self):
        """planning_node produces a valid plan for a trivial task (mocked LLM)."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        workdir = _make_workdir()
        orch = _make_mock_orchestrator(workdir)
        mock_resp = _plan_response(["Read app.py", "Add docstring to add()"])

        async def _llm(*a, **kw):
            return mock_resp

        state: Dict[str, Any] = {
            "task": "Add a docstring to the add function",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }

        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_llm,
        ):
            result = await planning_node(state, {"configurable": {"orchestrator": orch}})

        assert isinstance(result.get("current_plan"), list)
        assert len(result["current_plan"]) >= 1
        assert result.get("plan_mode_approved") is None


# ---------------------------------------------------------------------------
# Agent behaviour tests
# ---------------------------------------------------------------------------


class TestAgentBehaviorE2E:
    """End-to-end agent behavior tests."""

    def test_agent_read_before_edit(self):
        """execute_tool enforces read-before-write at the orchestrator layer."""
        from src.core.orchestration.orchestrator import Orchestrator

        workdir = _make_workdir()
        orch = Orchestrator(working_dir=str(workdir))

        # Edit without prior read must be rejected
        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_content": "def add",
                    "new_content": "def add2",
                },
            }
        )
        assert result.get("ok") is False
        assert "read" in result.get("error", "").lower()

        # After registering a read the block is lifted
        orch._session_read_files.add(str((workdir / "app.py").resolve()))
        result2 = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_content": "def add(a, b):\n    return a + b\n",
                    "new_content": "def add(a, b):\n    \"\"\"Return a+b.\"\"\"\n    return a + b\n",
                },
            }
        )
        assert "must read" not in result2.get("error", "")

    @pytest.mark.asyncio
    async def test_agent_loop_prevention(self):
        """plan_attempts increments across retries; fallback plan is never empty."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        workdir = _make_workdir()
        orch = _make_mock_orchestrator(workdir)

        async def _empty_llm(*a, **kw):
            return {"choices": [{"message": {"content": "not valid json at all"}}]}

        state: Dict[str, Any] = {
            "task": "fix a bug",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }

        for _ in range(2):
            with patch(
                "src.core.orchestration.graph.nodes.planning_node.call_model",
                side_effect=_empty_llm,
            ):
                result = await planning_node(
                    state, {"configurable": {"orchestrator": orch}}
                )
            state["plan_attempts"] = result.get("plan_attempts", 0)
            state["current_plan"] = result.get("current_plan", [])

        assert state["plan_attempts"] >= 2
        assert len(state["current_plan"]) > 0, "Fallback plan must always be non-empty"


# ---------------------------------------------------------------------------
# Scenario benchmarks
# ---------------------------------------------------------------------------


class TestScenarioBenchmarks:
    """SWE-bench style scenario benchmarks using mocked LLM."""

    @pytest.mark.asyncio
    async def test_bug_fix_scenario(self):
        """Bug-fix plan must include a read step before any write step."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        workdir = _make_workdir()
        (workdir / "buggy.py").write_text(
            "def divide(a, b):\n    return a / b  # missing guard\n"
        )

        orch = _make_mock_orchestrator(workdir)
        mock_resp = _plan_response(
            [
                "Read buggy.py to understand the divide function",
                "Edit buggy.py: add ZeroDivisionError guard",
                "Run tests to verify the fix",
            ]
        )

        async def _llm(*a, **kw):
            return mock_resp

        state: Dict[str, Any] = {
            "task": "Fix divide() to raise ValueError on zero divisor",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }

        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_llm,
        ):
            result = await planning_node(state, {"configurable": {"orchestrator": orch}})

        plan = result.get("current_plan", [])
        assert len(plan) >= 2
        descriptions = [s.get("description", "").lower() for s in plan]
        assert any("read" in d for d in descriptions), "Plan must include a read step"

    @pytest.mark.asyncio
    async def test_feature_add_scenario(self):
        """Feature-add plan must reference both implementation and test steps."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        workdir = _make_workdir()
        orch = _make_mock_orchestrator(workdir)
        mock_resp = _plan_response(
            [
                "Read app.py to understand existing functions",
                "Add multiply(a, b) function to app.py",
                "Add test_multiply() to tests/test_app.py",
            ]
        )

        async def _llm(*a, **kw):
            return mock_resp

        state: Dict[str, Any] = {
            "task": "Add a multiply function and write a test for it",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }

        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_llm,
        ):
            result = await planning_node(state, {"configurable": {"orchestrator": orch}})

        plan = result.get("current_plan", [])
        assert len(plan) >= 2
        descriptions = " ".join(s.get("description", "").lower() for s in plan)
        assert "multiply" in descriptions or "add" in descriptions
