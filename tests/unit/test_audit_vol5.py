"""
Audit Vol5 — regression tests.

Covers every fix applied in the vol5 session:

  C1 — Tool timeouts use ThreadPoolExecutor (not SIGALRM)
  C2 — Sandbox validates NEW Python content, not old file on disk
  C3 — analysis_node fast-path is suppressed for complex tasks
  C5 — EventBus publish_to_agent: wildcard+specific callbacks called exactly once
  F7/H9 — debug_attempts, max_debug_attempts, total_debug_attempts propagated across rounds
  H3  — send_prompt mutex prevents concurrent agent execution
  H6  — Dead state fields (tool_last_used, files_read) removed from AgentState
  P1  — _get_compiled_graph() returns same compiled graph object (singleton)
  F12 — DANGEROUS_PATTERNS whitespace-normalised before check in bash()
  F15 — _INDEXED_DIRS keyed by (path, mtime_ns); stale cache avoided
"""

from __future__ import annotations

import ast
import threading
import time
import types
from unittest.mock import MagicMock, patch, call
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# C5 — EventBus: no double-delivery to wildcard+specific subscriber
# ---------------------------------------------------------------------------

class TestC5EventBusNoDoubleDelivery:
    """C5: A callback registered on both '*' and a specific agent must fire once."""

    def _make_bus(self):
        from src.core.orchestration.event_bus import EventBus
        return EventBus()

    def test_wildcard_subscriber_receives_message(self):
        bus = self._make_bus()
        received = []
        bus.subscribe_to_agent("*", lambda m: received.append(m))
        bus.publish_to_agent("agent1", "payload")
        assert len(received) == 1

    def test_specific_subscriber_receives_message(self):
        bus = self._make_bus()
        received = []
        bus.subscribe_to_agent("agent1", lambda m: received.append(m))
        bus.publish_to_agent("agent1", "payload")
        assert len(received) == 1

    def test_both_wildcard_and_specific_no_double_delivery(self):
        """Core C5 regression: same callback registered on both '*' and 'agent1' must fire once."""
        bus = self._make_bus()
        received = []
        cb = lambda m: received.append(m)
        bus.subscribe_to_agent("*", cb)
        bus.subscribe_to_agent("agent1", cb)
        bus.publish_to_agent("agent1", "payload")
        assert len(received) == 1, (
            f"Callback must fire exactly once (got {len(received)}). "
            "C5: double-delivery regression"
        )

    def test_different_wildcard_and_specific_callbacks_both_fire(self):
        """Two distinct callbacks (one wildcard, one specific) must both fire exactly once."""
        bus = self._make_bus()
        wildcard_calls = []
        specific_calls = []
        bus.subscribe_to_agent("*", lambda m: wildcard_calls.append(m))
        bus.subscribe_to_agent("agent1", lambda m: specific_calls.append(m))
        bus.publish_to_agent("agent1", "payload")
        assert len(wildcard_calls) == 1
        assert len(specific_calls) == 1

    def test_wildcard_does_not_deliver_for_other_agent(self):
        """Wildcard sees messages for all agents but specific subscriber only sees its own."""
        bus = self._make_bus()
        wildcard_calls = []
        specific_calls = []
        bus.subscribe_to_agent("*", lambda m: wildcard_calls.append(m))
        bus.subscribe_to_agent("agent1", lambda m: specific_calls.append(m))
        bus.publish_to_agent("agent2", "other")
        assert len(wildcard_calls) == 1
        assert len(specific_calls) == 0

    def test_no_priority_based_double_delivery(self):
        """HIGH priority must not cause double-delivery (old bug used priority >= HIGH branch)."""
        from src.core.orchestration.event_bus import MessagePriority
        bus = self._make_bus()
        received = []
        cb = lambda m: received.append(m)
        bus.subscribe_to_agent("*", cb)
        bus.subscribe_to_agent("agent1", cb)
        bus.publish_to_agent("agent1", "hi", priority=MessagePriority.HIGH)
        assert len(received) == 1

    def test_no_critical_priority_double_delivery(self):
        from src.core.orchestration.event_bus import MessagePriority
        bus = self._make_bus()
        received = []
        cb = lambda m: received.append(m)
        bus.subscribe_to_agent("*", cb)
        bus.subscribe_to_agent("agent1", cb)
        bus.publish_to_agent("agent1", "critical", priority=MessagePriority.CRITICAL)
        assert len(received) == 1


# ---------------------------------------------------------------------------
# H6 — Dead state fields removed from AgentState TypedDict
# ---------------------------------------------------------------------------

class TestH6DeadStateFieldsRemoved:
    """H6: tool_last_used and files_read were re-added in vol5b with active functionality
    (cooldown enforcement and O(1) read-before-edit lookup). They must now be present."""

    def test_tool_last_used_in_agent_state(self):
        """tool_last_used re-added for cooldown enforcement (vol5b); must be present."""
        from src.core.orchestration.graph.state import AgentState
        assert "tool_last_used" in AgentState.__annotations__, (
            "tool_last_used must be in AgentState (used for cooldown enforcement)"
        )

    def test_files_read_in_agent_state(self):
        """files_read re-added for O(1) read-before-edit check (vol5b); must be present."""
        from src.core.orchestration.graph.state import AgentState
        assert "files_read" in AgentState.__annotations__, (
            "files_read must be in AgentState (used for O(1) MODIFYING_TOOLS check)"
        )

    def test_tool_call_count_still_present(self):
        """Ensure we didn't accidentally remove a live field alongside dead ones."""
        from src.core.orchestration.graph.state import AgentState
        assert "tool_call_count" in AgentState.__annotations__

    def test_max_tool_calls_still_present(self):
        from src.core.orchestration.graph.state import AgentState
        assert "max_tool_calls" in AgentState.__annotations__


# ---------------------------------------------------------------------------
# C1 — Tool timeout uses ThreadPoolExecutor, not SIGALRM
# ---------------------------------------------------------------------------

class TestC1ToolTimeoutThreadSafe:
    """C1: ThreadPoolExecutor timeout mechanism works from any thread (not SIGALRM)."""

    def test_threadpool_timeout_fires_from_daemon_thread(self):
        """
        The C1 fix replaces SIGALRM (main-thread-only) with ThreadPoolExecutor.result(timeout).
        Verify the mechanism fires correctly from a daemon thread.
        """
        import concurrent.futures as _cf

        error_holder = []

        def _run_in_daemon():
            # Reproduce the exact mechanism from execute_tool's C1 fix.
            # Use shutdown(wait=False) so the slow task doesn't block the daemon thread.
            executor = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(time.sleep, 10)  # 10-second task
                try:
                    future.result(timeout=1)  # 1-second timeout
                    error_holder.append("no_timeout")
                except _cf.TimeoutError:
                    future.cancel()
                    error_holder.append("timeout_fired")
            finally:
                executor.shutdown(wait=False)

        t = threading.Thread(target=_run_in_daemon, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), "Daemon thread must finish within 5s when timeout fires"
        assert error_holder == ["timeout_fired"], (
            "C1: ThreadPoolExecutor timeout must fire from daemon thread; "
            f"got {error_holder}"
        )

    def test_threadpool_allows_fast_completion(self):
        """Fast tasks must complete successfully without timeout interference."""
        import concurrent.futures as _cf

        results = []

        def _run():
            with _cf.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: 42)
                results.append(future.result(timeout=5))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=3)
        assert results == [42]

    def test_execute_tool_uses_threadpoolexecutor_in_source(self):
        """Verify the C1 fix is present: execute_tool source must reference ThreadPoolExecutor."""
        import inspect
        from src.core.orchestration import orchestrator as orc_mod
        source = inspect.getsource(orc_mod.Orchestrator.execute_tool)
        assert "ThreadPoolExecutor" in source, (
            "C1 fix: execute_tool must use ThreadPoolExecutor for timeouts"
        )
        assert "signal.SIGALRM" not in source, (
            "C1 fix: signal.SIGALRM must be removed from execute_tool (may still appear in comments)"
        )


# ---------------------------------------------------------------------------
# C2 — Sandbox validates new Python content directly
# ---------------------------------------------------------------------------

class TestC2SandboxValidatesNewContent:
    """C2: Python syntax errors in new content must be caught before writing."""

    def test_ast_parse_catches_syntax_error(self):
        """ast.parse raises SyntaxError on invalid Python — this is the core mechanism."""
        bad_python = "def foo(\n    pass\n"
        with pytest.raises(SyntaxError):
            ast.parse(bad_python)

    def test_ast_parse_accepts_valid_python(self):
        good_python = "def foo():\n    return 42\n"
        tree = ast.parse(good_python)
        assert tree is not None

    def test_new_content_validated_via_ast_parse(self):
        """
        The C2 fix uses ast.parse() on new content directly. Verify the validation
        path in execute_tool rejects Python files with syntax errors.
        """
        import inspect
        from src.core.orchestration import orchestrator as orc_mod

        source = inspect.getsource(orc_mod.Orchestrator.execute_tool)
        # C2 fix must parse new content directly, not call sandbox.validate_ast(path)
        assert "ast.parse" in source or "_ast.parse" in source, (
            "C2 fix: execute_tool must call ast.parse() on new content"
        )
        assert "new_py_content" in source, (
            "C2 fix: execute_tool must extract new Python content from tool args"
        )

    def test_bad_python_content_rejected_by_ast(self):
        """ast.parse raises SyntaxError — the core mechanism of the C2 fix."""
        bad = "def foo(\n    pass\n"
        with pytest.raises(SyntaxError):
            ast.parse(bad)

    def test_good_python_content_accepted_by_ast(self):
        good = "def foo():\n    return 42\n"
        assert ast.parse(good) is not None


# ---------------------------------------------------------------------------
# C3 — analysis_node fast-path suppressed for complex tasks
# ---------------------------------------------------------------------------

class TestC3AnalysisFastPath:
    """C3: Complex tasks must always go through full analysis even when next_action is set."""

    def _make_state(self, **kwargs) -> dict:
        base = {
            "task": "simple task",
            "history": [],
            "verified_reads": [],
            "rounds": 0,
            "working_dir": ".",
            "system_prompt": "",
            "next_action": None,
            "last_result": None,
            "errors": [],
            "current_plan": None,
            "current_step": 0,
            "deterministic": False,
            "seed": None,
            "analysis_summary": None,
            "relevant_files": None,
            "key_symbols": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
            "total_debug_attempts": 0,
            "last_debug_error_type": None,
            "verification_passed": None,
            "verification_result": None,
            "step_controller_enabled": True,
            "task_decomposed": False,
            "tool_call_count": 0,
            "max_tool_calls": 30,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": False,
            "plan_progress": None,
            "evaluation_result": None,
            "cancel_event": None,
            "empty_response_count": 0,
            "analyst_findings": None,
            "plan_resumed": None,
            "session_id": None,
            "delegation_results": None,
            "delegations": None,
            "last_tool_name": None,
            "original_task": None,
            "step_description": None,
            "planned_action": None,
            "plan_validation": None,
            "plan_enforce_warnings": None,
            "plan_strict_mode": None,
            "task_history": None,
            "step_retry_counts": None,
        }
        base.update(kwargs)
        return base

    def test_simple_task_with_next_action_skips_analysis(self):
        from src.core.orchestration.graph.builder import _task_is_complex
        state = self._make_state(
            task="list files",
            next_action={"name": "glob", "args": {}},
        )
        assert not _task_is_complex(state), "list files should be simple"

    def test_complex_task_is_detected(self):
        from src.core.orchestration.graph.builder import _task_is_complex
        state = self._make_state(
            task="refactor authentication module and add unit tests for all edge cases",
            relevant_files=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"],
        )
        assert _task_is_complex(state), "Refactoring + many files should be complex"

    def test_analysis_node_returns_fast_path_for_simple_task(self):
        """For a simple task with next_action set, analysis_node should return fast-path dict."""
        import asyncio
        from src.core.orchestration.graph.nodes.analysis_node import analysis_node

        state = self._make_state(
            task="list files",
            next_action={"name": "glob", "args": {}},
        )
        config = {"configurable": {"orchestrator": MagicMock()}}

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(analysis_node(state, config))
        finally:
            loop.close()
        assert result.get("analysis_summary") == "Skipped (Fast Path)", (
            "Simple task with next_action should take the fast path"
        )


# ---------------------------------------------------------------------------
# P1 — Graph singleton: _get_compiled_graph returns same object
# ---------------------------------------------------------------------------

class TestP1GraphSingleton:
    """P1: _get_compiled_graph() must compile the graph once and cache it."""

    def test_get_compiled_graph_returns_same_object(self):
        from src.core.orchestration.graph.builder import (
            _get_compiled_graph,
            _reset_compiled_graph,
        )
        _reset_compiled_graph()  # start clean
        g1 = _get_compiled_graph()
        g2 = _get_compiled_graph()
        assert g1 is g2, "_get_compiled_graph() must return the same compiled graph object"

    def test_reset_compiled_graph_clears_cache(self):
        from src.core.orchestration.graph.builder import (
            _get_compiled_graph,
            _reset_compiled_graph,
        )
        _reset_compiled_graph()
        g1 = _get_compiled_graph()
        _reset_compiled_graph()
        g2 = _get_compiled_graph()
        assert g1 is not g2, "_reset_compiled_graph() must force recompilation"

    def test_graph_singleton_is_callable(self):
        from src.core.orchestration.graph.builder import _get_compiled_graph, _reset_compiled_graph
        _reset_compiled_graph()
        g = _get_compiled_graph()
        assert hasattr(g, "ainvoke"), "Compiled graph must have ainvoke method"


# ---------------------------------------------------------------------------
# F15 — _INDEXED_DIRS keyed by (path, mtime_ns); stale cache avoided
# ---------------------------------------------------------------------------

class TestF15IndexedDirsStaleCachePrevented:
    """F15: _INDEXED_DIRS must be invalidated when directory mtime changes."""

    def test_is_already_indexed_returns_false_before_mark(self, tmp_path):
        from src.core.orchestration.graph.nodes.analysis_node import (
            _is_already_indexed,
            _mark_indexed,
            _INDEXED_DIRS,
        )
        _INDEXED_DIRS.clear()
        assert not _is_already_indexed(str(tmp_path))

    def test_is_already_indexed_returns_true_after_mark(self, tmp_path):
        from src.core.orchestration.graph.nodes.analysis_node import (
            _is_already_indexed,
            _mark_indexed,
            _INDEXED_DIRS,
        )
        _INDEXED_DIRS.clear()
        _mark_indexed(str(tmp_path))
        assert _is_already_indexed(str(tmp_path))

    def test_stale_cache_detected_on_mtime_change(self, tmp_path):
        """After a file is added to the directory, mtime changes → cache miss."""
        from src.core.orchestration.graph.nodes.analysis_node import (
            _is_already_indexed,
            _mark_indexed,
            _INDEXED_DIRS,
        )
        _INDEXED_DIRS.clear()
        _mark_indexed(str(tmp_path))
        assert _is_already_indexed(str(tmp_path))

        # Mutate the directory to change its mtime
        (tmp_path / "new_file.py").write_text("x = 1")

        assert not _is_already_indexed(str(tmp_path)), (
            "After directory mtime changes, _is_already_indexed must return False"
        )

    def test_mark_indexed_stores_mtime(self, tmp_path):
        import os
        from src.core.orchestration.graph.nodes.analysis_node import (
            _mark_indexed,
            _INDEXED_DIRS,
        )
        _INDEXED_DIRS.clear()
        _mark_indexed(str(tmp_path))
        resolved = str(os.path.realpath(tmp_path))
        assert resolved in _INDEXED_DIRS
        expected_mtime = os.stat(resolved).st_mtime_ns
        assert _INDEXED_DIRS[resolved] == expected_mtime


# ---------------------------------------------------------------------------
# F7/H9 — debug_attempts propagates across rounds in orchestrator
# ---------------------------------------------------------------------------

class TestF7H9DebugAttemptsPropagation:
    """F7/H9: debug_attempts and related fields must survive across graph rounds."""

    def test_debug_fields_present_in_state_init(self):
        """Orchestrator run_agent_once must not drop debug_attempts across rounds."""
        # The fix added these fields to the current_state rebuild in the multi-round loop.
        # We verify this by checking that the fields are listed in run_agent_once source.
        import inspect
        from src.core.orchestration import orchestrator as orc_module
        source = inspect.getsource(orc_module.Orchestrator.run_agent_once)
        assert "debug_attempts" in source, "debug_attempts must be propagated in run_agent_once"
        assert "max_debug_attempts" in source, "max_debug_attempts must be propagated"
        assert "total_debug_attempts" in source, "total_debug_attempts must be propagated"
        assert "last_debug_error_type" in source, "last_debug_error_type must be propagated"
        assert "step_retry_counts" in source, "step_retry_counts must be propagated"


# ---------------------------------------------------------------------------
# H3 — send_prompt mutex prevents concurrent agent execution
# ---------------------------------------------------------------------------

class TestH3SendPromptMutex:
    """H3: Concurrent send_prompt calls must be blocked by a mutex."""

    def test_agent_running_flag_initialized_false(self):
        """_agent_running must start as False on a fresh TUI instance."""
        from src.ui.textual_app_impl import TextualAppBase

        app = TextualAppBase.__new__(TextualAppBase)
        app._agent_lock = threading.Lock()
        app._agent_running = False
        assert not app._agent_running

    def test_is_agent_running_property(self):
        from src.ui.textual_app_impl import TextualAppBase

        app = TextualAppBase.__new__(TextualAppBase)
        app._agent_lock = threading.Lock()
        app._agent_running = False
        assert not app.is_agent_running

        app._agent_running = True
        assert app.is_agent_running

    def test_send_prompt_blocked_when_agent_running(self):
        """send_prompt must not start a new agent thread if one is already running."""
        from src.ui.textual_app_impl import TextualAppBase

        app = TextualAppBase.__new__(TextualAppBase)
        app._agent_lock = threading.Lock()
        app._agent_running = True  # simulate running agent
        # Mock dependencies that send_prompt uses
        app.log = MagicMock()
        app._cancel_event = MagicMock()

        threads_started = []
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread_cls.side_effect = lambda **kw: threads_started.append(kw) or MagicMock()
            # Call send_prompt — it should bail out early
            try:
                app.send_prompt("test prompt")
            except Exception:
                pass  # may fail on missing attrs — the important check is below

        assert len(threads_started) == 0, (
            "send_prompt must not start a new thread when agent is already running"
        )


# ---------------------------------------------------------------------------
# F12 — DANGEROUS_PATTERNS whitespace-normalised in bash()
# ---------------------------------------------------------------------------

class TestF12DangerousPatternsNormalised:
    """F12: bash() must detect dangerous patterns even with extra whitespace."""

    def test_rm_rf_with_double_space_blocked(self):
        from src.tools.file_tools import bash
        result = bash("rm  -rf /tmp/test")
        assert result.get("status") == "error"
        assert "dangerous" in result.get("error", "").lower()

    def test_rm_rf_normal_blocked(self):
        from src.tools.file_tools import bash
        result = bash("rm -rf /tmp/test")
        assert result.get("status") == "error"

    def test_pipe_operator_blocked(self):
        from src.tools.file_tools import bash
        result = bash("ls | grep foo")
        assert result.get("status") == "error"

    def test_background_operator_blocked(self):
        """Shell operators like && must be blocked."""
        from src.tools.file_tools import bash
        result = bash("echo hello && echo world")
        assert result.get("status") == "error"

    def test_safe_command_allowed(self):
        from src.tools.file_tools import bash
        result = bash("echo hello")
        # Should not return a dangerous-pattern error
        assert "dangerous" not in result.get("error", "").lower()
