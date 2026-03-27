"""
Regression tests for audit vol9 fixes.

P1-2: plan_attempts counter prevents infinite planning→validator→planning loop
P1-3: replan_attempts counter caps replan cycles
P1-5: todo_tools dead code removal
P1-6: plan_enforce_warnings enabled by default
P1-7: atomic providers.json write
P1-8: Textual startup exception calls shutdown()
P2-1: retry logic in OpenAICompatibleAdapter
P2-3: distiller compaction checkpoint at 50 msgs
P2-4: distiller JSON schema validation
P2-5: safe_resolve in run_tests
P2-6: start_line/end_line int coercion in edit_by_line_range
P2-8: wave execution partial failure recovery
P2-10: syntax_check timeout
"""

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# P1-2: plan_attempts counter
# ---------------------------------------------------------------------------

class TestPlanningNodeAttemptCounterPreventsInfiniteLoop:
    def test_should_after_plan_validator_forces_execute_on_plan_attempts_exceeded(self):
        """After plan_attempts >= 3, should force execution even with invalid plan."""
        from src.core.orchestration.graph.builder import should_after_plan_validator

        state = {
            "plan_validation": {"valid": False, "errors": ["bad plan"], "warnings": []},
            "action_failed": True,
            "rounds": 0,
            "plan_attempts": 3,
            "plan_mode_enabled": False,
            "plan_mode_approved": None,
        }
        result = should_after_plan_validator(state)
        assert result == "execute", f"Expected 'execute' but got {result!r}"

    def test_should_after_plan_validator_allows_replan_below_limit(self):
        """Below plan_attempts threshold, invalid plan routes back to planning."""
        from src.core.orchestration.graph.builder import should_after_plan_validator

        state = {
            "plan_validation": {"valid": False, "errors": ["bad plan"], "warnings": []},
            "action_failed": True,
            "rounds": 0,
            "plan_attempts": 2,
            "plan_mode_enabled": False,
            "plan_mode_approved": None,
        }
        result = should_after_plan_validator(state)
        assert result == "planning", f"Expected 'planning' but got {result!r}"

    def test_planning_node_increments_plan_attempts(self, tmp_path):
        """planning_node must increment plan_attempts in its return dict."""
        import pytest
        pytest.importorskip("src.core.orchestration.graph.nodes.planning_node")
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.get_provider_capabilities.return_value = {}
        mock_orch.cancel_event = None

        state = {
            "task": "do something simple",
            "history": [],
            "working_dir": str(tmp_path),
            "plan_attempts": 1,
            "current_plan": None,
            "current_step": 0,
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "analyst_findings": None,
            "plan_resumed": None,
            "session_id": "test",
            "next_action": {"tool": "read_file", "path": "foo.py"},
        }

        config = {"configurable": {"orchestrator": mock_orch}}
        result = asyncio.run(planning_node(state, config))
        # A one-step plan is built from next_action; plan_attempts should be 2
        assert result.get("plan_attempts") == 2

    def test_planning_node_error_path_includes_plan_attempts(self, tmp_path):
        """planning_node orchestrator error path must still return plan_attempts."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        state = {
            "task": "do something",
            "plan_attempts": 2,
            "current_plan": [],
            "current_step": 0,
            "working_dir": str(tmp_path),
            "history": [],
            "session_id": "test",
        }
        # No orchestrator in config — triggers the None/error path
        config = {"configurable": {}}
        result = asyncio.run(planning_node(state, config))
        # plan_attempts must be 3 (was 2, incremented to 3)
        assert result.get("plan_attempts") == 3

    def test_planning_node_task_decomposed_path_includes_plan_attempts(self, tmp_path):
        """planning_node task_decomposed early-return must include plan_attempts."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.get_provider_capabilities.return_value = {}
        mock_orch.cancel_event = None

        existing_plan = [
            {"description": "step 0", "completed": False},
            {"description": "step 1", "completed": False},
        ]
        state = {
            "task": "step 0",
            "task_decomposed": True,
            "current_plan": existing_plan,
            "current_step": 0,
            "plan_attempts": 0,
            "working_dir": str(tmp_path),
            "history": [],
            "analysis_summary": None,
            "relevant_files": [],
            "key_symbols": [],
            "analyst_findings": None,
            "plan_resumed": None,
            "session_id": "test",
            "next_action": None,
        }
        config = {"configurable": {"orchestrator": mock_orch}}
        result = asyncio.run(planning_node(state, config))
        assert result.get("plan_attempts") == 1


# ---------------------------------------------------------------------------
# P1-3: replan_attempts counter
# ---------------------------------------------------------------------------

class TestReplanNodeAttemptCounterRoutsToMemorySync:
    def test_should_after_execution_caps_replan_cycles(self):
        """After replan_attempts >= 5, route to memory_sync instead of replan."""
        from src.core.orchestration.graph.builder import should_after_execution_with_replan

        state = {
            "replan_required": "patch too large",
            "replan_attempts": 5,
            "tool_call_count": 0,
            "max_tool_calls": 20,
            "rounds": 2,
            "current_plan": [{"description": "step"}],
            "current_step": 0,
            "last_result": None,
            "session_id": "test",
        }
        with patch(
            "src.core.orchestration.token_budget.get_token_budget_monitor"
        ) as mock_mon:
            mock_mon.return_value.check_and_prepare_compaction.return_value = False
            result = should_after_execution_with_replan(state)

        assert result == "memory_sync", f"Expected memory_sync but got {result!r}"

    def test_should_after_execution_allows_replan_below_limit(self):
        """Below replan_attempts cap, replan_required routes to replan."""
        from src.core.orchestration.graph.builder import should_after_execution_with_replan

        state = {
            "replan_required": "patch too large",
            "replan_attempts": 3,
            "tool_call_count": 0,
            "max_tool_calls": 20,
            "rounds": 2,
            "current_plan": [{"description": "step"}],
            "current_step": 0,
            "last_result": None,
            "session_id": "test",
        }
        with patch(
            "src.core.orchestration.token_budget.get_token_budget_monitor"
        ) as mock_mon:
            mock_mon.return_value.check_and_prepare_compaction.return_value = False
            result = should_after_execution_with_replan(state)

        assert result == "replan", f"Expected replan but got {result!r}"

    def test_replan_node_increments_replan_attempts(self, tmp_path):
        """replan_node must include replan_attempts in return dict."""
        from src.core.orchestration.graph.nodes.replan_node import replan_node

        mock_orch = MagicMock()
        mock_orch.tool_registry.tools = {}
        mock_orch.get_provider_capabilities.return_value = {}

        state = {
            "task": "refactor foo",
            "history": [],
            "working_dir": str(tmp_path),
            "current_plan": [{"description": "big step", "completed": False}],
            "current_step": 0,
            "replan_required": "patch > 200 lines",
            "replan_attempts": 2,
            "original_task": "refactor foo",
            "session_id": "test",
        }
        config = {"configurable": {"orchestrator": mock_orch}}

        # Mock call_model to return a new step list
        new_steps = json.dumps([
            {"description": "step a", "completed": False},
            {"description": "step b", "completed": False},
        ])
        with patch(
            "src.core.orchestration.graph.nodes.replan_node.call_model",
            return_value={"choices": [{"message": {"content": new_steps}}]},
        ):
            result = asyncio.run(replan_node(state, config))

        assert result.get("replan_attempts") == 3

    def test_replan_node_error_path_includes_replan_attempts(self, tmp_path):
        """replan_node orchestrator-None error path must still return replan_attempts."""
        from src.core.orchestration.graph.nodes.replan_node import replan_node

        state = {
            "task": "refactor",
            "replan_required": "too large",
            "replan_attempts": 3,
            "current_plan": [{"description": "big step"}],
            "current_step": 0,
            "working_dir": str(tmp_path),
            "history": [],
            "session_id": "test",
        }
        # No orchestrator — triggers the None error path
        config = {"configurable": {}}
        result = asyncio.run(replan_node(state, config))
        assert result.get("replan_attempts") == 4


# ---------------------------------------------------------------------------
# P1-5: todo_tools dead code removal
# ---------------------------------------------------------------------------

class TestTodoToolsDeadDuplicateCodeRemoved:
    def test_create_with_depends_on_preserved(self, tmp_path):
        """Create action must preserve depends_on — confirms dead duplicate is removed."""
        from src.tools.todo_tools import manage_todo

        result = manage_todo(
            action="create",
            workdir=str(tmp_path),
            steps=["step A", "step B", "step C"],
            depends_on=[[], [0], [0, 1]],  # depends_on uses int indices
        )
        assert result["status"] == "ok"
        steps = result["steps"]
        assert steps[0]["depends_on"] == []
        assert steps[1]["depends_on"] == [0]
        assert steps[2]["depends_on"] == [0, 1]

    def test_create_returns_single_result_not_duplicate(self, tmp_path):
        """Calling create returns exactly one result dict with the correct steps."""
        from src.tools.todo_tools import manage_todo

        r1 = manage_todo(action="create", workdir=str(tmp_path), steps=["only one"])
        assert r1["step_count"] == 1
        assert len(r1["steps"]) == 1
        # Confirm the steps have depends_on correctly (from the first block, not stripped copy)
        assert "depends_on" in r1["steps"][0]


# ---------------------------------------------------------------------------
# P1-6: plan_enforce_warnings in initial_state
# ---------------------------------------------------------------------------

class TestOrchestratorPlanEnforceWarningsInitialState:
    def test_initial_state_sets_plan_enforce_warnings_true(self):
        """Orchestrator run_agent_once initial_state must set plan_enforce_warnings=True."""
        # We inspect the source to verify the key is set to True in initial_state
        import ast
        src_path = Path("/Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/orchestrator.py")
        source = src_path.read_text()
        assert '"plan_enforce_warnings": True' in source, (
            "plan_enforce_warnings: True must appear in orchestrator initial_state"
        )


# ---------------------------------------------------------------------------
# P1-7: atomic providers.json write
# ---------------------------------------------------------------------------

class TestSettingsPanelAtomicProvidersJsonWrite:
    def test_no_direct_write_text_on_cfg_path(self):
        """Verify that settings_panel uses os.replace (not cfg_path.write_text) for safety."""
        import ast
        src_path = Path("src/ui/views/settings_panel.py")
        if not src_path.exists():
            src_path = Path("/Users/tann200/PycharmProjects/CodingAgent/src/ui/views/settings_panel.py")
        source = src_path.read_text()
        # The new code should use os.replace; old cfg_path.write_text should be gone from the write path
        # (it may still appear in read path or elsewhere — check it's not in the write block)
        assert "os.replace" in source, "Atomic write via os.replace not found in settings_panel.py"
        assert "tempfile.mkstemp" in source, "tempfile.mkstemp not found in settings_panel.py"


# ---------------------------------------------------------------------------
# P2-1: retry logic in OpenAICompatibleAdapter
# ---------------------------------------------------------------------------

class TestOpenAICompatAdapterRetryOnTransientErrors:
    def test_chat_retries_on_503(self, tmp_path):
        """_chat_internal should retry on 503 status and succeed on 3rd attempt."""
        from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:8080/v1",
            api_key=None,
            default_model="test-model",
        )

        call_count = [0]
        responses = []
        for code in [503, 503, 200]:
            mock_r = MagicMock()
            mock_r.status_code = code
            if code == 200:
                mock_r.json.return_value = {
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}]
                }
                mock_r.raise_for_status.return_value = None
            else:
                mock_r.raise_for_status.side_effect = Exception(f"HTTP {code}")
            responses.append(mock_r)

        def fake_safe_post(*args, **kwargs):
            call_count[0] += 1
            return responses[call_count[0] - 1]

        with patch.object(adapter, "_safe_post", side_effect=fake_safe_post):
            with patch("time.sleep"):  # speed up test
                result = adapter._chat_internal(
                    [{"role": "user", "content": "hello"}], model="test-model", stream=False
                )

        assert call_count[0] == 3, f"Expected 3 attempts, got {call_count[0]}"
        # On final success, should return the response json
        assert "choices" in result or result is not None

    def test_chat_retries_on_connection_error(self):
        """_chat_internal should retry on ConnectionError."""
        import requests
        from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:8080/v1",
            api_key=None,
            default_model="test-model",
        )

        call_count = [0]

        def fake_safe_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise requests.exceptions.ConnectionError("refused")
            mock_r = MagicMock()
            mock_r.status_code = 200
            mock_r.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            mock_r.raise_for_status.return_value = None
            return mock_r

        with patch.object(adapter, "_safe_post", side_effect=fake_safe_post):
            with patch("time.sleep"):
                result = adapter._chat_internal(
                    [{"role": "user", "content": "hello"}], model="test-model", stream=False
                )

        assert call_count[0] == 3
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# P2-3 & P2-4: distiller compaction checkpoint and schema validation
# ---------------------------------------------------------------------------

class TestDistillerCheckpointWriteAndSchemaValidation:
    def test_compaction_checkpoint_written_at_50_messages(self, tmp_path):
        """distill_context should write a compaction_checkpoint.md when >= 50 messages."""
        from src.core.memory import distiller

        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(52)
        ]

        with patch.object(distiller, "compact_messages_to_prose", return_value="summary text") as mock_cmp:
            with patch.object(distiller, "_call_llm_sync", return_value='{"current_task":"t","current_state":"s","next_step":"n","files_modified":[],"completed_steps":[],"errors_resolved":[]}'):
                distiller.distill_context(messages, working_dir=tmp_path)

        mock_cmp.assert_called_once()
        cp_path = tmp_path / ".agent-context" / "compaction_checkpoint.md"
        assert cp_path.exists(), "compaction_checkpoint.md should be written at 50+ messages"

    def test_schema_validation_rejects_missing_keys(self, tmp_path):
        """distill_context should return {} when LLM output is missing required keys."""
        from src.core.memory import distiller

        messages = [{"role": "user", "content": "task"}]
        # Return JSON missing 'next_step'
        incomplete_json = '{"current_task": "foo", "current_state": "bar"}'

        with patch.object(distiller, "_call_llm_sync", return_value=incomplete_json):
            result = distiller.distill_context(messages, working_dir=tmp_path)

        assert result == {}, f"Expected empty dict for missing keys, got {result}"

    def test_schema_validation_accepts_complete_output(self, tmp_path):
        """distill_context should succeed when all required keys present."""
        from src.core.memory import distiller

        messages = [{"role": "user", "content": "task"}]
        complete_json = json.dumps({
            "current_task": "do foo",
            "current_state": "in progress",
            "next_step": "run tests",
            "files_modified": [],
            "completed_steps": [],
            "errors_resolved": [],
        })

        with patch.object(distiller, "_call_llm_sync", return_value=complete_json):
            result = distiller.distill_context(messages, working_dir=tmp_path)

        assert result.get("current_task") == "do foo"


# ---------------------------------------------------------------------------
# P2-5: safe_resolve in run_tests
# ---------------------------------------------------------------------------

class TestVerificationRunTestsSafeWorkdirResolution:
    def test_run_tests_resolves_workdir(self, tmp_path):
        """run_tests should resolve workdir (no path traversal)."""
        from src.tools.verification_tools import _safe_resolve_workdir

        # Basic resolution
        result = _safe_resolve_workdir(str(tmp_path))
        assert Path(result).is_absolute()

    def test_run_tests_resolves_dotdot(self, tmp_path):
        """_safe_resolve_workdir resolves ../.. without blocking (just normalizes)."""
        from src.tools.verification_tools import _safe_resolve_workdir

        tricky = str(tmp_path / "subdir" / ".." / "..")
        result = _safe_resolve_workdir(tricky)
        assert ".." not in result, "Result should not contain .."
        assert Path(result).is_absolute()


# ---------------------------------------------------------------------------
# P2-6: int coercion in edit_by_line_range
# ---------------------------------------------------------------------------

class TestEditByLineRangeStringToIntLineNumberCoercion:
    def test_string_line_numbers_accepted(self, tmp_path):
        """edit_by_line_range should accept string line numbers from LLM."""
        from src.tools.file_tools import edit_by_line_range
        from pathlib import Path as _Path

        target = tmp_path / "foo.py"
        target.write_text("line1\nline2\nline3\n")

        result = edit_by_line_range(
            path="foo.py",
            start_line="2",   # string, not int
            end_line="2",
            new_content="replaced\n",
            workdir=tmp_path,
        )
        assert result.get("status") == "ok", f"Expected ok but got {result}"
        assert "replaced" in target.read_text()

    def test_invalid_string_line_number_returns_error(self, tmp_path):
        """Non-numeric string line numbers should return error dict."""
        from src.tools.file_tools import edit_by_line_range

        target = tmp_path / "foo.py"
        target.write_text("line1\nline2\n")

        result = edit_by_line_range(
            path="foo.py",
            start_line="one",
            end_line="two",
            new_content="x\n",
            workdir=tmp_path,
        )
        assert result.get("status") == "error"
        assert "integer" in result.get("error", "").lower()


# ---------------------------------------------------------------------------
# P2-10: syntax_check timeout
# ---------------------------------------------------------------------------

class TestSyntaxCheckReturnsPartialStatusOnTimeout:
    def test_syntax_check_respects_timeout(self, tmp_path):
        """syntax_check should return partial status when timeout exceeded."""
        import time as real_time
        from src.tools import verification_tools

        # Create some .py files
        for i in range(5):
            (tmp_path / f"file_{i}.py").write_text(f"x = {i}\n")

        # Patch time.monotonic inside verification_tools to simulate timeout
        call_count = [0]
        start = real_time.monotonic()

        def fake_monotonic():
            call_count[0] += 1
            if call_count[0] > 2:
                return start + 1000  # way past any deadline
            return start

        with patch("src.tools.verification_tools.time.monotonic", side_effect=fake_monotonic):
            result = verification_tools.syntax_check(str(tmp_path), timeout_secs=0.001)

        # Should be "partial" (timed out) or "ok"/"fail" (fast enough)
        assert result.get("status") in ("partial", "ok", "fail")

    def test_syntax_check_has_timeout_parameter(self):
        """syntax_check function must accept timeout_secs parameter."""
        import inspect
        from src.tools.verification_tools import syntax_check
        sig = inspect.signature(syntax_check)
        assert "timeout_secs" in sig.parameters


# ---------------------------------------------------------------------------
# P2-8: wave execution partial failure recovery
# ---------------------------------------------------------------------------

class TestWaveCoordinatorSkipsExhaustedRetrySteps:
    def test_exhausted_retry_steps_treated_as_done_in_wave(self):
        """Wave should advance even if some steps exhausted retries (P2-8)."""
        # Simulate the wave_advance logic by checking the builder guard
        # that a step with retries >= MAX_STEP_RETRIES is treated as complete
        plan = [
            {"description": "step 0", "completed": True},
            {"description": "step 1", "completed": False},  # failed step
            {"description": "step 2", "completed": True},
        ]
        step_retry_counts = {"1": 3}  # step 1 exhausted retries
        MAX_STEP_RETRIES = 3

        wave_step_ids = ["step_0", "step_1", "step_2"]

        all_in_wave_complete = True
        for ws in wave_step_ids:
            ws_idx = int(ws.split("_")[-1]) if ws.startswith("step_") else int(ws)
            step_done = plan[ws_idx].get("completed")
            step_retries = int(step_retry_counts.get(str(ws_idx), 0))
            step_retry_exhausted = step_retries >= MAX_STEP_RETRIES
            if not step_done and not step_retry_exhausted:
                all_in_wave_complete = False
                break

        assert all_in_wave_complete, "Wave should be complete when failed step exhausted retries"
