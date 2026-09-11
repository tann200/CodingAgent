"""
tests/unit/test_workflow_state_safety.py — Task #6 workflow/state safety tranche.

Deterministic unit tests covering:
- Forced-retry (rounds/plan_attempts) approval gate: must not bypass pending approval.
- Stale approval flag detection: plan_mode_approved + awaiting_plan_approval = True.
- Cancellation wins in execution, wait-resume, debug/recovery, frontier, step-controller.
- Malformed state (current_plan, current_step, planned_action) boundary validation.
- Safe routing: plan_resumed and large-tier shortcuts preserve mandatory approval.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cancel_event() -> Any:
    """Return a threading.Event that is already set (cancelled)."""
    ev = threading.Event()
    ev.set()
    return ev


def _live_event() -> Any:
    """Return a threading.Event that is NOT set (still running)."""
    return threading.Event()


def _state(**kwargs: Any) -> dict[str, Any]:
    """Build a minimal state dict for routing tests."""
    defaults: dict[str, Any] = {
        "task": "test task",
        "rounds": 0,
        "current_plan": None,
        "current_step": 0,
        "plan_mode_enabled": False,
        "plan_mode_approved": False,
        "awaiting_plan_approval": False,
        "plan_validation": {"valid": True},
        "action_failed": False,
        "plan_attempts": 0,
        "plan_resumed": False,
        "model_tier": "medium",
        "cancel_event": None,
        "last_result": None,
        "next_action": None,
        "tool_call_count": 0,
        "max_tool_calls": 50,
        "errors": [],
        "replan_required": None,
        "debug_attempts": 0,
        "max_debug_attempts": 3,
        "total_recovery_attempts": 0,
        "step_retry_counts": {},
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Imports (routers under test)
# ---------------------------------------------------------------------------

from src.core.orchestration.graph.planning_routing import (
    should_after_plan_validator,
    should_after_step_controller,
)
from src.core.orchestration.graph.execution_routing import (
    route_execution,
    should_after_debug,
    _check_cancelled,
)
from src.core.orchestration.graph.session_routing import route_after_wait_for_user
from src.core.orchestration.graph.tier_graph_routing import (
    route_frontier_loop_exit,
    route_wait_frontier,
    route_debug_frontier,
)
from src.core.orchestration.graph.state import validate_state_boundaries


# ===========================================================================
# 1. Approval gate — forced-retry/rounds shortcuts must NOT bypass approval
# ===========================================================================


class TestApprovalGateNotBypassed:
    """Approval-sensitive routes must never select execution unless actually approved."""

    def test_rounds_forced_execute_blocked_when_approval_pending(self):
        """rounds >= 8 must NOT force execute when plan is unapproved."""
        state = _state(
            rounds=10,
            plan_mode_enabled=True,
            plan_mode_approved=False,
        )
        result = should_after_plan_validator(state)
        assert result == "wait_for_user", (
            f"rounds shortcut must not bypass approval gate; got {result!r}"
        )

    def test_plan_attempts_forced_execute_blocked_when_approval_pending(self):
        """plan_attempts >= 3 must NOT force execute when plan is unapproved."""
        state = _state(
            plan_attempts=5,
            plan_mode_enabled=True,
            plan_mode_approved=False,
        )
        result = should_after_plan_validator(state)
        assert result == "wait_for_user", (
            f"plan_attempts shortcut must not bypass approval gate; got {result!r}"
        )

    def test_plan_resumed_blocked_when_approval_pending(self):
        """plan_resumed=True must NOT bypass approval when plan_mode is unapproved."""
        state = _state(
            plan_resumed=True,
            plan_mode_enabled=True,
            plan_mode_approved=False,
        )
        result = should_after_plan_validator(state)
        assert result == "wait_for_user", (
            f"plan_resumed must not bypass approval gate; got {result!r}"
        )

    def test_large_tier_blocked_when_approval_pending(self):
        """Large/frontier tier shortcut must NOT bypass approval when plan_mode is unapproved."""
        state = _state(
            model_tier="large",
            plan_mode_enabled=True,
            plan_mode_approved=False,
        )
        result = should_after_plan_validator(state)
        assert result == "wait_for_user", (
            f"large-tier shortcut must not bypass approval gate; got {result!r}"
        )

    def test_frontier_tier_blocked_when_approval_pending(self):
        """Frontier tier shortcut must NOT bypass approval when plan_mode is unapproved."""
        state = _state(
            model_tier="frontier",
            plan_mode_enabled=True,
            plan_mode_approved=False,
        )
        result = should_after_plan_validator(state)
        assert result == "wait_for_user", (
            f"frontier-tier shortcut must not bypass approval gate; got {result!r}"
        )

    def test_approved_plan_resumes_normally_with_rounds_shortcut(self):
        """When plan IS approved, rounds >= 8 can still force execute."""
        state = _state(
            rounds=10,
            plan_mode_enabled=True,
            plan_mode_approved=True,
        )
        result = should_after_plan_validator(state)
        assert result == "execute", (
            f"approved plan with high rounds should execute; got {result!r}"
        )

    def test_approved_plan_large_tier_executes(self):
        """When plan IS approved, large tier executes normally."""
        state = _state(
            model_tier="large",
            plan_mode_enabled=True,
            plan_mode_approved=True,
        )
        result = should_after_plan_validator(state)
        assert result == "execute", (
            f"approved plan on large tier should execute; got {result!r}"
        )

    def test_plan_mode_disabled_valid_plan_executes(self):
        """When plan_mode is not enabled, a valid plan executes directly."""
        state = _state(
            plan_mode_enabled=False,
            plan_mode_approved=False,
            plan_validation={"valid": True},
        )
        result = should_after_plan_validator(state)
        assert result == "execute", (
            f"plan_mode disabled + valid plan should execute; got {result!r}"
        )

    def test_plan_resumed_with_no_plan_mode_executes(self):
        """plan_resumed=True without plan_mode enabled executes (approval not required)."""
        state = _state(
            plan_resumed=True,
            plan_mode_enabled=False,
            plan_mode_approved=False,
        )
        result = should_after_plan_validator(state)
        assert result == "execute", (
            f"plan_resumed without plan_mode should execute; got {result!r}"
        )


# ===========================================================================
# 2. Stale approval detection via validate_state_boundaries
# ===========================================================================


class TestStaleApprovalDetection:
    """validate_state_boundaries detects stale/contradictory approval flags."""

    def test_stale_approval_flag_detected(self):
        """plan_mode_approved=True AND awaiting_plan_approval=True is a stale flag."""
        state = _state(
            plan_mode_approved=True,
            awaiting_plan_approval=True,
        )
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "invalid_transition" in kinds, (
            f"Expected invalid_transition for stale approval, got {issues}"
        )
        # Verify the message mentions the stale flag
        stale_issues = [i for i in issues if i["kind"] == "invalid_transition" and "stale" in i["message"]]
        assert stale_issues, f"Expected a 'stale' invalid_transition issue; got {issues}"

    def test_approval_without_plan_mode_detected(self):
        """plan_mode_approved=True but plan_mode_enabled=False is an invalid transition."""
        state = _state(
            plan_mode_enabled=False,
            plan_mode_approved=True,
            awaiting_plan_approval=False,
        )
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "invalid_transition" in kinds, (
            f"Expected invalid_transition for approval-without-mode, got {issues}"
        )

    def test_clean_state_no_issues(self):
        """A clean, consistent state produces no boundary issues."""
        state = _state(
            current_plan=[{"description": "step 1"}, {"description": "step 2"}],
            current_step=0,
            planned_action={"tool": "read_file", "args": {}},
            plan_mode_enabled=True,
            plan_mode_approved=False,
            awaiting_plan_approval=True,
        )
        issues = validate_state_boundaries(state)
        assert issues == [], f"Expected no issues for clean state; got {issues}"

    def test_approved_state_no_stale_flag(self):
        """An approved plan with awaiting_plan_approval cleared is clean."""
        state = _state(
            plan_mode_enabled=True,
            plan_mode_approved=True,
            awaiting_plan_approval=False,
        )
        issues = validate_state_boundaries(state)
        # Should not produce invalid_transition for stale approval
        stale = [i for i in issues if "stale" in i.get("message", "")]
        assert not stale, f"No stale issue expected; got {stale}"


# ===========================================================================
# 3. Malformed state boundary validation
# ===========================================================================


class TestMalformedStateBoundaryValidation:
    """validate_state_boundaries rejects malformed current_plan/current_step/planned_action."""

    def test_current_plan_not_list_detected(self):
        """current_plan must be a list or None; a string is malformed."""
        state = _state(current_plan="not a list")
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "malformed_plan" in kinds, (
            f"Expected malformed_plan for string current_plan; got {issues}"
        )

    def test_current_plan_list_of_non_dicts_detected(self):
        """current_plan entries must be dicts; strings are malformed."""
        state = _state(current_plan=["step one", "step two"])
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "malformed_plan" in kinds, (
            f"Expected malformed_plan for list of strings; got {issues}"
        )

    def test_current_plan_none_is_valid(self):
        """current_plan=None is valid (no plan yet)."""
        state = _state(current_plan=None)
        issues = validate_state_boundaries(state)
        malformed = [i for i in issues if i["kind"] == "malformed_plan"]
        assert not malformed, f"None current_plan should not be malformed; got {malformed}"

    def test_current_plan_empty_list_is_valid(self):
        """current_plan=[] is valid (plan cleared)."""
        state = _state(current_plan=[])
        issues = validate_state_boundaries(state)
        malformed = [i for i in issues if i["kind"] == "malformed_plan"]
        assert not malformed, f"Empty list current_plan should not be malformed; got {malformed}"

    def test_current_step_string_detected(self):
        """current_step must be int or None; a string is malformed."""
        state = _state(current_step="2")
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "malformed_step" in kinds, (
            f"Expected malformed_step for string current_step; got {issues}"
        )

    def test_current_step_out_of_bounds_detected(self):
        """current_step >= len(current_plan) is out of bounds."""
        state = _state(
            current_plan=[{"description": "step 1"}, {"description": "step 2"}],
            current_step=5,
        )
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "out_of_bounds" in kinds, (
            f"Expected out_of_bounds for current_step=5 with plan length 2; got {issues}"
        )

    def test_current_step_valid_within_bounds(self):
        """current_step within plan bounds produces no out_of_bounds issue."""
        state = _state(
            current_plan=[{"description": "step 1"}, {"description": "step 2"}],
            current_step=1,
        )
        issues = validate_state_boundaries(state)
        oob = [i for i in issues if i["kind"] == "out_of_bounds"]
        assert not oob, f"In-bounds step should not produce out_of_bounds; got {oob}"

    def test_planned_action_not_dict_detected(self):
        """planned_action must be a dict or None; a string is malformed."""
        state = _state(planned_action="read_file path.py")
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "malformed_action" in kinds, (
            f"Expected malformed_action for string planned_action; got {issues}"
        )

    def test_planned_action_none_is_valid(self):
        """planned_action=None is valid."""
        state = _state(planned_action=None)
        issues = validate_state_boundaries(state)
        malformed = [i for i in issues if i["kind"] == "malformed_action"]
        assert not malformed, f"None planned_action should not be malformed; got {malformed}"

    def test_turns_exceeded_detected(self):
        """turn_count > max_turns is detected as turns_exceeded."""
        state = _state(turn_count=15, max_turns=10)
        issues = validate_state_boundaries(state)
        kinds = [i["kind"] for i in issues]
        assert "turns_exceeded" in kinds, (
            f"Expected turns_exceeded when turn_count > max_turns; got {issues}"
        )

    def test_issue_dict_has_required_keys(self):
        """Every issue dict must have kind, field, message, and value keys."""
        state = _state(current_plan="bad")
        issues = validate_state_boundaries(state)
        assert issues, "Expected at least one issue for malformed current_plan"
        for issue in issues:
            assert "kind" in issue, f"Missing 'kind' in {issue}"
            assert "field" in issue, f"Missing 'field' in {issue}"
            assert "message" in issue, f"Missing 'message' in {issue}"
            assert "value" in issue, f"Missing 'value' in {issue}"

    def test_multiple_issues_reported(self):
        """Multiple malformed fields produce multiple structured issues."""
        state = _state(
            current_plan="not a list",
            current_step="bad",
            planned_action=42,
        )
        issues = validate_state_boundaries(state)
        kinds = {i["kind"] for i in issues}
        assert "malformed_plan" in kinds
        assert "malformed_step" in kinds
        assert "malformed_action" in kinds


# ===========================================================================
# 4. Cancellation wins everywhere
# ===========================================================================


class TestCancellationWins:
    """Cancelled state must not re-enter execution in any router."""

    # ── route_execution ──────────────────────────────────────────────────

    def test_route_execution_cancelled_to_memory_sync(self):
        """Cancelled run routes to memory_sync from route_execution."""
        state = _state(
            cancel_event=_cancel_event(),
            current_plan=[{"description": "step"}],
            current_step=0,
            last_result={"ok": True},
        )
        result = route_execution(state)
        assert result == "memory_sync", (
            f"Cancelled run must route to memory_sync via route_execution; got {result!r}"
        )

    def test_route_execution_not_cancelled_proceeds_normally(self):
        """Non-cancelled run with a valid plan step reaches step_controller."""
        state = _state(
            cancel_event=_live_event(),
            current_plan=[{"description": "step 1"}, {"description": "step 2"}],
            current_step=0,
            last_result={"ok": True},
        )
        result = route_execution(state)
        assert result == "step_controller", (
            f"Non-cancelled run should reach step_controller; got {result!r}"
        )

    def test_route_execution_no_cancel_event_proceeds(self):
        """No cancel_event set proceeds normally."""
        state = _state(
            cancel_event=None,
            current_plan=[{"description": "step"}],
            current_step=0,
            last_result={"ok": True},
        )
        result = route_execution(state)
        # Not cancelled — should not be memory_sync due to cancellation
        assert result != "memory_sync" or state.get("tool_call_count", 0) >= state.get("max_tool_calls", 50), (
            "No cancel_event=None should not trigger cancellation routing"
        )

    # ── should_after_debug ───────────────────────────────────────────────

    def test_debug_cancelled_to_memory_sync(self):
        """Cancelled run routes to memory_sync from should_after_debug."""
        state = _state(
            cancel_event=_cancel_event(),
            next_action={"name": "write_file", "args": {}},
            debug_attempts=0,
            max_debug_attempts=3,
        )
        result = should_after_debug(state)
        assert result == "memory_sync", (
            f"Cancelled run must route to memory_sync via should_after_debug; got {result!r}"
        )

    def test_debug_not_cancelled_with_fix_to_execution(self):
        """Non-cancelled run with a fix routes to execution from should_after_debug."""
        state = _state(
            cancel_event=_live_event(),
            next_action={"name": "write_file", "args": {}},
            debug_attempts=1,
            max_debug_attempts=3,
        )
        result = should_after_debug(state)
        assert result == "execution", (
            f"Non-cancelled run with fix should route to execution; got {result!r}"
        )

    # ── route_after_wait_for_user ────────────────────────────────────────

    def test_wait_for_user_cancelled_to_perception(self):
        """Cancelled run routes to perception (not execute) from route_after_wait_for_user."""
        state = _state(
            cancel_event=_cancel_event(),
            plan_mode_approved=True,  # Would normally go to execute
            preview_confirmed=True,
        )
        result = route_after_wait_for_user(state)
        assert result == "perception", (
            f"Cancelled run must not resume execution; got {result!r}"
        )

    def test_wait_for_user_not_cancelled_approved_to_execute(self):
        """Non-cancelled + approved plan (plan mode active) routes to execute."""
        state = _state(
            cancel_event=_live_event(),
            plan_mode_enabled=True,
            plan_mode_approved=True,
        )
        result = route_after_wait_for_user(state)
        assert result == "execute", (
            f"Non-cancelled approved plan should execute; got {result!r}"
        )

    # ── should_after_step_controller ────────────────────────────────────

    def test_step_controller_cancelled_to_end(self):
        """Cancelled run routes to end from should_after_step_controller."""
        state = _state(
            cancel_event=_cancel_event(),
            current_plan=[{"description": "step"}],
            current_step=0,
            last_result={"ok": True},
        )
        result = should_after_step_controller(state)
        assert result == "end", (
            f"Cancelled run must route to end via step_controller; got {result!r}"
        )

    def test_step_controller_not_cancelled_proceeds(self):
        """Non-cancelled run with pending step routes to execution."""
        state = _state(
            cancel_event=_live_event(),
            current_plan=[{"description": "step"}],
            current_step=0,
            last_result=None,
        )
        result = should_after_step_controller(state)
        assert result == "execution", (
            f"Non-cancelled step should route to execution; got {result!r}"
        )

    # ── route_frontier_loop_exit ─────────────────────────────────────────

    def test_frontier_loop_exit_cancelled_to_memory_sync(self):
        """Cancelled run routes to memory_sync from route_frontier_loop_exit."""
        state = _state(
            cancel_event=_cancel_event(),
            last_result={"ok": True},
        )
        result = route_frontier_loop_exit(state)
        assert result == "memory_sync", (
            f"Cancelled run must route to memory_sync via frontier_loop_exit; got {result!r}"
        )

    def test_frontier_loop_exit_not_cancelled_with_result_to_verification(self):
        """Non-cancelled run with result routes to verification."""
        state = _state(
            cancel_event=_live_event(),
            last_result={"ok": True},
        )
        result = route_frontier_loop_exit(state)
        assert result == "verification", (
            f"Non-cancelled run with result should go to verification; got {result!r}"
        )

    # ── route_wait_frontier ──────────────────────────────────────────────

    def test_wait_frontier_cancelled_to_memory_sync(self):
        """Cancelled run routes to memory_sync from route_wait_frontier."""
        state = _state(
            cancel_event=_cancel_event(),
            plan_mode_approved=True,  # Would normally go to frontier_loop
        )
        result = route_wait_frontier(state)
        assert result == "memory_sync", (
            f"Cancelled run must route to memory_sync via route_wait_frontier; got {result!r}"
        )

    def test_wait_frontier_not_cancelled_approved_to_frontier_loop(self):
        """Non-cancelled + approved routes to frontier_loop."""
        state = _state(
            cancel_event=_live_event(),
            plan_mode_approved=True,
        )
        result = route_wait_frontier(state)
        assert result == "frontier_loop", (
            f"Non-cancelled approved should go to frontier_loop; got {result!r}"
        )

    # ── route_debug_frontier ─────────────────────────────────────────────

    def test_debug_frontier_cancelled_to_memory_sync(self):
        """Cancelled run routes to memory_sync from route_debug_frontier."""
        state = _state(cancel_event=_cancel_event())
        result = route_debug_frontier(
            state,
            should_after_debug_fn=lambda s: "execution",
        )
        assert result == "memory_sync", (
            f"Cancelled run must route to memory_sync via route_debug_frontier; got {result!r}"
        )

    def test_debug_frontier_not_cancelled_maps_execution_to_frontier_loop(self):
        """Non-cancelled run maps 'execution' to 'frontier_loop'."""
        state = _state(cancel_event=_live_event())
        result = route_debug_frontier(
            state,
            should_after_debug_fn=lambda s: "execution",
        )
        assert result == "frontier_loop", (
            f"Non-cancelled execution result should map to frontier_loop; got {result!r}"
        )


# ===========================================================================
# 5. _check_cancelled helper
# ===========================================================================


class TestCheckCancelledHelper:
    def test_returns_true_when_event_set(self):
        assert _check_cancelled({"cancel_event": _cancel_event()}) is True

    def test_returns_false_when_event_not_set(self):
        assert _check_cancelled({"cancel_event": _live_event()}) is False

    def test_returns_false_when_no_event(self):
        assert _check_cancelled({"cancel_event": None}) is False
        assert _check_cancelled({}) is False

    def test_returns_false_for_non_event_object(self):
        """Objects without is_set() are not treated as cancellation events."""
        assert _check_cancelled({"cancel_event": "cancelled"}) is False


# ===========================================================================
# 6. Lite/frontier plan approval preserved
# ===========================================================================


class TestFrontierLitePlanApprovalPreserved:
    """Lite/frontier paths must preserve mandatory plan approval."""

    def test_route_wait_frontier_unapproved_stays_in_memory_sync(self):
        """plan_mode_approved=False routes to memory_sync (not frontier_loop)."""
        state = _state(plan_mode_approved=False, cancel_event=None)
        result = route_wait_frontier(state)
        assert result == "memory_sync", (
            f"Unapproved plan must not re-enter frontier_loop; got {result!r}"
        )

    def test_route_wait_frontier_none_approved_stays_in_memory_sync(self):
        """plan_mode_approved=None (missing) routes to memory_sync (not frontier_loop)."""
        state = _state(cancel_event=None)
        del state["plan_mode_approved"]
        result = route_wait_frontier(state)
        assert result == "memory_sync", (
            f"Missing approval must not re-enter frontier_loop; got {result!r}"
        )

    def test_route_frontier_loop_exit_awaiting_approval_to_wait(self):
        """awaiting_plan_approval=True routes to wait_for_user (approval preserved)."""
        state = _state(
            awaiting_plan_approval=True,
            last_result={"ok": True},
            cancel_event=None,
        )
        result = route_frontier_loop_exit(state)
        assert result == "wait_for_user", (
            f"Pending approval must route to wait_for_user; got {result!r}"
        )

    def test_route_frontier_loop_exit_no_approval_with_result_to_verification(self):
        """No pending approval with result routes to verification."""
        state = _state(
            awaiting_plan_approval=False,
            last_result={"ok": True},
            cancel_event=None,
        )
        result = route_frontier_loop_exit(state)
        assert result == "verification", (
            f"No pending approval with result should go to verification; got {result!r}"
        )


# ===========================================================================
# 7. Validator semantics corrections (Task #6 correction)
# ===========================================================================

from src.core.orchestration.graph.state import (
    enforce_state_boundaries,
    BoundaryValidationError,
)


class TestValidatorSemantics:
    """Corrected boundary validator semantics."""

    def test_bool_current_step_rejected(self):
        """bool must NOT count as int for current_step."""
        issues = validate_state_boundaries({"current_step": True})
        kinds = [i["kind"] for i in issues]
        assert "malformed_step" in kinds, f"bool current_step must be malformed; got {issues}"

    def test_bool_current_step_false_rejected(self):
        """current_step=False (a bool) is rejected too."""
        issues = validate_state_boundaries({"current_step": False})
        kinds = [i["kind"] for i in issues]
        assert "malformed_step" in kinds

    def test_negative_current_step_rejected(self):
        """Negative current_step is invalid."""
        issues = validate_state_boundaries({"current_step": -1})
        kinds = [i["kind"] for i in issues]
        assert "malformed_step" in kinds, f"negative current_step must be malformed; got {issues}"

    def test_step_equals_plan_len_allowed_generally(self):
        """current_step == len(plan) is allowed as an exhausted post-execution state."""
        state = {
            "current_step": 2,
            "current_plan": [{"description": "s1"}, {"description": "s2"}],
        }
        issues = validate_state_boundaries(state)
        oob = [i for i in issues if i["kind"] == "out_of_bounds"]
        assert not oob, f"exhausted state should be allowed generally; got {oob}"

    def test_step_equals_plan_len_rejected_at_execution(self):
        """current_step == len(plan) is rejected at the execution boundary."""
        state = {
            "current_step": 2,
            "current_plan": [{"description": "s1"}, {"description": "s2"}],
        }
        issues = validate_state_boundaries(state, boundary="execution")
        kinds = [i["kind"] for i in issues]
        assert "out_of_bounds" in kinds, (
            f"exhausted state at execution entry must be rejected; got {issues}"
        )

    def test_step_beyond_plan_len_always_rejected(self):
        """current_step > len(plan) is always out of bounds."""
        state = {
            "current_step": 5,
            "current_plan": [{"description": "s1"}, {"description": "s2"}],
        }
        assert any(i["kind"] == "out_of_bounds" for i in validate_state_boundaries(state))
        assert any(
            i["kind"] == "out_of_bounds"
            for i in validate_state_boundaries(state, boundary="execution")
        )

    def test_malformed_next_action_detected(self):
        """next_action must be a dict or None."""
        issues = validate_state_boundaries({"next_action": "read_file"})
        kinds = [i["kind"] for i in issues]
        assert "malformed_action" in kinds, (
            f"malformed next_action must be detected; got {issues}"
        )

    def test_malformed_planned_action_detected(self):
        """planned_action must be a dict or None."""
        issues = validate_state_boundaries({"planned_action": 42})
        kinds = [i["kind"] for i in issues]
        assert "malformed_action" in kinds

    def test_next_action_dict_ok(self):
        """A dict next_action produces no malformed_action issue."""
        issues = validate_state_boundaries({"next_action": {"name": "read_file"}})
        assert not [i for i in issues if i["kind"] == "malformed_action"]

    def test_turn_count_bool_not_counted(self):
        """bool turn_count/max_turns must not trigger turns_exceeded comparison."""
        # True > 0 would be a false positive if bool were treated as int
        issues = validate_state_boundaries({"turn_count": True, "max_turns": 0})
        assert not [i for i in issues if i["kind"] == "turns_exceeded"]


class TestEnforceStateBoundaries:
    """enforce_state_boundaries raises structured exceptions."""

    def test_raises_on_malformed_state(self):
        with pytest.raises(BoundaryValidationError) as exc:
            enforce_state_boundaries({"current_step": "bad"}, "planning")
        assert exc.value.boundary == "planning"
        assert exc.value.issues
        assert exc.value.issues[0]["kind"] == "malformed_step"

    def test_no_raise_on_clean_state(self):
        # Should not raise
        enforce_state_boundaries(
            {"current_step": 0, "current_plan": [{"description": "s1"}]},
            "execution",
        )

    def test_unknown_boundary_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown boundary"):
            enforce_state_boundaries({}, "not_a_boundary")

    def test_execution_boundary_rejects_exhausted_plan(self):
        with pytest.raises(BoundaryValidationError) as exc:
            enforce_state_boundaries(
                {"current_step": 1, "current_plan": [{"description": "only"}]},
                "execution",
            )
        assert any(i["kind"] == "out_of_bounds" for i in exc.value.issues)


# ===========================================================================
# 8. Boundary wrappers prevent underlying node from running on invalid state
# ===========================================================================


class TestBoundaryWrapperPreventsNodeExecution:
    """Compiled boundary wrappers must block the node spy on invalid state."""

    @pytest.mark.asyncio
    async def test_wrapper_blocks_node_on_malformed_state(self):
        from src.core.orchestration.graph.builder import _with_boundary

        calls: list[Any] = []

        async def _spy_node(state, config):
            calls.append(state)
            return {"ran": True}

        wrapped = _with_boundary(_spy_node, "execution")

        bad_state = {"current_step": "not-an-int"}
        with pytest.raises(BoundaryValidationError):
            await wrapped(bad_state, None)

        assert calls == [], "Underlying node must NOT run on malformed state"

    @pytest.mark.asyncio
    async def test_wrapper_runs_node_on_valid_state(self):
        from src.core.orchestration.graph.builder import _with_boundary

        calls: list[Any] = []

        async def _spy_node(state, config):
            calls.append(state)
            return {"ran": True}

        wrapped = _with_boundary(_spy_node, "execution")

        good_state = {
            "current_step": 0,
            "current_plan": [{"description": "step 1"}],
        }
        result = await wrapped(good_state, None)

        assert result == {"ran": True}
        assert len(calls) == 1, "Underlying node must run once on valid state"

    @pytest.mark.asyncio
    async def test_execution_wrapper_blocks_exhausted_plan(self):
        """Execution boundary wrapper rejects an exhausted plan before running node."""
        from src.core.orchestration.graph.builder import _with_boundary

        calls: list[Any] = []

        async def _spy_node(state, config):
            calls.append(state)
            return {"ran": True}

        wrapped = _with_boundary(_spy_node, "execution")

        exhausted_state = {
            "current_step": 1,
            "current_plan": [{"description": "only step"}],
        }
        with pytest.raises(BoundaryValidationError):
            await wrapped(exhausted_state, None)
        assert calls == [], "Node must not run when plan is exhausted at execution entry"

    @pytest.mark.asyncio
    async def test_planning_wrapper_allows_exhausted_plan(self):
        """Planning boundary allows current_step == len(plan) (not execution)."""
        from src.core.orchestration.graph.builder import _with_boundary

        calls: list[Any] = []

        async def _spy_node(state, config):
            calls.append(state)
            return {"ran": True}

        wrapped = _with_boundary(_spy_node, "planning")

        state = {
            "current_step": 1,
            "current_plan": [{"description": "only step"}],
        }
        result = await wrapped(state, None)
        assert result == {"ran": True}
        assert len(calls) == 1


# ===========================================================================
# 9. Lite graph topology: wait_for_user + approval routing
# ===========================================================================

from src.core.orchestration.graph.tier_graph_routing import (
    route_frontier_loop_exit_lite,
    route_wait_lite,
)


class TestLiteGraphApprovalTopology:
    """Lite graph must include wait_for_user and preserve plan approval."""

    def test_lite_graph_has_wait_for_user_node(self):
        from src.core.orchestration.graph.builder import _compile_lite_graph

        graph = _compile_lite_graph()
        nodes = set(graph.get_graph().nodes.keys())
        assert "wait_for_user" in nodes, (
            f"lite graph must include wait_for_user node; nodes={nodes}"
        )
        assert "frontier_loop" in nodes
        assert "memory_sync" in nodes

    def test_lite_frontier_exit_suspends_for_approval(self):
        """awaiting_plan_approval routes to wait_for_user, not memory_sync."""
        state = _state(awaiting_plan_approval=True, cancel_event=None)
        assert route_frontier_loop_exit_lite(state) == "wait_for_user"

    def test_lite_frontier_exit_no_approval_to_memory_sync(self):
        """No pending approval routes to memory_sync."""
        state = _state(awaiting_plan_approval=False, cancel_event=None)
        assert route_frontier_loop_exit_lite(state) == "memory_sync"

    def test_lite_frontier_exit_cancel_wins_over_approval(self):
        """Cancellation routes to memory_sync even with pending approval."""
        state = _state(awaiting_plan_approval=True, cancel_event=_cancel_event())
        assert route_frontier_loop_exit_lite(state) == "memory_sync"

    def test_lite_wait_resumes_on_explicit_approval(self):
        """Explicit plan_mode_approved=True resumes frontier_loop."""
        state = _state(plan_mode_approved=True, cancel_event=None)
        assert route_wait_lite(state) == "frontier_loop"

    def test_lite_wait_rejection_to_memory_sync(self):
        """Rejection (plan_mode_approved=False) routes to memory_sync."""
        state = _state(plan_mode_approved=False, cancel_event=None)
        assert route_wait_lite(state) == "memory_sync"

    def test_lite_wait_missing_approval_to_memory_sync(self):
        """Missing approval routes to memory_sync (does not resume)."""
        state = _state(cancel_event=None)
        del state["plan_mode_approved"]
        assert route_wait_lite(state) == "memory_sync"

    def test_lite_wait_cancel_wins(self):
        """Cancellation routes to memory_sync even if approved."""
        state = _state(plan_mode_approved=True, cancel_event=_cancel_event())
        assert route_wait_lite(state) == "memory_sync"


# ===========================================================================
# 10. route_after_wait_for_user stale approval handling
# ===========================================================================


class TestWaitForUserStaleApproval:
    """route_after_wait_for_user must not treat stale approval as executable."""

    def test_stale_approval_without_plan_mode_not_execute(self):
        """plan_mode_approved=True but plan_mode_enabled falsy → not execute."""
        state = _state(
            plan_mode_enabled=False,
            plan_mode_approved=True,
            preview_confirmed=False,
            cancel_event=None,
        )
        result = route_after_wait_for_user(state)
        assert result != "execute", (
            f"stale approval must not execute; got {result!r}"
        )
        assert result == "perception"

    def test_stale_approval_falls_through_to_preview_confirm(self):
        """Stale approval + preview_confirmed=True → preview path executes."""
        state = _state(
            plan_mode_enabled=False,
            plan_mode_approved=True,
            preview_confirmed=True,
            cancel_event=None,
        )
        result = route_after_wait_for_user(state)
        assert result == "execute", (
            f"preview confirmation must still execute; got {result!r}"
        )

    def test_active_plan_mode_approved_executes(self):
        """plan_mode_enabled=True + approved → execute."""
        state = _state(
            plan_mode_enabled=True,
            plan_mode_approved=True,
            cancel_event=None,
        )
        assert route_after_wait_for_user(state) == "execute"

    def test_active_plan_mode_rejected_replans(self):
        """plan_mode_enabled=True + rejected → planning."""
        state = _state(
            plan_mode_enabled=True,
            plan_mode_approved=False,
            cancel_event=None,
        )
        assert route_after_wait_for_user(state) == "planning"

    def test_preview_confirmed_without_plan_mode(self):
        """Pure preview confirmation (no plan mode) executes."""
        state = _state(
            plan_mode_enabled=False,
            preview_confirmed=True,
            cancel_event=None,
        )
        del state["plan_mode_approved"]
        assert route_after_wait_for_user(state) == "execute"

    def test_preview_rejected_to_perception(self):
        """No approval, no confirmation → perception."""
        state = _state(
            plan_mode_enabled=False,
            preview_confirmed=False,
            cancel_event=None,
        )
        del state["plan_mode_approved"]
        assert route_after_wait_for_user(state) == "perception"
