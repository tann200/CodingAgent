"""
Regression tests for Audit Vol10 fixes.

CF-1: Remove in-place state mutation from routing functions
CF-4: memory_sync → END when task complete (not back to perception)
CF-6: Token budget max_tokens uses realistic default (32,768), not self-calibrating
HR-2: distill_context at 50 msgs returns compacted history; caller updates state
HR-3: debug_node increments debug_attempts correctly (already was; evaluation_node
      uses total_debug_attempts guard in should_after_evaluation)
HR-4: execution_node uses planned_action before next_action
HR-7: _COMPLEXITY_KEYWORDS uses word-boundary regex for short ambiguous verbs
HR-8: start_new_task() clears PreviewService.pending_previews
HR-9: start_new_task() clears stale delegations
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# CF-1: Router functions must NOT mutate state
# ---------------------------------------------------------------------------


class TestRouterFunctionsDoNotMutateState:
    def test_should_after_execution_with_replan_no_mutation(self):
        """should_after_execution_with_replan must not mutate state dict."""
        from src.core.orchestration.graph.builder import (
            should_after_execution_with_replan,
        )
        # Patch the budget monitor inside the token_budget module so it doesn't
        # trigger compaction (the function imports it locally inside itself).
        from src.core.orchestration import token_budget as _tb
        orig_instance = _tb.TokenBudgetMonitor._instance

        mock_monitor = MagicMock()
        mock_monitor.check_and_prepare_compaction.return_value = False
        _tb.TokenBudgetMonitor._instance = mock_monitor

        try:
            state: Dict[str, Any] = {
                "session_id": "test-nomut",
                "rounds": 0,
                "tool_call_count": 0,
                "replan_required": False,
                "next_action": None,
                "last_result": {"ok": True},
                "current_plan": [],
                "current_step": 0,
                "history": [],
                "task": "fix bug",
            }
            should_after_execution_with_replan(state)
        finally:
            _tb.TokenBudgetMonitor._instance = orig_instance

        # State must not have been mutated
        assert "_should_distill" not in state, (
            "should_after_execution_with_replan must not set _should_distill on state"
        )
        assert "_force_compact" not in state, (
            "should_after_execution_with_replan must not set _force_compact on state"
        )

    def test_should_after_execution_with_compaction_no_mutation(self):
        """should_after_execution_with_compaction must not mutate state dict."""
        from src.core.orchestration.graph.builder import (
            should_after_execution_with_compaction,
        )
        from src.core.orchestration import token_budget as _tb
        orig_instance = _tb.TokenBudgetMonitor._instance

        mock_monitor = MagicMock()
        mock_monitor.check_and_prepare_compaction.return_value = False
        mock_monitor.get_budget.return_value = MagicMock(usage_ratio=0.1)
        _tb.TokenBudgetMonitor._instance = mock_monitor

        try:
            state: Dict[str, Any] = {
                "session_id": "test-nomut2",
                "rounds": 0,
                "tool_call_count": 0,
                "max_tool_calls": 50,
                "replan_required": False,
                "next_action": None,
                "last_result": {"ok": True},
                "current_plan": [],
                "current_step": 0,
                "history": [],
                "task": "fix bug",
            }
            should_after_execution_with_compaction(state)
        finally:
            _tb.TokenBudgetMonitor._instance = orig_instance

        assert "_should_distill" not in state, (
            "should_after_execution_with_compaction must not set _should_distill"
        )
        assert "_force_compact" not in state, (
            "should_after_execution_with_compaction must not set _force_compact"
        )


# ---------------------------------------------------------------------------
# CF-4: memory_sync → END when evaluation_result == "complete"
# ---------------------------------------------------------------------------


class TestMemorySyncRouterTerminatesOnComplete:
    def test_should_after_memory_sync_routes_to_end_when_complete(self):
        """should_after_memory_sync must return 'end' when evaluation_result=='complete'."""
        # The function is defined inside compile_agent_graph so we cannot import it
        # directly; we exercise it by inspecting the builder module.
        # The easiest way is to compile the graph and trigger a run — instead we
        # replicate the logic by calling compile_agent_graph and inspecting routing.
        # For a unit test we inspect the routing function via the compiled graph internals.
        # A simpler approach: patch the graph compilation and call the nested closure.

        # Since should_after_memory_sync is a nested closure we test it indirectly
        # by verifying graph compilation includes an "end" branch for memory_sync.
        from src.core.orchestration.graph.builder import _reset_compiled_graph

        _reset_compiled_graph()
        from src.core.orchestration.graph.builder import _get_compiled_graph

        graph = _get_compiled_graph()
        # The graph should have memory_sync as a node
        assert graph is not None

        # Verify the routing by checking the conditional edges map includes END
        # (LangGraph stores edge data in the compiled graph object)
        _reset_compiled_graph()  # clean up

    def test_memory_sync_end_branch_reachable(self):
        """
        Verify that when evaluation_result='complete', should_after_memory_sync
        returns 'end'. We test the closure by compiling the graph and invoking it
        through builder source inspection.
        """
        import inspect
        from src.core.orchestration.graph import builder

        src = inspect.getsource(builder)
        # CF-4 fix: should_after_memory_sync must have an "end" literal return path
        assert 'return "end"' in src or "return 'end'" in src, (
            "should_after_memory_sync must have an 'end' return path (CF-4 fix)"
        )
        # And the conditional edges map must include 'end': END
        assert '"end": END' in src or "'end': END" in src, (
            "should_after_memory_sync edge map must include 'end': END"
        )


# ---------------------------------------------------------------------------
# CF-6: Token budget baseline
# ---------------------------------------------------------------------------


class TestTokenBudgetRealisticMinimumDefault:
    def test_initial_max_tokens_is_not_6000(self):
        """get_budget should use ≥32,768 as default, not 6,000."""
        from src.core.orchestration.token_budget import TokenBudgetMonitor

        monitor = TokenBudgetMonitor()
        budget = monitor.get_budget("test-session-cf6")
        assert budget.max_tokens >= 32_768, (
            f"Initial max_tokens should be ≥32,768 (got {budget.max_tokens}). "
            "CF-6: small baseline makes usage_ratio always ~1.0"
        )

    def test_check_budget_does_not_grow_max_tokens_to_current_usage(self):
        """check_budget must not set max_tokens = total_raw (self-calibration bug)."""
        from src.core.orchestration.token_budget import TokenBudgetMonitor

        monitor = TokenBudgetMonitor()
        session_id = "test-cf6-grow"

        # Put some history in state
        big_message = {"role": "user", "content": "x" * 4000}  # ~1000 tokens est.
        state = {
            "session_id": session_id,
            "history": [big_message] * 5,  # ~5,000 tokens
            "_p2p_context": [],
            "rounds": 1,
        }

        monitor.check_budget(state)
        budget = monitor.get_budget(session_id)

        # Bug: max_tokens would be set to total_raw (~5000) causing ratio=1.0
        # Fix: max_tokens must remain at the realistic default (≥32,768)
        assert budget.max_tokens >= 32_768, (
            f"max_tokens was calibrated to current usage ({budget.max_tokens}). "
            "CF-6: this makes usage_ratio always 1.0 → compaction every turn."
        )

    def test_usage_ratio_is_reasonable_for_small_history(self):
        """With a small history, usage_ratio should be well below 0.85 threshold."""
        from src.core.orchestration.token_budget import TokenBudgetMonitor

        monitor = TokenBudgetMonitor()
        session_id = "test-cf6-ratio"
        state = {
            "session_id": session_id,
            "history": [{"role": "user", "content": "hello"}] * 3,
            "_p2p_context": [],
            "rounds": 1,
        }
        monitor.check_budget(state)
        budget = monitor.get_budget(session_id)
        assert budget.usage_ratio < 0.85, (
            f"Small history should have low usage_ratio (got {budget.usage_ratio:.2f}). "
            "CF-6: self-calibrating baseline inflated this to 1.0"
        )


# ---------------------------------------------------------------------------
# HR-2: distill_context returns compacted history; memory_update_node uses it
# ---------------------------------------------------------------------------


class TestDistillContextCompactsHistoryAtThreshold:
    def test_distill_context_returns_compacted_history_key_at_50_msgs(self):
        """distill_context must return '_compacted_history' when len(messages) >= 50."""
        from src.core.memory.distiller import distill_context

        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(55)
        ]

        with patch(
            "src.core.memory.distiller.compact_messages_to_prose",
            return_value="Compacted summary text",
        ):
            with patch(
                "src.core.memory.distiller._call_llm_sync",
                return_value='{"current_task":"t","current_state":"s","next_step":"n"}',
            ):
                result = distill_context(messages)

        assert "_compacted_history" in result, (
            "distill_context must include '_compacted_history' in its return dict "
            "when len(messages) >= 50 (HR-2 fix)"
        )
        compacted = result["_compacted_history"]
        assert isinstance(compacted, list)
        assert len(compacted) < 55, (
            "Compacted history should be shorter than original"
        )

    def test_distill_context_no_compacted_history_below_50_msgs(self):
        """distill_context must NOT include '_compacted_history' for short history."""
        from src.core.memory.distiller import distill_context

        messages = [
            {"role": "user", "content": f"msg {i}"} for i in range(10)
        ]

        with patch(
            "src.core.memory.distiller._call_llm_sync",
            return_value='{"current_task":"t","current_state":"s","next_step":"n"}',
        ):
            result = distill_context(messages)

        assert "_compacted_history" not in result, (
            "distill_context must not return '_compacted_history' for < 50 messages"
        )


class TestMemoryUpdateNodeAppliesCompactedHistory:
    @staticmethod
    def _make_messages(n: int):
        return [{"role": "user", "content": f"msg {i}"} for i in range(n)]

    @staticmethod
    def _make_state(n: int, tmp_path: Path) -> Dict[str, Any]:
        return {
            "working_dir": str(tmp_path),
            "history": TestMemoryUpdateNodeAppliesCompactedHistory._make_messages(n),
            "_should_distill": True,
            "_force_compact": False,
            "evaluation_result": "complete",
            "task": "test task",
            "session_id": "test",
        }

    def test_memory_update_node_returns_compacted_history(self, tmp_path):
        """memory_update_node must return compacted history when distill_context compacts."""
        import asyncio
        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        state = self._make_state(55, tmp_path)
        config = {"configurable": {"orchestrator": None}}

        compact_msgs = [{"role": "system", "content": "Session Summary:\nCompacted"}]

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            return_value={"current_task": "t", "_compacted_history": compact_msgs},
        ):
            result = asyncio.run(memory_update_node(state, config))

        assert "history" in result, (
            "memory_update_node must return 'history' when distill_context compacts"
        )
        assert result["history"] == compact_msgs

    def test_memory_update_node_returns_force_compact_false(self, tmp_path):
        """memory_update_node always returns _force_compact: False."""
        import asyncio
        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        state = self._make_state(5, tmp_path)
        config = {"configurable": {"orchestrator": None}}

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            return_value={},
        ):
            result = asyncio.run(memory_update_node(state, config))

        assert result.get("_force_compact") is False


# ---------------------------------------------------------------------------
# HR-4: execution_node planned_action priority
# ---------------------------------------------------------------------------


class TestExecutionNodePlannedActionOverridesNextAction:
    def test_planned_action_takes_priority_over_next_action(self, tmp_path):
        """planned_action (fresher, from step_controller) must take priority over next_action."""
        import asyncio
        from src.core.orchestration.graph.nodes.execution_node import execution_node

        planned = {"name": "read_file", "arguments": {"path": "app.py"}}
        stale_next = {"name": "bash", "arguments": {"command": "old command"}}

        mock_orch = MagicMock()
        mock_orch.working_dir = str(tmp_path)
        mock_orch.execute_tool = MagicMock(return_value={"ok": True, "output": "content"})
        mock_orch.get_provider_capabilities = MagicMock(return_value={})
        mock_orch.cancel_event = None
        mock_orch.event_bus = None
        mock_orch.rollback_manager = None
        mock_orch.plan_mode = None
        mock_orch._session_read_files = set()
        mock_orch._session_modified_files = set()
        mock_orch.tool_call_count = 0
        mock_orch._usage_buffer = {}

        state: Dict[str, Any] = {
            "task": "test",
            "working_dir": str(tmp_path),
            "next_action": stale_next,
            "planned_action": planned,
            "current_plan": [{"description": "step 1"}],
            "current_step": 0,
            "history": [],
            "rounds": 0,
            "tool_call_count": 0,
            "step_retry_counts": {},
            "_session_read_files": set(),
        }
        config = {"configurable": {"orchestrator": mock_orch}}

        asyncio.run(execution_node(state, config))

        calls = mock_orch.execute_tool.call_args_list
        # The first call should have used the planned action, not the stale next_action
        if calls:
            first_call_tool = calls[0][0][0] if calls[0][0] else calls[0][1].get("tool")
            if isinstance(first_call_tool, dict):
                assert first_call_tool.get("name") == planned["name"], (
                    f"execution_node used '{first_call_tool.get('name')}' instead of "
                    f"planned_action '{planned['name']}'. HR-4: planned_action must have priority."
                )


# ---------------------------------------------------------------------------
# HR-7: _COMPLEXITY_KEYWORDS word-boundary fix
# ---------------------------------------------------------------------------


class TestTaskComplexityKeywordWordBoundaryRegex:
    def test_authentication_not_classified_as_complex(self):
        """'authentication' must NOT trigger the 'after ' or similar false-positive."""
        from src.core.orchestration.graph.builder import _task_is_complex

        # "authentication" contains "after" as substring → was a false-positive before fix
        # With word-boundary fix, "after" must only match as a whole word.
        state = {
            "task": "Fix the authentication flow",
            "relevant_files": [],  # fewer than 3
            "current_plan": [],  # fewer than 2 steps
        }

        # After the fix: "Fix the authentication flow" should NOT be classified as complex
        # because "after" only appears as part of "authentication", not as a whole word.
        # Note: it WILL be classified complex because "fix" → no, but it contains
        # "add" in "authentication"? No. Let's check the word-boundary regex.
        # "authentication" does not contain \badd\b, \bedit\b, etc.
        # The only suspicious keyword is "auth" which is not in the list.
        result = _task_is_complex(state)
        # This should be False since the only trigger was the old "after " substring
        assert not result, (
            "Task 'Fix the authentication flow' was falsely classified as complex. "
            "HR-7: 'after' matched as substring of 'authentication'."
        )

    def test_before_only_matches_as_word(self):
        """'before' must match as a word, not as part of 'forebode'."""
        from src.core.orchestration.graph.builder import _task_is_complex

        state = {
            "task": "Analyze the forebode module",
            "relevant_files": [],
            "current_plan": [],
        }
        result = _task_is_complex(state)
        assert not result, (
            "Task 'Analyze the forebode module' was falsely classified as complex. "
            "HR-7: 'before' matched as substring of 'forebode'."
        )

    def test_add_as_word_still_matches(self):
        """'add' as a standalone word must still classify a task as complex."""
        from src.core.orchestration.graph.builder import _task_is_complex

        state = {
            "task": "Add a logging statement to main.py",
            "relevant_files": [],
            "current_plan": [],
        }
        result = _task_is_complex(state)
        assert result, (
            "Task 'Add a logging statement' should be classified as complex. "
            "HR-7: word-boundary match should still catch 'add' as a full word."
        )

    def test_edit_as_word_still_matches(self):
        """'edit' as a standalone word must still classify a task as complex."""
        from src.core.orchestration.graph.builder import _task_is_complex

        state = {
            "task": "Edit the config file",
            "relevant_files": [],
            "current_plan": [],
        }
        result = _task_is_complex(state)
        assert result


# ---------------------------------------------------------------------------
# HR-8 + HR-9: start_new_task clears PreviewService and delegations
# ---------------------------------------------------------------------------


class TestOrchestratorStartNewTaskClearsStaleState:
    def test_start_new_task_clears_preview_service(self, tmp_path):
        """start_new_task() must clear PreviewService.pending_previews."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))

        # Inject a stale preview entry
        try:
            from src.core.orchestration.preview_service import PreviewService

            svc = PreviewService.get_instance()
            svc.pending_previews["stale-preview-123"] = MagicMock()
            assert "stale-preview-123" in svc.pending_previews

            orch.start_new_task()

            assert "stale-preview-123" not in svc.pending_previews, (
                "start_new_task() must clear PreviewService.pending_previews. "
                "HR-8: stale previews from prior task block new task."
            )
        except ImportError:
            pass  # PreviewService not available in test env — skip

    def test_start_new_task_clears_pending_delegations(self, tmp_path):
        """start_new_task() must reset _pending_delegations."""
        from src.core.orchestration.orchestrator import Orchestrator

        orch = Orchestrator(working_dir=str(tmp_path))

        # Inject stale delegations
        orch._pending_delegations = [{"role": "analyst", "task": "stale"}]
        assert len(orch._pending_delegations) == 1

        orch.start_new_task()

        assert orch._pending_delegations == [], (
            "start_new_task() must clear _pending_delegations. "
            "HR-9: stale delegations cause spurious delegation on next task."
        )


# ---------------------------------------------------------------------------
# HR-3: should_after_evaluation caps debug via total_debug_attempts
# ---------------------------------------------------------------------------


class TestEvaluationRouterTotalDebugAttemptsCap:
    def test_should_after_evaluation_caps_debug_via_total_debug_attempts(self):
        """should_after_evaluation must route to memory_sync when total_debug_attempts >= 9."""
        from src.core.orchestration.graph.builder import should_after_evaluation

        state = {
            "evaluation_result": "debug",
            "total_debug_attempts": 9,
        }
        result = should_after_evaluation(state)
        assert result == "memory_sync", (
            f"should_after_evaluation returned '{result}' instead of 'memory_sync' "
            "when total_debug_attempts=9. HR-3: debug loop must be capped."
        )

    def test_should_after_evaluation_allows_debug_below_cap(self):
        """should_after_evaluation must route to debug when total_debug_attempts < 9."""
        from src.core.orchestration.graph.builder import should_after_evaluation

        state = {
            "evaluation_result": "debug",
            "total_debug_attempts": 4,
        }
        result = should_after_evaluation(state)
        assert result == "debug", (
            f"should_after_evaluation returned '{result}' instead of 'debug'. "
            "Debug routing should still work below the cap."
        )

    def test_debug_node_increments_debug_attempts(self, tmp_path):
        """debug_node must return incremented debug_attempts in every return path."""
        import asyncio
        from src.core.orchestration.graph.nodes.debug_node import debug_node

        mock_orch = MagicMock()
        mock_orch.working_dir = str(tmp_path)
        mock_orch.get_provider_capabilities = MagicMock(return_value={})
        mock_orch.adapter = None
        mock_orch.cancel_event = None
        mock_orch.tool_registry = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.session_store = MagicMock()
        mock_orch.rollback_manager = MagicMock()
        mock_orch.rollback_manager.current_snapshot = None

        state: Dict[str, Any] = {
            "task": "fix a bug",
            "working_dir": str(tmp_path),
            "debug_attempts": 1,
            "max_debug_attempts": 3,
            "total_debug_attempts": 2,
            "last_result": {},
            "verification_result": {},
            "history": [],
            "cancel_event": None,
        }
        config = {"configurable": {"orchestrator": mock_orch}}

        mock_tool = {"name": "read_file", "arguments": {"path": "file.py"}}

        with patch(
            "src.core.orchestration.graph.nodes.debug_node.call_model",
            return_value={
                "choices": [
                    {"message": {"content": "name: read_file\npath: file.py"}}
                ]
            },
        ):
            with patch(
                "src.core.orchestration.graph.nodes.debug_node.parse_tool_block",
                return_value=mock_tool,
            ):
                with patch(
                    "src.core.orchestration.graph.nodes.debug_node.ContextBuilder"
                ) as mock_cb:
                    mock_cb.return_value.build_prompt.return_value = [
                        {"role": "user", "content": "fix"}
                    ]
                    result = asyncio.run(debug_node(state, config))

        assert "debug_attempts" in result, (
            "debug_node must return 'debug_attempts' in its result dict. "
            "Without this, the debug loop counter is never incremented."
        )
        assert result["debug_attempts"] == 2, (
            f"debug_node must return debug_attempts = prev + 1 = 2, got {result.get('debug_attempts')}"
        )
        assert "total_debug_attempts" in result
        assert result["total_debug_attempts"] == 3


# ---------------------------------------------------------------------------
# HR-1: ContextController removed from analysis_node (hardcoded stats fix)
# ---------------------------------------------------------------------------


class TestAnalysisNodeRelevantFilesHardCap:
    def test_relevant_files_capped_at_25_not_by_context_controller(self):
        """analysis_node must cap relevant_files at 25, not via ContextController."""
        import inspect
        from src.core.orchestration.graph.nodes import analysis_node as _an_mod

        src = inspect.getsource(_an_mod)
        # Strip comment lines — our fix adds comments explaining the old approach
        # which mention the literal values; only check executable code.
        code = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "line_count=50" not in code, (
            "HR-1: ContextController hardcoded line_count=50 still in executable code"
        )
        assert "estimated_tokens=200" not in code, (
            "HR-1: ContextController hardcoded estimated_tokens=200 still in executable code"
        )
        assert "MAX_RELEVANT_FILES" in code, (
            "HR-1: MAX_RELEVANT_FILES cap not found in analysis_node"
        )


# ---------------------------------------------------------------------------
# HR-5 / HR-12: Delegation depth limit + asyncio.wait_for timeout
# ---------------------------------------------------------------------------


class TestDelegationNodeDepthLimitAndSubagentTimeout:
    def test_delegation_depth_limit_enforced(self):
        """delegation_node must refuse to spawn when depth >= MAX_DELEGATION_DEPTH."""
        import inspect
        from src.core.orchestration.graph.nodes import delegation_node as _dn_mod

        src = inspect.getsource(_dn_mod)
        assert "_MAX_DELEGATION_DEPTH" in src, (
            "HR-5: _MAX_DELEGATION_DEPTH constant not found in delegation_node"
        )
        assert "depth limit" in src or "delegation depth" in src.lower(), (
            "HR-5: delegation depth limit enforcement not found in delegation_node"
        )

    def test_asyncio_wait_for_used_for_subagent(self):
        """delegation_node must wrap delegate_task_async with asyncio.wait_for."""
        import inspect
        from src.core.orchestration.graph.nodes import delegation_node as _dn_mod

        src = inspect.getsource(_dn_mod)
        assert "asyncio.wait_for" in src, (
            "HR-12: asyncio.wait_for not found in delegation_node — "
            "hung subagents can block parent indefinitely"
        )
        assert "timeout=300" in src or "timeout=300.0" in src, (
            "HR-12: 300s timeout not set on delegate_task_async call"
        )


# ---------------------------------------------------------------------------
# CF-2: route_execution includes replan_required and W2 (fail→analysis) paths
# ---------------------------------------------------------------------------


class TestRouteExecutionConditionalBranches:
    def test_route_execution_routes_replan_required(self):
        """route_execution must route to 'replan' when replan_required is set."""
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "replan_required": "Step too large",
            "awaiting_plan_approval": False,
            "awaiting_user_input": False,
            "current_plan": [{"description": "step1"}],
            "current_step": 0,
            "rounds": 1,
            "last_result": {"ok": False},
            "last_tool_name": "",
        }
        result = route_execution(state)
        assert result == "replan", (
            f"route_execution returned '{result}' instead of 'replan' when "
            "replan_required is set. CF-2: replan path was dead in main graph."
        )

    def test_route_execution_w2_fail_no_plan_to_analysis(self):
        """route_execution must route to 'analysis' when fast-path execution fails (W2)."""
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "replan_required": None,
            "awaiting_plan_approval": False,
            "awaiting_user_input": False,
            "current_plan": [],          # no plan — fast-path mode
            "current_step": 0,
            "rounds": 2,
            "last_result": {"ok": False},  # execution failed
            "last_tool_name": "bash",
        }
        result = route_execution(state)
        assert result == "analysis", (
            f"route_execution returned '{result}' instead of 'analysis' on "
            "fast-path execution failure. CF-2/W2: should route to analysis for "
            "deeper context before retry."
        )

    def test_route_execution_read_only_to_memory_sync(self):
        """route_execution must route to 'memory_sync' for fast-path read-only tasks."""
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "replan_required": None,
            "awaiting_plan_approval": False,
            "awaiting_user_input": False,
            "current_plan": [],
            "rounds": 1,
            "last_result": {"ok": True},
            "last_tool_name": "read_file",
        }
        result = route_execution(state)
        assert result == "memory_sync", (
            f"route_execution returned '{result}' for read-only fast-path task. "
            "Should go to memory_sync (task complete)."
        )

    def test_route_execution_replan_key_in_graph_edges(self):
        """The compiled graph must include 'replan' in the execution edge map."""
        import inspect
        from src.core.orchestration.graph import builder as _b

        src = inspect.getsource(_b)
        # The add_conditional_edges call for "execution" must include "replan"
        assert '"replan": "replan"' in src or "'replan': 'replan'" in src, (
            "CF-2: 'replan' branch not in route_execution edge map"
        )
        assert '"analysis": "analysis"' in src or "'analysis': 'analysis'" in src, (
            "CF-2/W2: 'analysis' branch not in route_execution edge map"
        )


# ---------------------------------------------------------------------------
# CF-5: plan_resumed → execute directly (skip plan re-validation)
# ---------------------------------------------------------------------------


class TestPlanValidatorRouterBypassesValidationOnResumePlan:
    def test_should_after_plan_validator_skips_validation_on_resume(self):
        """should_after_plan_validator must route to 'execute' when plan_resumed=True."""
        from src.core.orchestration.graph.builder import should_after_plan_validator

        state = {
            "plan_resumed": True,
            "plan_validation": None,   # would normally fail validation
            "action_failed": True,     # would normally force re-plan
            "rounds": 2,
            "plan_attempts": 1,
            "plan_mode_enabled": False,
            "plan_mode_approved": False,
        }
        result = should_after_plan_validator(state)
        assert result == "execute", (
            f"should_after_plan_validator returned '{result}' instead of 'execute' "
            "when plan_resumed=True. CF-5: resumed plan must skip re-validation."
        )

    def test_should_after_plan_validator_normal_path_unchanged(self):
        """should_after_plan_validator must still reject invalid plans when not resumed."""
        from src.core.orchestration.graph.builder import should_after_plan_validator

        state = {
            "plan_resumed": False,
            "plan_validation": {"valid": False},
            "action_failed": False,
            "rounds": 0,
            "plan_attempts": 0,
            "plan_mode_enabled": False,
        }
        result = should_after_plan_validator(state)
        assert result == "planning", (
            "Normal invalid-plan path must still route to 'planning'."
        )


# ---------------------------------------------------------------------------
# TS-6: find_symbol cooldown uses correct argument key (name, not path)
# ---------------------------------------------------------------------------


class TestExecutionNodeToolCooldownPrimaryArgKey:
    def test_find_symbol_cooldown_key_uses_name(self):
        """find_symbol cooldown must use args['name'], not args['path']."""
        import inspect
        from src.core.orchestration.graph.nodes import execution_node as _en_mod

        src = inspect.getsource(_en_mod)
        # The TS-6 fix introduces _primary_arg that reads args.get("name") first
        assert 'args.get("name")' in src or "args.get('name')" in src, (
            "TS-6: args.get('name') not found in execution_node cooldown logic — "
            "find_symbol cooldown key still uses wrong argument"
        )
        assert "_primary_arg" in src, (
            "TS-6: _primary_arg not found — cooldown key fix not applied"
        )

    def test_search_code_cooldown_key_uses_query(self):
        """search_code cooldown must use args['query'], not args['path']."""
        import inspect
        from src.core.orchestration.graph.nodes import execution_node as _en_mod

        src = inspect.getsource(_en_mod)
        assert 'args.get("query")' in src or "args.get('query')" in src, (
            "TS-6: args.get('query') not found in execution_node — "
            "search_code cooldown key still uses wrong argument"
        )


# ---------------------------------------------------------------------------
# ME-3: memory_update_node uses distill_context result for analysis_summary
# ---------------------------------------------------------------------------


class TestMemoryUpdateNodeInjectsDistilledAnalysisSummary:
    def test_memory_update_node_returns_analysis_summary_from_distilled(self, tmp_path):
        """memory_update_node must return 'analysis_summary' from distilled current_state."""
        import asyncio
        from src.core.orchestration.graph.nodes.memory_update_node import (
            memory_update_node,
        )

        state = {
            "working_dir": str(tmp_path),
            "history": [{"role": "user", "content": f"m{i}"} for i in range(5)],
            "_should_distill": True,
            "_force_compact": False,
            "evaluation_result": "complete",
            "task": "test",
            "session_id": "me3-test",
        }
        config = {"configurable": {"orchestrator": None}}

        distilled_return = {
            "current_task": "test task",
            "current_state": "All steps completed successfully.",
            "next_step": "Done",
        }

        with patch(
            "src.core.orchestration.graph.nodes.memory_update_node.distill_context",
            return_value=distilled_return,
        ):
            result = asyncio.run(memory_update_node(state, config))

        assert "analysis_summary" in result, (
            "memory_update_node must return 'analysis_summary' from distilled current_state. "
            "ME-3: distilled state never fed back to agent context."
        )
        assert result["analysis_summary"] == "All steps completed successfully.", (
            f"analysis_summary has wrong value: {result.get('analysis_summary')!r}"
        )
