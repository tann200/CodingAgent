"""
Regression tests for Vol29 audit fixes.

Covers:
  CF-1  perception_node success path clears errors (stale context_overflow persists)
  CF-1b memory_update_node clears errors on return
  HR-1  distiller uses module-level singleton executor (no per-call ThreadPoolExecutor)
  HR-2  execution_trace capped at 2000 entries on flush (unbounded growth)
"""


# ruff: noqa: E501
import inspect


class TestCf1ErrorsClearedInPerceptionNode:
    """CF-1: perception_node success return must include errors=[]."""

    def test_success_path_sets_errors_empty(self):
        """The main success return dict in perception_node must set errors=[]."""
        import src.core.orchestration.graph.nodes.perception_node as pn

        src_text = inspect.getsource(pn)
        # Find the primary result dict — it should include "errors": []
        # We verify both that "errors" appears in the result assignment and
        # that the value is an empty list (not a non-empty list).
        assert '"errors": []' in src_text, (
            "CF-1: perception_node success path must set 'errors': [] "
            "to prevent stale errors from prior turns leaking into next round"
        )

    def test_errors_cleared_adjacent_to_next_action(self):
        """errors=[] must be in the same result dict as 'next_action': tool_call."""
        import src.core.orchestration.graph.nodes.perception_node as pn

        src_text = inspect.getsource(pn)
        # Both '"next_action": tool_call' and '"errors": []' must exist
        assert '"next_action": tool_call' in src_text, (
            "Could not find 'next_action': tool_call in perception_node main result"
        )
        assert '"errors": []' in src_text, (
            "CF-1: errors=[] must appear in perception_node (success path clears errors)"
        )
        # They should be within 500 characters of each other
        na_idx = src_text.rfind('"next_action": tool_call')
        err_idx = src_text.rfind('"errors": []')
        assert abs(na_idx - err_idx) < 500, (
            f"CF-1: 'next_action': tool_call and 'errors': [] are too far apart "
            f"({abs(na_idx - err_idx)} chars) — they should be in the same result dict"
        )


class TestCf1bMemoryUpdateNodeClearsErrors:
    """CF-1b: memory_update_node must clear errors on return."""

    def test_memory_update_returns_errors_empty(self):
        """memory_update_node must include errors=[] in its return dict."""
        import src.core.orchestration.graph.nodes.memory_update_node as mu

        src_text = inspect.getsource(mu)
        assert '"errors": []' in src_text, (
            "CF-1b: memory_update_node must return errors=[] to clear "
            "transient errors (e.g. context_overflow) before the next graph round"
        )

    def test_errors_cleared_near_result_assignment(self):
        """errors=[] should appear in the final result dict."""
        import src.core.orchestration.graph.nodes.memory_update_node as mu

        src_text = inspect.getsource(mu)
        # Find the final result dict: it includes "_force_compact": False
        fc_idx = src_text.rfind('"_force_compact": False')
        assert fc_idx != -1, "Could not find _force_compact in memory_update_node"
        region = src_text[max(0, fc_idx - 50) : fc_idx + 200]
        assert '"errors": []' in region, (
            "CF-1b: errors=[] must appear in the same result dict as _force_compact"
        )


class TestHr1DistillerSingletonExecutor:
    """HR-1: distiller must use a module-level singleton executor."""

    def test_singleton_executor_defined(self):
        """_DISTILLER_LLM_EXECUTOR module-level singleton must exist."""
        import src.core.memory.distiller as dist

        src_text = inspect.getsource(dist)
        assert "_DISTILLER_LLM_EXECUTOR" in src_text, (
            "HR-1: distiller must define a module-level _DISTILLER_LLM_EXECUTOR singleton"
        )

    def test_no_per_call_threadpoolexecutor(self):
        """_call_llm_sync must NOT instantiate a new ThreadPoolExecutor per call."""
        import src.core.memory.distiller as dist

        fn_src = inspect.getsource(dist._call_llm_sync)
        # The function body must not contain a ThreadPoolExecutor(...) constructor call.
        # (Module-level references in comments are fine; we check for the constructor call.)
        assert "ThreadPoolExecutor(" not in fn_src, (
            "HR-1: _call_llm_sync must not call ThreadPoolExecutor() per invocation — "
            "use the module-level singleton via _get_distiller_executor()"
        )

    def test_get_distiller_executor_returns_singleton(self):
        """_get_distiller_executor() must return the same instance on repeated calls."""
        from src.core.memory.distiller import _get_distiller_executor

        e1 = _get_distiller_executor()
        e2 = _get_distiller_executor()
        assert e1 is e2, (
            "HR-1: _get_distiller_executor() must return the same singleton instance"
        )

    def test_singleton_is_threadpoolexecutor(self):
        """The singleton must be a ThreadPoolExecutor."""
        import concurrent.futures
        from src.core.memory.distiller import _get_distiller_executor

        executor = _get_distiller_executor()
        assert isinstance(executor, concurrent.futures.ThreadPoolExecutor), (
            "HR-1: _get_distiller_executor() must return a ThreadPoolExecutor"
        )


class TestHr2ExecutionTraceCap:
    """HR-2: flush_execution_trace_impl must cap trace at 2000 entries."""

    def test_trace_cap_present_in_source(self):
        """flush_execution_trace_impl must contain a cap / rotation logic."""
        import src.core.orchestration.execution_trace as et

        src_text = inspect.getsource(et.flush_execution_trace_impl)
        assert "_TRACE_MAX" in src_text or "trace[-" in src_text, (
            "HR-2: flush_execution_trace_impl must cap trace size to prevent "
            "unbounded growth"
        )

    def test_trace_cap_value_is_reasonable(self):
        """The cap must be at least 500 and at most 10000."""
        import src.core.orchestration.execution_trace as et

        src_text = inspect.getsource(et.flush_execution_trace_impl)
        # Extract the cap value
        import re
        matches = re.findall(r"_TRACE_MAX\s*=\s*(\d+)", src_text)
        assert matches, "HR-2: _TRACE_MAX must be defined in flush_execution_trace_impl"
        cap = int(matches[0])
        assert 500 <= cap <= 10000, (
            f"HR-2: _TRACE_MAX={cap} is out of reasonable range [500, 10000]"
        )

    def test_flush_caps_oversized_trace(self, tmp_path):
        """flush_execution_trace_impl must trim a trace > _TRACE_MAX entries."""
        import json
        from unittest.mock import MagicMock

        from src.core.orchestration.execution_trace import flush_execution_trace_impl

        # Build a fake orchestrator with a big existing trace file
        context_dir = tmp_path / ".agent-context"
        context_dir.mkdir()
        trace_path = context_dir / "execution_trace.json"

        # Write 2500 existing entries
        existing = [{"tool": "read_file", "ts": "2026-01-01T00:00:00+00:00", "i": i} for i in range(2500)]
        trace_path.write_text(json.dumps(existing))

        orch = MagicMock()
        orch.working_dir = tmp_path
        orch._execution_trace_buffer = [{"tool": "write_file", "ts": "2026-01-01T00:01:00+00:00"}]

        flush_execution_trace_impl(orch)

        result = json.loads(trace_path.read_text())
        assert len(result) <= 2000, (
            f"HR-2: trace should be capped at 2000 after flush, got {len(result)}"
        )
        # Most recent entry (from buffer) must be present
        assert result[-1]["tool"] == "write_file", (
            "HR-2: most recent buffer entry must survive after capping"
        )
