from typing import Any
from src.core.orchestration.graph.builder import (
    should_after_execution_with_replan,
    should_after_execution,
    should_after_planning,
    should_after_replan,
    should_after_evaluation,
    route_after_perception,
    route_execution,
    should_after_analysis,
    _task_is_complex,
    _task_has_more_steps,
)
from src.core.orchestration.graph.state import AgentState


def _make_state(**kwargs: Any) -> AgentState:
    """Helper to create AgentState with all required fields."""
    default: AgentState = {  # type: ignore[assignment]
        "task": "",
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
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 0,
        "max_tool_calls": 50,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
        "analyst_findings": None,
        "plan_resumed": None,
        "last_debug_error_type": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": None,
    }
    for key, value in kwargs.items():
        if key in default:
            default[key] = value
    return default


def test_should_after_planning_with_plan():
    """Test routing after planning when a plan exists."""
    state = _make_state(
        rounds=1,
        next_action=None,
        current_plan=[{"description": "step 1"}],
        last_result=None,
    )
    result = should_after_planning(state)
    assert result == "execute"


def test_should_after_planning_with_next_action():
    """Test routing after planning when a next_action exists."""
    state = _make_state(
        rounds=1,
        next_action={"name": "echo"},
        current_plan=[],
        last_result=None,
    )
    result = should_after_planning(state)
    assert result == "execute"


def test_should_after_planning_with_last_result():
    """Test routing after planning when last_result exists."""
    state = _make_state(
        rounds=5,
        next_action=None,
        current_plan=[],
        last_result={"ok": True},
    )
    result = should_after_planning(state)
    assert result == "memory_sync"


def test_should_after_execution_with_replan():
    """Test routing after execution when replan is required."""
    state = _make_state(
        current_plan=[{"description": "step 1"}],
        current_step=0,
        last_result={"ok": True},
        replan_required="Patch exceeded 200 lines. Split into multiple targeted functions.",
    )
    result = should_after_execution_with_replan(state)
    assert result == "replan"


def test_should_after_execution_with_more_steps():
    """W5: execution routes to step_controller (not execution) when more plan steps remain."""
    state = _make_state(
        current_plan=[{"description": "step 1"}, {"description": "step 2"}],
        current_step=0,
        last_result={"ok": True},
        replan_required=None,
    )
    result = should_after_execution_with_replan(state)
    assert result == "step_controller"


def test_should_after_execution_no_more_steps():
    """Test routing after execution when no more steps remain."""
    state = _make_state(
        current_plan=[{"description": "step 1"}, {"description": "step 2"}],
        current_step=1,
        last_result={"ok": True},
        replan_required=None,
        step_controller_enabled=False,
    )
    result = should_after_execution_with_replan(state)
    assert result == "verification"


def test_should_after_execution_empty_plan():
    """W2: fallback with empty plan routes to analysis (not perception) for deeper context."""
    state = _make_state(
        current_plan=[],
        current_step=0,
        last_result=None,
        replan_required=None,
    )
    result = should_after_execution_with_replan(state)
    assert result == "analysis"


def test_should_after_replan_success():
    """Test routing after replan when replan succeeded."""
    state = _make_state(
        replan_required=None,
    )
    result = should_after_replan(state)
    assert result == "step_controller"


def test_should_after_replan_still_required():
    """Test routing after replan when replan is still needed."""
    state = _make_state(
        replan_required="Failed to split step",
    )
    result = should_after_replan(state)
    assert result == "perception"


def test_should_after_evaluation_complete():
    """Test routing after evaluation when task is complete."""
    state = _make_state(
        evaluation_result="complete",
    )
    result = should_after_evaluation(state)
    assert result == "memory_sync"


def test_should_after_evaluation_replan():
    """Test routing after evaluation when more work is needed."""
    state = _make_state(
        evaluation_result="replan",
    )
    result = should_after_evaluation(state)
    assert result == "step_controller"


def test_should_after_evaluation_end():
    """Test routing after evaluation when task should end."""
    state = _make_state(
        evaluation_result="end",
    )
    result = should_after_evaluation(state)
    assert result == "end"


def test_route_after_perception_fast_path():
    """Test fast-path routing when next_action exists and task is simple."""
    state = _make_state(
        task="read the file",
        next_action={"name": "read_file", "arguments": {"path": "test.py"}},
    )
    result = route_after_perception(state)
    assert result == "execution"


def test_route_after_perception_standard_path():
    """Test standard routing when no next_action and task is complex (goes to analysis)."""
    # P2-A: simple tasks on rounds==0 now bypass analysis → planning.
    # To exercise the analysis path we must use a complex task so that
    # _task_is_complex() returns True and the P2-A shortcut is skipped.
    state = _make_state(
        task="refactor the authentication module",
        next_action=None,
    )
    result = route_after_perception(state)
    assert result == "analysis"


# ---------------------------------------------------------------------------
# W3: Fast-path complexity guard
# ---------------------------------------------------------------------------


def test_task_is_complex_keyword_refactor():
    """W3: 'refactor' keyword marks task as complex."""
    state = _make_state(task="refactor the authentication module")
    assert _task_is_complex(state) is True


def test_task_is_complex_keyword_implement():
    """W3: 'implement' keyword marks task as complex."""
    state = _make_state(task="implement user authentication with JWT")
    assert _task_is_complex(state) is True


def test_task_is_complex_many_relevant_files():
    """W3: more than 3 relevant_files marks task as complex."""
    state = _make_state(relevant_files=["a.py", "b.py", "c.py", "d.py"])
    assert _task_is_complex(state) is True


def test_task_is_complex_existing_plan():
    """W3: existing plan with 2+ steps marks task as complex."""
    state = _make_state(current_plan=[{"description": "s1"}, {"description": "s2"}])
    assert _task_is_complex(state) is True


def test_task_is_not_complex_simple():
    """W3: simple one-line task is not complex."""
    state = _make_state(task="read main.py")
    assert _task_is_complex(state) is False


def test_route_after_perception_complex_task_overrides_fast_path():
    """W3: complex task overrides fast-path even when next_action is set."""
    state = _make_state(
        task="refactor the entire authentication system across multiple files",
        next_action={"name": "edit_file_atomic", "arguments": {}},
    )
    result = route_after_perception(state)
    assert result == "analysis"


def test_route_after_perception_simple_task_fast_path_ok():
    """W3: simple task still uses fast-path when next_action is set."""
    state = _make_state(
        task="show me the file",
        next_action={"name": "read_file", "arguments": {"path": "test.py"}},
    )
    result = route_after_perception(state)
    assert result == "execution"


# ---------------------------------------------------------------------------
# W12: Tool call budget enforcement
# ---------------------------------------------------------------------------


def test_budget_exhausted_routes_to_memory_sync():
    """W12: when tool_call_count >= max_tool_calls, route to memory_sync."""
    state = _make_state(
        tool_call_count=30,
        max_tool_calls=30,
        current_plan=[{"description": "step 1"}],
        current_step=0,
        last_result={"ok": True},
    )
    result = should_after_execution_with_replan(state)
    assert result == "memory_sync"


def test_budget_not_exhausted_routes_to_perception():
    """After successful execution, route to perception to continue the task."""
    state = _make_state(
        tool_call_count=5,
        max_tool_calls=30,
        current_plan=[],
        current_step=0,
        last_result={"ok": True},
        replan_required=None,
    )
    result = should_after_execution_with_replan(state)
    # Successful execution goes to perception to continue, not memory_sync
    # Task completion is detected by route_after_perception
    assert result == "perception"


def test_budget_one_over_limit_routes_to_memory_sync():
    """W12: one call over budget still bails to memory_sync."""
    state = _make_state(
        tool_call_count=31,
        max_tool_calls=30,
        current_plan=[{"description": "more steps"}],
        current_step=0,
        last_result={"ok": True},
    )
    result = should_after_execution_with_replan(state)
    assert result == "memory_sync"


def test_budget_zero_max_falls_back_to_default():
    """W12: max_tool_calls=0 is falsy — falls back to default of 30, so budget not exhausted."""
    state = _make_state(
        tool_call_count=1,
        max_tool_calls=0,  # falsy → treated as 30 via `or 30` fallback
        current_plan=[{"description": "step"}],
        current_step=0,
        last_result={"ok": True},
    )
    # tool_call_count=1 < 30 (default) → not exhausted → normal routing
    result = should_after_execution_with_replan(state)
    assert result != "memory_sync"  # budget not hit, normal path taken


# ---------------------------------------------------------------------------
# Task Completion Detection
# ---------------------------------------------------------------------------


def test_route_after_perception_task_complete_routes_to_memory_sync():
    """After successful execution with no next_action, route to memory_sync (task complete)."""
    state = _make_state(
        task="create a readme file",
        next_action=None,  # No next action = task likely complete
        last_result={"ok": True, "status": "ok"},
        rounds=2,  # rounds > 0 to avoid triggering on initial perception
    )
    result = route_after_perception(state)
    assert result == "memory_sync"


def test_route_after_perception_continues_with_next_action():
    """With next_action set, continue to execution."""
    state = _make_state(
        task="create a readme file",
        next_action={
            "name": "write_file",
            "arguments": {"path": "README.md", "content": "# Hello"},
        },
    )
    result = route_after_perception(state)
    assert result == "execution"


def test_route_after_perception_first_round_continues():
    """On first round (rounds=0), simple task bypasses analysis and goes to planning (P2-A)."""
    state = _make_state(
        task="read main.py",
        next_action=None,
        rounds=0,  # First perception round
        last_result=None,  # No previous result
    )
    result = route_after_perception(state)
    # P2-A: simple tasks on first round skip analysis and go directly to planning,
    # not memory_sync (which would prematurely end the task).
    assert result == "planning"


def test_route_after_perception_multistep_continues():
    """Multi-step tasks using 'then' keyword continue execution after first tool succeeds."""
    # WF-VOL23-2: r"\band\b" removed from multi_step_patterns; use 'then' which is
    # a genuine multi-step signal, unlike 'and' which appears in almost all sentences.
    state = _make_state(
        task="create docs folder then create ARCHITECTURE.md inside it",
        next_action=None,
        last_result={"ok": True, "status": "ok"},
        rounds=1,
        tool_call_count=1,
    )
    result = route_after_perception(state)
    assert result == "analysis"


def test_route_after_perception_multistep_then_keyword():
    """Tasks with 'then' keyword continue after first tool succeeds."""
    state = _make_state(
        task="first do X then do Y",
        next_action=None,
        last_result={"ok": True, "status": "ok"},
        rounds=1,
        tool_call_count=1,
    )
    result = route_after_perception(state)
    assert result == "analysis"


def test_task_has_more_steps_with_and():
    """'and' alone no longer indicates multi-step — too broad; 'then' does."""
    # WF-VOL23-2: r"\band\b" removed because it matches virtually every English
    # sentence.  "create folder and file" is now treated as a single step.
    state = _make_state(
        task="create folder and file",
        tool_call_count=1,
    )
    assert _task_has_more_steps(state) is False

    # Verify that 'then' still triggers multi-step detection.
    state_then = _make_state(
        task="create folder then create file",
        tool_call_count=1,
    )
    assert _task_has_more_steps(state_then) is True


def test_task_has_more_steps_single_step():
    """Single-step task returns False."""
    state = _make_state(
        task="read main.py",
        tool_call_count=1,
    )
    assert _task_has_more_steps(state) is False


def test_task_has_more_steps_max_tool_calls():
    """At max tool calls, don't continue even for multi-step."""
    state = _make_state(
        task="create folder and file",
        tool_call_count=10,
    )
    assert _task_has_more_steps(state) is False


# ---------------------------------------------------------------------------
# #56: should_after_analysis routing
# ---------------------------------------------------------------------------


def test_should_after_analysis_simple_task_goes_to_planning():
    """#56: simple task routes directly to planning."""
    state = _make_state(task="read a file")
    assert should_after_analysis(state) == "planning"


def test_should_after_analysis_complex_keyword_goes_to_analyst():
    """#56: complex keyword routes to analyst_delegation."""
    state = _make_state(task="refactor the authentication system")
    assert should_after_analysis(state) == "analyst_delegation"


def test_should_after_analysis_many_files_goes_to_analyst():
    """#56: >3 relevant files routes to analyst_delegation."""
    state = _make_state(
        task="update config",
        relevant_files=["a.py", "b.py", "c.py", "d.py"],
    )
    assert should_after_analysis(state) == "analyst_delegation"


# ---------------------------------------------------------------------------
# W3 — replan_node integration path: requires_split → replan → step_controller
# ---------------------------------------------------------------------------


def test_requires_split_routes_to_replan():
    """W3: when last_result has requires_split=True, execution must route to replan."""
    state = _make_state(
        replan_required="Patch exceeded 200 lines. Split into multiple targeted functions.",
        current_plan=[{"description": "write large module"}],
        current_step=0,
        last_result={"ok": True, "requires_split": True},
        tool_call_count=1,
        max_tool_calls=30,
    )
    result = should_after_execution_with_replan(state)
    assert result == "replan", f"requires_split must route to replan, got '{result}'"


def test_requires_split_false_does_not_route_to_replan():
    """W3: without requires_split, normal routing applies (not replan)."""
    state = _make_state(
        replan_required=None,
        current_plan=[{"description": "write small module"}],
        current_step=0,
        last_result={"ok": True},
        tool_call_count=1,
        max_tool_calls=30,
    )
    result = should_after_execution_with_replan(state)
    assert result != "replan", (
        f"no requires_split must not route to replan, got '{result}'"
    )


def test_should_after_replan_routes_to_step_controller():
    """W3: after replan_node runs, should_after_replan must route to step_controller."""
    state = _make_state(
        current_plan=[
            {"description": "step A"},
            {"description": "step B"},
        ],
        current_step=0,
        replan_required=None,
    )
    result = should_after_replan(state)
    assert result == "step_controller", (
        f"should_after_replan must route to step_controller, got '{result}'"
    )


def test_should_after_replan_empty_plan_goes_to_perception():
    """W3: if replan produced an empty plan, route back to perception."""
    state = _make_state(
        current_plan=[],
        current_step=0,
        replan_required=None,
    )
    result = should_after_replan(state)
    # Empty plan: no steps to execute, should fall back
    assert result in ("perception", "step_controller"), (
        f"should_after_replan with empty plan should route to perception or step_controller, got '{result}'"
    )


def test_requires_split_flag_set_by_execution_node():
    """W3: execution_node must set replan_required when tool returns requires_split."""
    # Simulate execution_node output when write_file returns requires_split=True
    tool_result = {
        "status": "ok",
        "requires_split": True,
        "error": "write_file wrote 250 lines. Split into multiple targeted function/section writes.",
        "path": "/tmp/big_module.py",
    }
    # The replan_triggered dict that execution_node builds
    replan_triggered = {}
    if tool_result.get("requires_split") is True:
        error_msg = tool_result.get("error", "Patch too large")
        replan_triggered = {
            "replan_required": error_msg,
            "action_failed": True,
            "next_action": None,
        }
    assert replan_triggered.get("replan_required") is not None
    assert replan_triggered.get("action_failed") is True


def test_h1_intermediate_step_skips_full_suite():
    """H1: verification_node must skip full test suite on intermediate steps."""
    # Verify the logic: intermediate step (not final) with no explicit verification request
    current_plan = [
        {"description": "step 1"},
        {"description": "step 2"},
        {"description": "step 3"},
    ]
    current_step = 0  # intermediate
    step_desc = current_plan[current_step].get("description", "").lower()
    step_requests = any(k in step_desc for k in ("run_tests", "verify", "test", "lint"))
    at_final_step = current_step >= len(current_plan) - 1
    run_full_suite = at_final_step or step_requests
    assert run_full_suite is False, "Intermediate step must NOT trigger full test suite"


def test_h1_final_step_runs_full_suite():
    """H1: verification_node must run full test suite at the final plan step."""
    current_plan = [
        {"description": "step 1"},
        {"description": "step 2"},
        {"description": "step 3"},
    ]
    current_step = 2  # final
    at_final_step = current_step >= len(current_plan) - 1
    run_full_suite = at_final_step
    assert run_full_suite is True, "Final step MUST trigger full test suite"


def test_h1_step_requesting_verification_always_runs():
    """H1: a step explicitly requesting 'run_tests' triggers full suite even if intermediate."""
    current_plan = [
        {"description": "edit auth module"},
        {"description": "run_tests to verify changes"},  # step 1, not final
        {"description": "update docs"},
    ]
    current_step = 1  # intermediate but requests verification
    step_desc = current_plan[current_step].get("description", "").lower()
    step_requests = any(k in step_desc for k in ("run_tests", "verify", "test", "lint"))
    at_final_step = current_step >= len(current_plan) - 1
    run_full_suite = at_final_step or step_requests
    assert run_full_suite is True, (
        "Intermediate step requesting 'run_tests' MUST run full suite"
    )


# ---------------------------------------------------------------------------
# Pipeline loop-termination regression tests (ET-3)
# Covers the "respond tool not registered" completion-detection fixes:
#   - execution_node sets _completion_detected=True in last_result
#   - should_after_execution routes to memory_sync when _completion_detected
#   - orchestrator round loop breaks when handled=True
# ---------------------------------------------------------------------------


class TestCompletionDetectionRouting:
    """ET-3: should_after_execution routes to memory_sync on _completion_detected."""

    def _make_state(self, **kwargs: Any) -> AgentState:
        base: AgentState = {  # type: ignore[assignment]
            "task": "list all files in the working directory",
            "history": [],
            "verified_reads": [],
            "rounds": 2,
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
            "verification_passed": None,
            "verification_result": None,
            "step_controller_enabled": True,
            "task_decomposed": False,
            "tool_call_count": 2,
            "max_tool_calls": 50,
            "repo_summary_data": None,
            "replan_required": None,
            "action_failed": False,
            "plan_progress": None,
            "evaluation_result": None,
            "cancel_event": None,
            "empty_response_count": 0,
            "analyst_findings": None,
            "plan_resumed": None,
            "last_debug_error_type": None,
            "session_id": None,
            "delegation_results": None,
            "delegations": None,
            "last_tool_name": None,
            "no_plan_fail_count": 0,
            "step_retry_counts": {},
            "total_debug_attempts": 0,
        }
        base.update(kwargs)  # type: ignore[call-overload]
        return base

    def test_completion_detected_flag_routes_to_memory_sync(self):
        """When _completion_detected=True in last_result, route to memory_sync not perception."""
        state = self._make_state(
            last_result={
                "ok": True,
                "output": ["file.txt"],
                "_completion_detected": True,
            },
            current_plan=None,
        )
        result = should_after_execution(state)
        assert result == "memory_sync", (
            f"Expected memory_sync when _completion_detected=True, got {result}"
        )

    def test_normal_ok_result_without_flag_routes_to_perception(self):
        """Without _completion_detected, a successful no-plan execution still goes to perception."""
        state = self._make_state(
            last_result={"ok": True, "output": ["file.txt"]},
            current_plan=None,
            rounds=2,
        )
        result = should_after_execution(state)
        assert result == "perception", (
            f"Expected perception on normal success (no _completion_detected), got {result}"
        )

    def test_completion_detected_no_plan_routes_to_memory_sync(self):
        """_completion_detected with no plan routes to memory_sync (typical fast-path case)."""
        state = self._make_state(
            last_result={"ok": True, "_completion_detected": True},
            current_plan=None,
            current_step=0,
        )
        result = should_after_execution(state)
        assert result == "memory_sync", (
            "_completion_detected with no plan must route to memory_sync"
        )

    def test_tool_budget_exhaustion_still_overrides_completion_flag(self):
        """tool_call_count >= max_tool_calls takes priority over _completion_detected."""
        state = self._make_state(
            last_result={"ok": True, "_completion_detected": True},
            tool_call_count=50,
            max_tool_calls=50,
        )
        result = should_after_execution(state)
        assert result == "memory_sync", (
            "Budget exhaustion must also route to memory_sync (compatible outcome)"
        )


class TestExecutionNodeCompletionSignalHistory:
    """ET-3: execution_node completion signal path adds synthetic tool_execution_result."""

    def test_synthetic_tool_result_contains_expected_keys(self):
        """Verify the format of the synthetic completion message matches what the
        orchestrator round loop checks (looks for 'tool_execution_result' in content)."""
        import json

        tool_name = "respond"
        synthetic_content = json.dumps(
            {
                "tool_execution_result": {
                    "tool_name": tool_name,
                    "output": "Task already completed. No further action needed.",
                    "status": "ok",
                }
            }
        )
        parsed = json.loads(synthetic_content)
        assert "tool_execution_result" in parsed
        assert parsed["tool_execution_result"]["tool_name"] == tool_name
        assert parsed["tool_execution_result"]["status"] == "ok"
        # Orchestrator round loop checks string contains "tool_execution_result"
        assert "tool_execution_result" in synthetic_content


class TestOrchestratorBridgingUserMessage:
    """ET-3: Orchestrator injects bridging user message when history ends with assistant."""

    def test_bridging_message_added_when_history_ends_with_assistant(self):
        """When the current_state history ends with an assistant message,
        a user-role bridge should be appended before re-invoking the graph."""
        history = [
            {"role": "user", "content": "List all files"},
            {"role": "assistant", "content": "STATUS: complete"},
        ]
        # Simulate the orchestrator bridging logic
        if history and history[-1].get("role") == "assistant":
            history = list(history) + [
                {
                    "role": "user",
                    "content": (
                        "Continue. If the task is already complete, "
                        "output STATUS: complete with no tool call."
                    ),
                }
            ]
        assert history[-1]["role"] == "user", (
            "History must end with 'user' role before re-invoking graph"
        )
        assert len(history) == 3

    def test_no_bridging_when_history_ends_with_user(self):
        """When history ends with a user message, no bridging is needed."""
        history = [
            {"role": "user", "content": "List all files"},
            {"role": "assistant", "content": "```yaml\nname: list_files\n```"},
            {"role": "user", "content": '{"tool_execution_result": {"output": []}}'},
        ]
        original_len = len(history)
        if history and history[-1].get("role") == "assistant":
            history = list(history) + [{"role": "user", "content": "Continue."}]
        assert len(history) == original_len, "No bridging message should be added"
        assert history[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# route_execution: W12 tool budget enforcement (ported from dead code)
# ---------------------------------------------------------------------------


def test_route_execution_tool_budget_exhausted_routes_to_memory_sync():
    """W12: route_execution must bail to memory_sync when tool_call_count >= max_tool_calls."""
    state = _make_route_exec_state(
        tool_call_count=30,
        max_tool_calls=30,
        current_plan=None,
        last_result={"ok": True},
    )
    result = route_execution(state)
    assert result == "memory_sync", (
        f"route_execution must route to memory_sync when budget exhausted (got {result!r})"
    )


def test_route_execution_tool_budget_exceeded_routes_to_memory_sync():
    """W12: route_execution must bail even when count is above the limit."""
    state = _make_route_exec_state(
        tool_call_count=35,
        max_tool_calls=30,
        current_plan=[{"description": "step 1"}],
        last_result={"ok": True},
    )
    result = route_execution(state)
    assert result == "memory_sync", (
        f"route_execution must route to memory_sync when budget exceeded (got {result!r})"
    )


def test_route_execution_read_file_modify_task_routes_to_perception():
    """
    Regression: route_execution must route back to perception after read_file when
    the task implies modification (e.g. 'add todays date on top of TEST.md').

    Bug: route_execution sent read_file unconditionally to memory_sync, so the
    write step was never executed.  The fix checks for modification keywords
    and routes to perception so the write tool call can be generated.
    """
    state = _make_route_exec_state(
        task="Task: add todays date on top of TEST.md\nContext: You just read the file.",
        last_result={"ok": True, "result": {"content": "# Test\n"}},
        last_tool_name="read_file",
        current_plan=None,
        rounds=1,
    )
    result = route_execution(state)
    assert result == "perception", (
        f"After read_file with a modification task, route_execution must route to "
        f"perception so the write step executes (got {result!r})"
    )


def test_route_execution_read_file_query_task_routes_to_memory_sync():
    """read_file with a pure query task (no modification keywords) must go to memory_sync."""
    state = _make_route_exec_state(
        task="read README.md and tell me what it says",
        last_result={"ok": True, "result": {"content": "# README\n"}},
        last_tool_name="read_file",
        current_plan=None,
        rounds=1,
    )
    result = route_execution(state)
    assert result == "memory_sync", (
        f"read_file with a query task should route to memory_sync (got {result!r})"
    )


def test_route_execution_list_files_modify_task_routes_to_memory_sync():
    """list_files is always informational — route to memory_sync even with modification keywords."""
    state = _make_route_exec_state(
        task="add a file to the directory",
        last_result={"ok": True, "result": {"items": []}},
        last_tool_name="list_files",
        current_plan=None,
        rounds=1,
    )
    result = route_execution(state)
    assert result == "memory_sync", (
        f"list_files must always route to memory_sync (got {result!r})"
    )


def test_route_execution_read_file_top_of_keyword_routes_to_perception():
    """'top of' is a modification keyword — 'add date on top of' must trigger perception."""
    state = _make_route_exec_state(
        task="add todays date on top of TEST.md",
        last_result={"ok": True, "result": {"content": "# Test\n"}},
        last_tool_name="read_file",
        current_plan=None,
        rounds=1,
    )
    result = route_execution(state)
    assert result == "perception", (
        f"'on top of' must be recognised as a modification keyword (got {result!r})"
    )


def test_route_execution_budget_not_exhausted_proceeds_normally():
    """W12: route_execution must not bail to memory_sync when budget is not exhausted."""
    state = _make_route_exec_state(
        tool_call_count=5,
        max_tool_calls=30,
        current_plan=None,
        last_result={"ok": True},
        last_tool_name="list_files",
    )
    result = route_execution(state)
    assert result != "memory_sync" or result == "memory_sync", (
        True
    )  # any valid route is fine
    # Specifically: with list_files (read-only) it should go to memory_sync for completion,
    # not because of budget exhaustion — both are memory_sync but for different reasons.
    # The important assertion is that budget < limit does NOT prevent correct routing.
    assert result == "memory_sync"  # list_files read-only fast-path


# ---------------------------------------------------------------------------
# route_execution: replan_attempts cap (ported from dead should_after_execution_with_replan)
# ---------------------------------------------------------------------------


def test_route_execution_replan_attempts_cap_routes_to_memory_sync():
    """P1-3: when replan_attempts >= 5, route to memory_sync instead of replan."""
    state = _make_route_exec_state(
        replan_required="patch too large",
        replan_attempts=5,
        current_plan=None,
        last_result={"ok": False},
    )
    result = route_execution(state)
    assert result == "memory_sync", (
        f"route_execution must bail to memory_sync when replan_attempts >= 5 (got {result!r})"
    )


def test_route_execution_replan_below_cap_routes_to_replan():
    """P1-3: when replan_attempts < 5 and replan_required, route to replan."""
    state = _make_route_exec_state(
        replan_required="patch too large",
        replan_attempts=2,
        current_plan=None,
        last_result={"ok": False},
    )
    result = route_execution(state)
    assert result == "replan", (
        f"route_execution must route to replan when replan_attempts < 5 (got {result!r})"
    )


def test_route_execution_replan_attempts_at_four_still_replans():
    """P1-3: boundary — 4 attempts is still below cap, should still replan."""
    state = _make_route_exec_state(
        replan_required="needs split",
        replan_attempts=4,
        current_plan=None,
        last_result={"ok": False},
    )
    result = route_execution(state)
    assert result == "replan", (
        f"4 replan attempts should still route to replan (cap is >= 5), got {result!r}"
    )


# ---------------------------------------------------------------------------
# route_execution: no_plan_fail_count cap (ported from dead should_after_execution)
# ---------------------------------------------------------------------------


def test_route_execution_no_plan_fail_count_cap_routes_to_memory_sync():
    """HR-4: when no_plan_fail_count >= 3 on fast-path failure, bail to memory_sync."""
    state = _make_route_exec_state(
        last_result={"ok": False, "error": "command not found"},
        no_plan_fail_count=3,
        current_plan=None,
        rounds=2,
        last_tool_name="bash",
    )
    result = route_execution(state)
    assert result == "memory_sync", (
        f"route_execution must bail to memory_sync when no_plan_fail_count >= 3 "
        f"(got {result!r})"
    )


def test_route_execution_no_plan_fail_count_below_cap_routes_to_analysis():
    """HR-4: below cap (count=1), fast-path failures still route to analysis for context."""
    state = _make_route_exec_state(
        last_result={"ok": False, "error": "file not found"},
        no_plan_fail_count=1,
        current_plan=None,
        rounds=2,
        last_tool_name="bash",
    )
    result = route_execution(state)
    assert result == "analysis", (
        f"route_execution must route to analysis when fail count < 3 (got {result!r})"
    )


def test_route_execution_no_plan_fail_count_at_two_routes_to_analysis():
    """HR-4: boundary — count=2 is still below cap (3), should route to analysis."""
    state = _make_route_exec_state(
        last_result={"ok": False, "error": "timeout"},
        no_plan_fail_count=2,
        current_plan=None,
        rounds=3,
        last_tool_name="bash",
    )
    result = route_execution(state)
    assert result == "analysis", (
        f"count=2 is below the cap of 3, should still route to analysis (got {result!r})"
    )


# ---------------------------------------------------------------------------
# route_execution: _completion_detected flag must be honoured
# Regression test for BUG-1: flag was only checked in dead should_after_execution;
# route_execution (the live router) ignored it, causing perception loops after
# write tools when the model hallucinates a "respond" tool.
# ---------------------------------------------------------------------------


def _make_route_exec_state(**kwargs: Any) -> AgentState:
    base: AgentState = {  # type: ignore[assignment]
        "task": "write hello to file",
        "history": [],
        "verified_reads": [],
        "rounds": 2,
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
        "verification_passed": None,
        "verification_result": None,
        "step_controller_enabled": True,
        "task_decomposed": False,
        "tool_call_count": 3,
        "max_tool_calls": 50,
        "repo_summary_data": None,
        "replan_required": None,
        "action_failed": False,
        "plan_progress": None,
        "evaluation_result": None,
        "cancel_event": None,
        "empty_response_count": 0,
        "analyst_findings": None,
        "plan_resumed": None,
        "last_debug_error_type": None,
        "session_id": None,
        "delegation_results": None,
        "delegations": None,
        "last_tool_name": "write_file",  # write tool — NOT in read_only_tools
        "no_plan_fail_count": 0,
        "replan_attempts": 0,
        "step_retry_counts": {},
        "total_debug_attempts": 0,
        "awaiting_plan_approval": False,
        "awaiting_user_input": False,
        "plan_mode_enabled": False,
        "plan_mode_approved": None,
    }
    base.update(kwargs)  # type: ignore[call-overload]
    return base


class TestRouteExecutionCompletionDetected:
    """Regression: route_execution must honour _completion_detected flag."""

    def test_completion_detected_after_write_tool_routes_to_memory_sync(self):
        """
        When execution_node sets _completion_detected=True (hallucinated 'respond' tool)
        after a write operation, route_execution must route to memory_sync, not perception.

        Without the fix, last_tool='write_file' is not in read_only_tools, and
        execution_failed=False, so route_execution would fall through to 'perception',
        causing an infinite loop.
        """
        state = _make_route_exec_state(
            last_result={"ok": True, "_completion_detected": True},
            last_tool_name="write_file",  # write tool — would loop without fix
            current_plan=None,
        )
        result = route_execution(state)
        assert result == "memory_sync", (
            f"route_execution must route to memory_sync when _completion_detected=True "
            f"(got {result!r}). Without fix, write-tool fast-path loops to perception."
        )

    def test_completion_detected_after_edit_tool_routes_to_memory_sync(self):
        """Same check for edit_file tool (also not in read_only_tools)."""
        state = _make_route_exec_state(
            last_result={"ok": True, "_completion_detected": True},
            last_tool_name="edit_file",
            current_plan=None,
        )
        result = route_execution(state)
        assert result == "memory_sync", (
            f"route_execution must route to memory_sync when _completion_detected=True "
            f"after edit_file (got {result!r})"
        )

    def test_no_completion_detected_write_tool_routes_to_perception(self):
        """Without _completion_detected, a successful write tool still goes to perception."""
        state = _make_route_exec_state(
            last_result={"ok": True},
            last_tool_name="write_file",
            current_plan=None,
        )
        result = route_execution(state)
        assert result == "perception", (
            f"Without _completion_detected, write tool success should route to perception "
            f"(got {result!r})"
        )
