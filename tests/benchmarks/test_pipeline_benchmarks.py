"""
P4-3: Pipeline benchmark tests.

All benchmarks use mocked LLM backends so they can run in CI without a
live provider. The goal is to catch regressions in orchestration overhead
(context building, state transitions, tool dispatch) rather than LLM speed.

Timing thresholds are deliberately generous to avoid flakiness in slow CI
environments (GitHub Actions). They serve as regression guards, not SLAs.
"""
from __future__ import annotations

import asyncio
import json
import time
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
    (d / "main.py").write_text("def main():\n    pass\n")
    return d


def _make_orchestrator(workdir: Path) -> Any:
    from src.core.orchestration.orchestrator import Orchestrator
    return Orchestrator(working_dir=str(workdir))


def _mock_plan_response(n_steps: int = 3) -> Dict:
    steps = [
        {
            "step_id": f"step_{i}",
            "description": f"Step {i}: do something",
            "files": ["main.py"],
            "depends_on": [f"step_{i - 1}"] if i > 0 else [],
        }
        for i in range(n_steps)
    ]
    return {"choices": [{"message": {"content": json.dumps({"root_task": "bench", "steps": steps})}}]}


# ---------------------------------------------------------------------------
# Benchmark 1: Orchestrator tool dispatch overhead
# ---------------------------------------------------------------------------


class TestToolDispatchBenchmark:
    """Measure per-call overhead of execute_tool (no LLM involved)."""

    def test_read_file_dispatch_latency(self):
        """read_file dispatch must complete under 200 ms for an existing file."""
        workdir = _make_workdir()
        orch = _make_orchestrator(workdir)

        start = time.perf_counter()
        for _ in range(10):
            orch.execute_tool({"name": "read_file", "arguments": {"path": "main.py"}})
        elapsed_ms = (time.perf_counter() - start) * 1000 / 10

        assert elapsed_ms < 200, f"read_file dispatch too slow: {elapsed_ms:.1f} ms/call"

    def test_unknown_tool_dispatch_latency(self):
        """Unknown-tool error path must return under 50 ms."""
        workdir = _make_workdir()
        orch = _make_orchestrator(workdir)

        start = time.perf_counter()
        for _ in range(20):
            orch.execute_tool({"name": "nonexistent_tool", "arguments": {}})
        elapsed_ms = (time.perf_counter() - start) * 1000 / 20

        assert elapsed_ms < 50, f"Unknown-tool dispatch too slow: {elapsed_ms:.1f} ms/call"

    def test_read_before_write_check_latency(self):
        """Read-before-write enforcement must add less than 5 ms overhead per call."""
        workdir = _make_workdir()
        orch = _make_orchestrator(workdir)

        start = time.perf_counter()
        for _ in range(50):
            orch.execute_tool(
                {
                    "name": "edit_file",
                    "arguments": {"path": "main.py", "old_content": "x", "new_content": "y"},
                }
            )
        elapsed_ms = (time.perf_counter() - start) * 1000 / 50

        assert elapsed_ms < 50, f"Read-before-write check too slow: {elapsed_ms:.1f} ms/call"


# ---------------------------------------------------------------------------
# Benchmark 2: Planning node overhead (mocked LLM)
# ---------------------------------------------------------------------------


class TestPlanningBenchmark:
    """Measure planning_node latency with a mocked call_model."""

    @pytest.mark.asyncio
    async def test_planning_node_latency_3_steps(self):
        """planning_node must return within 500 ms for a 3-step plan (mocked LLM)."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        workdir = _make_workdir()
        mock_response = _mock_plan_response(3)

        orch = MagicMock()
        orch.working_dir = str(workdir)
        orch.session_store = MagicMock()
        orch.get_provider_capabilities = MagicMock(return_value={})
        orch.cancel_event = None

        async def _fast_model(*args, **kwargs):
            return mock_response

        state: Dict[str, Any] = {
            "task": "Refactor main function",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }
        config = {"configurable": {"orchestrator": orch}}

        start = time.perf_counter()
        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_fast_model,
        ):
            result = await planning_node(state, config)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 500, f"planning_node too slow: {elapsed_ms:.1f} ms"
        assert len(result.get("current_plan", [])) > 0

    @pytest.mark.asyncio
    async def test_planning_node_latency_10_steps(self):
        """planning_node must return within 1000 ms for a 10-step plan (mocked LLM)."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        workdir = _make_workdir()
        mock_response = _mock_plan_response(10)

        orch = MagicMock()
        orch.working_dir = str(workdir)
        orch.session_store = MagicMock()
        orch.get_provider_capabilities = MagicMock(return_value={})
        orch.cancel_event = None

        async def _fast_model(*args, **kwargs):
            return mock_response

        state: Dict[str, Any] = {
            "task": "Big refactor with 10 steps",
            "working_dir": str(workdir),
            "current_plan": [],
            "current_step": 0,
            "task_decomposed": False,
            "plan_attempts": 0,
            "history": [],
        }
        config = {"configurable": {"orchestrator": orch}}

        start = time.perf_counter()
        with patch(
            "src.core.orchestration.graph.nodes.planning_node.call_model",
            side_effect=_fast_model,
        ):
            result = await planning_node(state, config)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 1000, f"planning_node 10-step plan too slow: {elapsed_ms:.1f} ms"
        assert len(result.get("current_plan", [])) == 10


# ---------------------------------------------------------------------------
# Benchmark 3: DAG parser throughput
# ---------------------------------------------------------------------------


class TestDAGParserBenchmark:
    """Measure DAG parsing throughput for common plan sizes."""

    def test_dag_parse_throughput_small(self):
        """_parse_dag_content must parse a 5-step plan in under 5 ms."""
        from src.core.orchestration.dag_parser import _parse_dag_content

        content = json.dumps(
            {
                "root_task": "bench",
                "steps": [
                    {
                        "step_id": f"step_{i}",
                        "description": f"Step {i}",
                        "files": ["a.py"],
                        "depends_on": [f"step_{i - 1}"] if i > 0 else [],
                    }
                    for i in range(5)
                ],
            }
        )

        start = time.perf_counter()
        for _ in range(100):
            dag = _parse_dag_content(content)
        elapsed_ms = (time.perf_counter() - start) * 1000 / 100

        assert dag is not None
        assert elapsed_ms < 5, f"DAG parse too slow: {elapsed_ms:.2f} ms/call"

    def test_dag_topological_sort_throughput(self):
        """topological_sort_waves must finish a 20-step chain in under 10 ms."""
        from src.core.orchestration.dag_parser import _parse_dag_content

        content = json.dumps(
            {
                "root_task": "bench",
                "steps": [
                    {
                        "step_id": f"step_{i}",
                        "description": f"Step {i}",
                        "files": [f"f{i}.py"],
                        "depends_on": [f"step_{i - 1}"] if i > 0 else [],
                    }
                    for i in range(20)
                ],
            }
        )

        dag = _parse_dag_content(content)
        assert dag is not None

        start = time.perf_counter()
        for _ in range(200):
            waves = dag.topological_sort_waves()
        elapsed_ms = (time.perf_counter() - start) * 1000 / 200

        assert waves is not None
        assert elapsed_ms < 10, f"topological_sort_waves too slow: {elapsed_ms:.2f} ms/call"
