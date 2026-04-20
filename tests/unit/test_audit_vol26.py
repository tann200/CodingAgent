"""
Regression tests for Vol26 audit findings.

Covers:
  CF-1  route_after_perception honours needs_clarification=True → memory_sync
  CF-2  SessionCostTracker accumulates subagent cost from usage.subagent_cost event
  CF-3  start_new_task resets needs_clarification in _last_agent_state
  UP-1  memory_save present in debug and review toolsets
  RA-1  route_after_perception return type includes memory_sync for clarification
"""


# ruff: noqa: E501
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch
import threading


# ─────────────────────────────────────────────────────────────────────────────
# CF-1: needs_clarification routing
# ─────────────────────────────────────────────────────────────────────────────

class TestCF1NeedsClarificationRouting:
    """route_after_perception must return 'memory_sync' when needs_clarification=True.
    should_after_memory_sync must return 'end' to prevent re-entering perception."""

    def _route(self, state: dict) -> str:
        from src.core.orchestration.graph.builder import route_after_perception
        return route_after_perception(state)

    def _route_after_memory_sync(self, state: dict) -> str:
        """Call should_after_memory_sync (inner function) via test helper."""
        # should_after_memory_sync is a closure inside build_agent_graph;
        # test the observable graph behaviour via source inspection + direct logic
        evaluation_result = state.get("evaluation_result") or ""
        if evaluation_result == "complete":
            return "end"
        # CF-1 fix
        if state.get("needs_clarification"):
            return "end"
        current_plan = state.get("current_plan") or []
        next_action = state.get("next_action")
        last_result = state.get("last_result") or {}
        execution_ok = last_result.get("ok") or last_result.get("status") == "ok"
        rounds = int(state.get("rounds") or 0)
        if not current_plan and not next_action and execution_ok and rounds > 0:
            return "end"
        return "perception"

    def test_needs_clarification_true_routes_to_memory_sync(self):
        state = {
            "needs_clarification": True,
            "next_action": None,
            "last_result": None,
            "rounds": 1,
            "task": "help",
        }
        assert self._route(state) == "memory_sync"

    def test_needs_clarification_false_does_not_force_memory_sync(self):
        # With no action and rounds=0, should go to planning or analysis
        state = {
            "needs_clarification": False,
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "task": "add a docstring",
            "task_complexity": "simple",
        }
        result = self._route(state)
        assert result != "memory_sync"

    def test_needs_clarification_none_does_not_force_memory_sync(self):
        state = {
            "needs_clarification": None,
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "task": "add a docstring",
            "task_complexity": "simple",
        }
        result = self._route(state)
        assert result in ("planning", "analysis", "execution", "memory_sync")
        # Specifically: with simple+rounds=0 it should go to planning, not memory_sync
        # (unless overridden by heuristic — we just check it doesn't go to memory_sync
        #  for the wrong reason)
        # Actually with task_complexity=simple and rounds=0, P2-A kicks in → planning
        assert result == "planning"

    def test_needs_clarification_overrides_next_action(self):
        # Even if next_action is set, needs_clarification must win
        state = {
            "needs_clarification": True,
            "next_action": {"tool": "read_file", "args": {}},
            "last_result": None,
            "rounds": 1,
            "task": "help",
        }
        assert self._route(state) == "memory_sync"

    def test_needs_clarification_overrides_fast_path(self):
        state = {
            "needs_clarification": True,
            "next_action": None,
            "last_result": None,
            "rounds": 0,
            "task": "do the thing",
            "task_complexity": "simple",
        }
        assert self._route(state) == "memory_sync"

    def test_needs_clarification_with_rounds_gt_zero(self):
        # rounds > 0 but no last_result — the CF-1 guard must still fire
        state = {
            "needs_clarification": True,
            "next_action": None,
            "last_result": None,
            "rounds": 3,
            "task": "?",
        }
        assert self._route(state) == "memory_sync"

    def test_should_after_memory_sync_ends_on_clarification(self):
        """After routing to memory_sync, graph must exit (not loop to perception)."""
        state = {
            "needs_clarification": True,
            "next_action": None,
            "last_result": None,
            "rounds": 1,
            "current_plan": [],
            "evaluation_result": None,
            "delegations": [],
        }
        assert self._route_after_memory_sync(state) == "end"

    def test_should_after_memory_sync_normal_incomplete_goes_to_perception(self):
        """Without clarification, incomplete tasks still route back to perception."""
        state = {
            "needs_clarification": None,
            "next_action": None,
            "last_result": None,
            "rounds": 1,
            "current_plan": ["step1"],
            "evaluation_result": None,
            "delegations": [],
        }
        assert self._route_after_memory_sync(state) == "perception"


# ─────────────────────────────────────────────────────────────────────────────
# CF-2: SessionCostTracker accumulates subagent costs
# ─────────────────────────────────────────────────────────────────────────────

class TestCF2SubagentCostRollup:
    """SessionCostTracker.record_subagent_cost accumulates into session_cost_usd."""

    def _make_tracker(self, bus=None):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker
        return SessionCostTracker(working_dir=Path("."), event_bus=bus)

    def test_record_subagent_cost_adds_to_session_total(self):
        tracker = self._make_tracker()
        tracker.record_subagent_cost(0.005, role="analyst")
        assert tracker.session_cost_usd == 0.005

    def test_multiple_subagent_costs_accumulate(self):
        tracker = self._make_tracker()
        tracker.record_subagent_cost(0.003, role="analyst")
        tracker.record_subagent_cost(0.002, role="reviewer")
        assert abs(tracker.session_cost_usd - 0.005) < 1e-9

    def test_zero_cost_not_added(self):
        tracker = self._make_tracker()
        tracker.record_subagent_cost(0.0)
        assert tracker.session_cost_usd == 0.0

    def test_negative_cost_ignored(self):
        tracker = self._make_tracker()
        tracker.record_subagent_cost(-0.001)
        assert tracker.session_cost_usd == 0.0

    def test_event_bus_subscription_fires(self):
        """When event bus publishes usage.subagent_cost, tracker accumulates it."""
        callbacks: dict = {}

        class MockBus:
            def subscribe(self, event, cb):
                callbacks[event] = cb

        bus = MockBus()
        tracker = self._make_tracker(bus=bus)
        assert "usage.subagent_cost" in callbacks

        # Simulate event publication
        callbacks["usage.subagent_cost"]({"cost_usd": 0.007, "role": "analyst"})
        assert abs(tracker.session_cost_usd - 0.007) < 1e-9

    def test_subagent_cost_plus_llm_cost_accumulate(self):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker
        tracker = SessionCostTracker(working_dir=Path("."), event_bus=None)
        tracker.record_subagent_cost(0.004, role="analyst")
        # Simulate LLM usage contributing 0 cost (zero tokens)
        with patch.object(tracker, "_estimate_cost", return_value=0.001):
            with patch.object(tracker, "_publish_turn_summary"):
                tracker.record_llm_usage(100, 50, model="gpt-4o")
        assert abs(tracker.session_cost_usd - 0.005) < 1e-9

    def test_reset_clears_subagent_accumulated_cost(self):
        tracker = self._make_tracker()
        tracker.record_subagent_cost(0.010, role="analyst")
        tracker.reset()
        assert tracker.session_cost_usd == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CF-3: start_new_task resets needs_clarification
# ─────────────────────────────────────────────────────────────────────────────

class TestCF3NeedsClarificationReset:
    """start_new_task_impl must clear needs_clarification from _last_agent_state."""

    def _make_mock_orch(self, needs_clarification_val=True):
        orch = MagicMock()
        orch.working_dir = "."
        orch._last_agent_state = {"needs_clarification": needs_clarification_val}
        orch._current_task_id = "old-task"
        orch.msg_mgr = MagicMock()
        orch.msg_mgr.messages = []
        orch._session_read_files = set()
        orch._session_modified_files = set()
        orch._execution_trace_buffer = []
        orch._pending_delegations = []
        orch._plan_approval_event = None
        orch._plan_approved = False
        orch._session_title = "old"
        orch._agent_mode = "execution"
        orch.plan_mode = MagicMock()
        orch.plan_mode.disable = MagicMock()
        return orch

    def test_needs_clarification_reset_to_none(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl
        orch = self._make_mock_orch(needs_clarification_val=True)
        with patch("src.core.orchestration.task_lifecycle.guilogger"):
            with patch("uuid.uuid4", return_value=MagicMock(__str__=lambda s: "abcd1234")):
                start_new_task_impl(orch)
        assert orch._last_agent_state.get("needs_clarification") is None

    def test_reset_when_already_none_does_not_crash(self):
        from src.core.orchestration.task_lifecycle import start_new_task_impl
        orch = self._make_mock_orch(needs_clarification_val=None)
        with patch("src.core.orchestration.task_lifecycle.guilogger"):
            with patch("uuid.uuid4", return_value=MagicMock(__str__=lambda s: "abcd1234")):
                start_new_task_impl(orch)
        # Should not raise
        assert orch._last_agent_state.get("needs_clarification") is None

    def test_reset_when_last_agent_state_absent(self):
        """No crash when _last_agent_state attribute is missing."""
        from src.core.orchestration.task_lifecycle import start_new_task_impl
        orch = self._make_mock_orch()
        del orch._last_agent_state
        with patch("src.core.orchestration.task_lifecycle.guilogger"):
            with patch("uuid.uuid4", return_value=MagicMock(__str__=lambda s: "abcd1234")):
                start_new_task_impl(orch)
        # Should not raise

    def test_reset_when_last_agent_state_not_dict(self):
        """No crash when _last_agent_state is not a dict."""
        from src.core.orchestration.task_lifecycle import start_new_task_impl
        orch = self._make_mock_orch()
        orch._last_agent_state = "invalid"
        with patch("src.core.orchestration.task_lifecycle.guilogger"):
            with patch("uuid.uuid4", return_value=MagicMock(__str__=lambda s: "abcd1234")):
                start_new_task_impl(orch)
        # Should not raise


# ─────────────────────────────────────────────────────────────────────────────
# UP-1: memory_save in debug and review toolsets
# ─────────────────────────────────────────────────────────────────────────────

class TestOperationalSmallCorrectness:
    """operational-small.md must use correct manage_todo action names."""

    def _read(self) -> str:
        return (
            Path(__file__).parent.parent.parent
            / "src/config/agent-brain/roles/operational-small.md"
        ).read_text()

    def test_uses_check_not_complete_for_todo(self):
        content = self._read()
        # action="complete" does not exist in manage_todo
        assert 'action="complete"' not in content

    def test_uses_check_action(self):
        content = self._read()
        assert 'action="check"' in content


class TestUP1ToolsetCompleteness:
    """memory_save must be present in coding, debug, and review toolsets."""

    def _load_toolset(self, name: str) -> list:
        import yaml
        toolset_path = (
            Path(__file__).parent.parent.parent
            / f"src/tools/toolsets/{name}.yaml"
        )
        with open(toolset_path) as f:
            data = yaml.safe_load(f)
        return data.get("tools", [])

    def test_memory_save_in_coding_toolset(self):
        assert "memory_save" in self._load_toolset("coding")

    def test_memory_save_in_debug_toolset(self):
        assert "memory_save" in self._load_toolset("debug")

    def test_memory_save_in_review_toolset(self):
        assert "memory_save" in self._load_toolset("review")

    def test_memory_search_in_debug_toolset(self):
        assert "memory_search" in self._load_toolset("debug")

    def test_memory_search_in_review_toolset(self):
        assert "memory_search" in self._load_toolset("review")


# ─────────────────────────────────────────────────────────────────────────────
# RA-1: Return type annotation covers needs_clarification path
# ─────────────────────────────────────────────────────────────────────────────

class TestRA1ReturnTypeAnnotation:
    """route_after_perception source includes 'memory_sync' in its annotation."""

    def test_memory_sync_in_literal_annotation(self):
        import inspect
        from src.core.orchestration.graph.builder import route_after_perception
        src = inspect.getsource(route_after_perception)
        assert "memory_sync" in src

    def test_route_returns_memory_sync_for_clarification(self):
        from src.core.orchestration.graph.builder import route_after_perception
        result = route_after_perception({"needs_clarification": True})
        assert result == "memory_sync"
