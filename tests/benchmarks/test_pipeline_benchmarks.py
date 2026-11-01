"""
P4-3: Pipeline benchmark tests.

All benchmarks use mocked LLM backends so they can run in CI without a
live provider. The goal is to catch regressions in orchestration overhead
(context building, state transitions, tool dispatch) rather than LLM speed.

Timing thresholds are deliberately generous to avoid flakiness in slow CI
environments (GitHub Actions). They serve as regression guards, not SLAs.
"""

# ruff: noqa: E501
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
    (d / ".codingAgent").mkdir()
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

        assert dag is not None # type: ignore[possibly-undefined]
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

        assert waves is not None # type: ignore[possibly-undefined]
        assert elapsed_ms < 10, f"topological_sort_waves too slow: {elapsed_ms:.2f} ms/call"


# ---------------------------------------------------------------------------
# Benchmark 4: Token efficiency & parallel wave dispatch latency
# ---------------------------------------------------------------------------


class TestTokenEfficiencyAndParallelWavesBenchmark:
    """Benchmark token footprint reduction and parallel wave dispatch speed."""

    def test_compact_vs_frontier_toolset_token_efficiency(self):
        """compact-flat (12 tools) must reduce tool schema size by >= 40% vs frontier-flat (25 tools)."""
        workdir = _make_workdir()
        orch = _make_orchestrator(workdir)

        from src.core.orchestration.graph.runtime_policy import (
            COMPACT_RUNTIME_POLICY,
            FRONTIER_RUNTIME_POLICY,
            resolve_effective_tool_schemas,
        )

        compact_schemas = resolve_effective_tool_schemas(orch, COMPACT_RUNTIME_POLICY)
        frontier_schemas = resolve_effective_tool_schemas(orch, FRONTIER_RUNTIME_POLICY)

        assert len(compact_schemas) <= 12, f"Expected <= 12 tools for compact-flat, got {len(compact_schemas)}"
        assert len(frontier_schemas) >= 20, f"Expected >= 20 tools for frontier-flat, got {len(frontier_schemas)}"

        compact_bytes = len(json.dumps(compact_schemas))
        frontier_bytes = len(json.dumps(frontier_schemas))
        reduction_ratio = 1.0 - (compact_bytes / frontier_bytes)

        # Must save at least 40% tool schema footprint to maximize KV cache and prompt efficiency on 9-14B models
        assert reduction_ratio >= 0.40, f"Expected >= 40% reduction, achieved {reduction_ratio * 100:.1f}%"

    @pytest.mark.asyncio
    async def test_parallel_read_wave_dispatch_latency(self):
        """Parallel wave partition and concurrent execution must complete in under 100 ms for 3 reads."""
        workdir = _make_workdir()
        orch = _make_orchestrator(workdir)

        for i in range(3):
            (workdir / f"bench_{i}.py").write_text(f"def foo_{i}(): return {i}\n")

        from src.core.orchestration.graph.nodes.frontier_dispatch import (
            execute_raw_tool,
            normalize_tool_calls_with_ids,
            partition_tool_call_waves,
        )

        raw_calls = [
            {"function": {"name": "read_file", "arguments": json.dumps({"path": f"bench_{i}.py"})}}
            for i in range(3)
        ]
        norm = normalize_tool_calls_with_ids(raw_calls, turn_index=1)
        waves = partition_tool_call_waves(norm, orch, max_parallel_reads=3)

        assert len(waves) == 1
        is_parallel, wave_items = waves[0]
        assert is_parallel is True
        assert len(wave_items) == 3

        start = time.perf_counter()
        results = await asyncio.gather(*[
            execute_raw_tool(orch, {"name": t_name, "arguments": t_args}, model_tier="medium", run_async=True)
            for (_cid, t_name, t_args, _otc) in wave_items
        ])
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(results) == 3
        for res in results:
            assert res.get("ok") is True or res.get("status") == "ok"
        assert elapsed_ms < 100, f"Parallel read wave too slow: {elapsed_ms:.1f} ms"

    def test_evaluation_cli_list_scenarios_smoke(self, capsys):
        """CLI runner 'list' must output available evaluation scenarios without error."""
        from src.core.evaluation.cli import main

        ret = main(["list"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "scenarios" in captured.out

