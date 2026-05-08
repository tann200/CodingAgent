"""Tests for src/core/orchestration/inference_loop.py (Phase E refactor).

Covers:
- _generate_work_summary: various state combinations
- run_agent_once_impl: cancel-before-start early return
- run_agent_once_impl: max_turns guard
- run_agent_once_impl: resets _session_read_files, _usage_buffer, _dry_run_log
- Module exports and importability
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(**kwargs):
    """Return a minimal mock orchestrator for inference_loop tests."""
    orch = MagicMock()
    orch._current_task_id = "test1234"
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._usage_buffer = {}
    orch._dry_run_log = []
    orch._session_title = None
    orch._agent_mode = None
    orch.working_dir = "/tmp/test_dir"
    orch.deterministic = False
    orch.seed = None
    orch.cancel_event = None
    orch.cost_tracker = MagicMock()
    orch.tool_execution_service = MagicMock()
    orch.msg_mgr = MagicMock()
    orch.msg_mgr.messages = []
    orch.session_store = MagicMock()
    orch.event_bus = MagicMock()
    orch._adapter = None
    orch._graph_executor = MagicMock()
    for k, v in kwargs.items():
        setattr(orch, k, v)
    return orch


# ---------------------------------------------------------------------------
# _generate_work_summary
# ---------------------------------------------------------------------------


class TestGenerateWorkSummary:
    def test_returns_empty_string_for_none_state(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        result = _generate_work_summary(None, [])
        assert result == ""

    def test_returns_empty_string_for_empty_state(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        result = _generate_work_summary({}, [])
        assert result == ""

    def test_includes_task_in_summary(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {"task": "Fix the bug in module X"}
        result = _generate_work_summary(state, [])
        assert "Fix the bug" in result

    def test_truncates_long_task(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        long_task = "A" * 200
        state = {"task": long_task}
        result = _generate_work_summary(state, [])
        assert "A" * 100 in result
        assert "A" * 101 not in result

    def test_includes_rounds(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {"rounds": 7}
        result = _generate_work_summary(state, [])
        assert "7" in result

    def test_includes_plan_progress(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {
            "current_plan": ["step1", "step2", "step3"],
            "current_step": 2,
        }
        result = _generate_work_summary(state, [])
        assert "2/3" in result

    def test_includes_verified_reads_count(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {"verified_reads": ["file1.py", "file2.py", "file3.py"]}
        result = _generate_work_summary(state, [])
        assert "3" in result

    def test_counts_tool_calls_from_history(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        history = [
            {"role": "tool", "tool": "read_file", "result": {}},
            {"role": "tool", "tool": "read_file", "result": {}},
            {"role": "tool", "tool": "write_file", "result": {}},
        ]
        state = {"task": "something"}
        result = _generate_work_summary(state, history)
        # read_file appears twice — should be in top tools
        assert "read_file" in result

    def test_counts_tool_errors(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        history = [
            {
                "role": "tool",
                "tool": "write_file",
                "result": {"error": "permission denied", "status": "error"},
            },
        ]
        state = {"task": "something"}
        result = _generate_work_summary(state, history)
        assert "Error" in result or "error" in result.lower()

    def test_reports_failed_status(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {"last_result": {"ok": False}}
        result = _generate_work_summary(state, [])
        assert "failed" in result.lower()

    def test_no_plan_fail_count(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {"no_plan_fail_count": 3}
        result = _generate_work_summary(state, [])
        assert "3" in result

    def test_separator_pipe_present_when_multiple_parts(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        state = {"task": "do something", "rounds": 5}
        result = _generate_work_summary(state, [])
        assert "|" in result


# ---------------------------------------------------------------------------
# run_agent_once_impl — cancel before start
# ---------------------------------------------------------------------------


class TestRunAgentOnceImplCancelBeforeStart:
    def test_returns_cancel_error_when_event_set(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        cancel_event = threading.Event()
        cancel_event.set()

        result = run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "do something"}],
            tools={},
            cancel_event=cancel_event,
        )

        assert result.get("ok") is False
        assert result.get("error") == "canceled_before_start"

    def test_cancel_before_start_no_graph_invocation(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        cancel_event = threading.Event()
        cancel_event.set()

        # Graph selection is imported inline, so patch the builder module directly
        with patch(
            "src.core.orchestration.graph.builder.get_compiled_graph_for_orchestrator"
        ) as mock_graph:
            run_agent_once_impl(
                orch,
                system_prompt_name=None,
                messages=[{"role": "user", "content": "do something"}],
                tools={},
                cancel_event=cancel_event,
            )
            # Graph should never be reached because cancel fires before the graph call
            mock_graph.assert_not_called()


# ---------------------------------------------------------------------------
# run_agent_once_impl — max_turns guard
# ---------------------------------------------------------------------------


class TestRunAgentOnceImplMaxTurns:
    def test_max_turns_guard_returns_error(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()

        # Patch graph selection and config so we can reach the max_turns check
        with (
            patch(
                "src.core.orchestration.inference_loop.load_system_prompt",
                return_value="system",
                create=True,
            ),
            patch(
                "src.core.orchestration.graph.builder.get_compiled_graph_for_orchestrator",
                create=True,
            ),
            patch("src.core.config_loader.get", return_value=1),
        ):
            # turn_count >= max_turns: override via config returning max=1
            # We need to inject the messages so turn_count starts from initial_state=0
            # But config returns max_turns=1 and turn_count starts at 0 — 0 < 1 so
            # we need turn_count=1 which requires a started state.
            # Instead, test directly: turn_count=0 and max_turns=0 via config
            with patch("src.core.config_loader.get", return_value=0):
                result = run_agent_once_impl(
                    orch,
                    system_prompt_name=None,
                    messages=[{"role": "user", "content": "hi"}],
                    tools={},
                    cancel_event=None,
                )
        assert result.get("ok") is False
        assert "max_turns" in result.get("error", "")

    def test_max_turns_guard_uses_tier_aware_graph_selector(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()

        with (
            patch(
                "src.core.orchestration.graph.builder.get_compiled_graph_for_orchestrator",
                return_value=MagicMock(),
            ) as mock_graph,
            patch("src.core.config_loader.get", return_value=0),
        ):
            run_agent_once_impl(
                orch,
                system_prompt_name=None,
                messages=[{"role": "user", "content": "hi"}],
                tools={},
                cancel_event=None,
            )

        mock_graph.assert_called_once_with(orchestrator=orch)

    def test_max_turns_guard_does_not_block_normal_turn(self):
        """When turn_count < max_turns, the guard should not fire."""
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        cancel_event = threading.Event()
        cancel_event.set()  # cancel after guard to prevent full graph execution

        # config returns None → uses default max_turns=50, turn_count=0 → fine
        with patch("src.core.config_loader.get", return_value=None):
            result = run_agent_once_impl(
                orch,
                system_prompt_name=None,
                messages=[{"role": "user", "content": "hi"}],
                tools={},
                cancel_event=cancel_event,
            )
        # Should fail with canceled_before_start, NOT max_turns
        assert result.get("error") == "canceled_before_start"


# ---------------------------------------------------------------------------
# run_agent_once_impl — state resets
# ---------------------------------------------------------------------------


class TestRunAgentOnceImplStateResets:
    def test_resets_session_read_files(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        orch._session_read_files = {"file1.py", "file2.py"}
        cancel_event = threading.Event()
        cancel_event.set()

        run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "task"}],
            tools={},
            cancel_event=cancel_event,
        )

        # Should be reset to an empty set at the top of run_agent_once_impl
        assert orch._session_read_files == set()

    def test_resets_usage_buffer(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        orch._usage_buffer = {"some": "data"}
        cancel_event = threading.Event()
        cancel_event.set()

        run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "task"}],
            tools={},
            cancel_event=cancel_event,
        )

        assert orch._usage_buffer == {}

    def test_resets_dry_run_log(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        orch._dry_run_log = ["old_entry"]
        cancel_event = threading.Event()
        cancel_event.set()

        run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "task"}],
            tools={},
            cancel_event=cancel_event,
        )

        assert orch._dry_run_log == []

    def test_cancel_event_stored_on_orch(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        cancel_event = threading.Event()
        cancel_event.set()

        run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "task"}],
            tools={},
            cancel_event=cancel_event,
        )

        assert orch.cancel_event is cancel_event

    def test_cost_tracker_reset_called(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        cancel_event = threading.Event()
        cancel_event.set()

        run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "task"}],
            tools={},
            cancel_event=cancel_event,
        )

        orch.cost_tracker.reset.assert_called_once()

    def test_tool_execution_service_reset_idempotency_called(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        orch = _make_orch()
        cancel_event = threading.Event()
        cancel_event.set()

        run_agent_once_impl(
            orch,
            system_prompt_name=None,
            messages=[{"role": "user", "content": "task"}],
            tools={},
            cancel_event=cancel_event,
        )

        orch.tool_execution_service.reset_idempotency.assert_called_once()


# ---------------------------------------------------------------------------
# Module-level API checks
# ---------------------------------------------------------------------------


class TestInferenceLoopModuleAPI:
    def test_module_importable(self):
        import src.core.orchestration.inference_loop as m

        assert m is not None

    def test_run_agent_once_impl_exported(self):
        from src.core.orchestration.inference_loop import run_agent_once_impl

        assert callable(run_agent_once_impl)

    def test_generate_work_summary_exported(self):
        from src.core.orchestration.inference_loop import _generate_work_summary

        assert callable(_generate_work_summary)

    def test_source_contains_session_title(self):
        import inspect

        import src.core.orchestration.inference_loop as m

        src = inspect.getsource(m)
        assert "_session_title" in src

    def test_source_contains_session_title_generated_event(self):
        import inspect

        import src.core.orchestration.inference_loop as m

        src = inspect.getsource(m)
        assert "session.title_generated" in src

    def test_source_contains_agent_mode(self):
        import inspect

        import src.core.orchestration.inference_loop as m

        src = inspect.getsource(m)
        assert '"agent_mode"' in src

    def test_source_contains_max_turns(self):
        import inspect

        import src.core.orchestration.inference_loop as m

        src = inspect.getsource(m)
        assert "max_turns" in src
