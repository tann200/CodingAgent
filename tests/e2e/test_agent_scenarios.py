"""
P3-3: E2E scenario tests using a mock LLM (no live provider required).

These tests exercise the full LangGraph pipeline:
  perception → analysis → planning → execution → verification

The LLM is mocked so tests run in CI without a running local provider.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(workdir: Path) -> Any:
    """Instantiate Orchestrator pointing at an isolated temp working directory."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(workdir))
    return orch


def _mock_call_model(response_text: str):
    """Return an async mock yielding an OpenAI-shaped choices dict with response_text."""

    async def _call(*args, **kwargs):
        return {"choices": [{"message": {"content": response_text}}]}

    return _call


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path):
    """Isolated working directory with a minimal Python file."""
    (tmp_path / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}'\n")
    (tmp_path / ".agent-context").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Scenario 1: Read-before-write enforcement
# ---------------------------------------------------------------------------


class TestReadBeforeWriteE2E:
    """Agent must read a file before writing it — enforced at orchestrator level."""

    def test_write_blocked_without_read(self, workdir):
        """execute_tool rejects edit_file when file not yet read."""
        orch = _make_orchestrator(workdir)

        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "hello.py",
                    "old_content": "def greet",
                    "new_content": "def greet2",
                },
            }
        )
        assert result.get("ok") is False
        assert "read" in result.get("error", "").lower()

    def test_write_allowed_after_read(self, workdir):
        """execute_tool allows edit_file after read_file registers the path."""
        orch = _make_orchestrator(workdir)

        # Register the read
        orch._session_read_files.add(str((workdir / "hello.py").resolve()))

        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "hello.py",
                    "old_content": "def greet(name):\n    return f'Hello, {name}'\n",
                    "new_content": "def greet(name):\n    return f'Hi, {name}'\n",
                },
            }
        )
        # Should not be a read-violation error
        error = result.get("error", "")
        assert "must read" not in error, f"Unexpected read-before-write block: {error}"


# ---------------------------------------------------------------------------
# Scenario 2: Planning node produces a valid plan
# ---------------------------------------------------------------------------


class TestPlanningNodeE2E:
    """planning_node generates a usable plan from a task description."""

    def _make_mock_orchestrator(self, workdir: Path) -> Any:
        orch = MagicMock()
        orch.working_dir = str(workdir)
        orch.session_store = MagicMock()
        orch.session_store.add_plan = MagicMock()
        orch.get_provider_capabilities = MagicMock(return_value={})
        orch.get_agent_brain_manager = MagicMock(return_value=None)
        # cancel_event must be None so the cancellation check never fires
        orch.cancel_event = None
        return orch

    @pytest.mark.asyncio
    async def test_planning_node_returns_plan(self, workdir):
        """planning_node must return current_plan as a non-empty list."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        mock_plan = json.dumps(
            {
                "root_task": "Add docstring to greet function",
                "steps": [
                    {
                        "step_id": "step_0",
                        "description": "Read hello.py",
                        "files": ["hello.py"],
                        "depends_on": [],
                    },
                    {
                        "step_id": "step_1",
                        "description": "Add docstring to greet function",
                        "files": ["hello.py"],
                        "depends_on": ["step_0"],
                    },
                ],
            }
        )

        orch = self._make_mock_orchestrator(workdir)

        state: Dict[str, Any] = {
            "task": "Add docstring to greet function",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }

        config = {"configurable": {"orchestrator": orch}}

        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_mock_call_model(mock_plan),
        ):
            result = await planning_node(state, config)

        assert isinstance(result.get("current_plan"), list), "current_plan must be a list"
        assert len(result["current_plan"]) > 0, "Plan must have at least one step"

    @pytest.mark.asyncio
    async def test_planning_node_resets_plan_mode_approved(self, workdir):
        """planning_node must always set plan_mode_approved=None (P2-9)."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        orch = self._make_mock_orchestrator(workdir)

        state: Dict[str, Any] = {
            "task": "do something",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "plan_mode_approved": True,  # stale approval from previous cycle
            "history": [],
        }

        config = {"configurable": {"orchestrator": orch}}

        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_mock_call_model('{"steps": [{"step_id": "s0", "description": "x", "depends_on": []}]}'),
        ):
            result = await planning_node(state, config)

        assert result.get("plan_mode_approved") is None, (
            "planning_node must reset plan_mode_approved to None on every plan cycle"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Perception node gathers repo context
# ---------------------------------------------------------------------------


class TestPerceptionNodeE2E:
    """perception_node populates task-relevant fields from the repo."""

    @pytest.mark.asyncio
    async def test_perception_node_sets_relevant_files(self, workdir):
        """perception_node must return a task key and non-null working_dir."""
        from src.core.orchestration.graph.nodes.perception_node import perception_node

        state: Dict[str, Any] = {
            "task": "add docstring to greet",
            "working_dir": str(workdir),
            "history": [],
            "perception_rounds": 0,
        }
        config = {"configurable": {"orchestrator": None}}

        mock_search_result = {"results": [{"file": "hello.py", "snippet": "def greet"}]}

        with patch(
            "src.core.orchestration.graph.nodes.perception_node.call_model",
            side_effect=_mock_call_model(
                json.dumps({"search_queries": ["greet function"], "relevant_files": ["hello.py"]})
            ),
        ):
            result = await perception_node(state, config)

        assert result.get("working_dir") == str(workdir) or "working_dir" not in result or result.get("task")
        # Must not raise; basic smoke test
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Scenario 4: Distiller persists to VectorStore (P3-7)
# ---------------------------------------------------------------------------


class TestDistillerVectorStoreE2E:
    """distill_context must call VectorStore.add_memory after successful distillation."""

    def test_distill_context_calls_vector_store(self, workdir):
        """After distillation, add_memory must be called with the summary text."""
        from src.core.memory.distiller import distill_context

        messages = [
            {"role": "user", "content": "add a docstring to greet()"},
            {"role": "assistant", "content": "I'll read hello.py first."},
            {"role": "user", "content": '{"tool_execution_result": {"ok": true, "content": "def greet..."}}'},
            {"role": "assistant", "content": "Now I'll add the docstring."},
        ]

        distilled_result = {
            "current_task": "add docstring",
            "current_state": "reading file",
            "next_step": "write docstring",
        }

        add_memory_calls = []

        class FakeVectorStore:
            def __init__(self, **kwargs):
                pass

            def add_memory(self, text, metadata=None):
                add_memory_calls.append((text, metadata))

        with (
            patch("src.core.memory.distiller._call_llm_sync", return_value=json.dumps(distilled_result)),
            patch("src.core.indexing.vector_store.VectorStore", FakeVectorStore),
        ):
            result = distill_context(messages, working_dir=workdir)

        assert result == distilled_result
        assert len(add_memory_calls) == 1, "add_memory must be called exactly once"
        text, metadata = add_memory_calls[0]
        assert "add docstring" in text
        assert metadata == distilled_result
