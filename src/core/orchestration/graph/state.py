from __future__ import annotations

import logging
from typing import (
    TypedDict,
    List,
    Dict,
    Any,
    Annotated,
    Mapping,
    TYPE_CHECKING,
)
import operator

# Import the authoritative PlanDAG from dag_parser — state.py previously had
# an incompatible duplicate dataclass definition (different fields) which
# caused AttributeErrors at runtime when code mixed both.
from src.core.orchestration.dag_parser import PlanDAG  # noqa: F401

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AgentState — shared state of the LangGraph cognitive pipeline.
# All Optional[X] annotations are written as X | None (Python 3.10+ union
# syntax) for consistency with the rest of the codebase.
# ---------------------------------------------------------------------------


class _AgentStateSpec(TypedDict, total=False):
    """
    Represents the shared state of the LangGraph cognitive pipeline.
    """

    task: str
    history: Annotated[List[Dict[str, Any]], operator.add]
    verified_reads: Annotated[List[str], operator.add]
    next_action: Dict[str, Any] | None
    last_result: Dict[str, Any] | None
    rounds: int
    working_dir: str
    system_prompt: str
    errors: List[str]
    session_id: str | None
    delegation_results: Dict[str, Any] | None
    current_plan: List[Dict[str, Any]] | None
    current_step: int | None
    deterministic: bool | None
    seed: int | None
    analysis_summary: str | None
    relevant_files: List[str] | None
    key_symbols: List[str] | None
    analyst_findings: str | None
    plan_resumed: bool | None
    delegations: List[Dict[str, Any]] | None
    delegation_depth: int | None
    debug_attempts: int | None
    max_debug_attempts: int | None
    total_debug_attempts: int | None
    last_debug_error_type: str | None
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
    step_controller_enabled: bool | None
    task_decomposed: bool | None
    tool_call_count: int | None
    max_tool_calls: int | None
    last_tool_name: str | None
    repo_summary_data: str | None
    replan_required: str | None
    action_failed: bool | None
    plan_progress: Dict[str, Any] | None
    evaluation_result: str | None
    cancel_event: Any | None
    empty_response_count: int | None
    original_task: str | None
    step_description: str | None
    planned_action: Dict[str, Any] | None
    plan_validation: Dict[str, Any] | None
    plan_enforce_warnings: bool | None
    plan_strict_mode: bool | None
    task_history: List[Dict[str, Any]] | None
    step_retry_counts: Dict[str, int] | None
    no_plan_fail_count: int | None
    tool_last_used: Dict[str, int] | None
    files_read: Dict[str, bool] | None
    plan_dag: Dict[str, Any] | None
    execution_waves: List[List[str]] | None
    current_wave: int | None
    pending_preview_id: str | None
    preview_mode_enabled: bool | None
    awaiting_user_input: bool | None
    preview_confirmed: bool | None
    _should_distill: bool | None
    _force_compact: bool | None
    _budget_compaction: bool | None
    _p2p_context: List[Dict[str, Any]] | None
    plan_mode_enabled: bool | None
    awaiting_plan_approval: bool | None
    plan_mode_approved: bool | None
    plan_mode_blocked_tool: str | None
    needs_clarification: bool | None
    _file_lock_manager: Any | None
    _write_queue: List[Dict[str, Any]] | None
    _agent_session_manager: Any | None
    _agent_messages: List[Dict[str, Any]] | None
    _context_controller: Any | None
    last_compact_at: Any | None
    last_compact_turn: int | None
    context_degradation_detected: bool | None
    plan_attempts: int | None
    replan_attempts: int | None
    total_recovery_attempts: int | None
    call_graph: Dict[str, Any] | None
    test_map: Dict[str, Any] | None
    turn_count: int | None
    max_turns: int | None
    recent_tool_calls: List[str] | None
    model_tier: str | None
    session_cost_usd: float | None
    snapshots: List[str] | None
    agent_mode: str | None
    parent_session_id: str | None
    affected_files: List[str] | None
    task_complexity: str | None
    step_lint_warnings: List[str] | None
    evaluation_llm_verdict: str | None
    evaluation_llm_reason: str | None
    _compacted_history: List[Dict[str, Any]] | None
    _compaction_last_round: int | None
    last_plan_hash: str | None
    _pending_injections_source: Any | None


# Expose the TypedDict as AgentState at runtime so tests that inspect
# __annotations__ pass. Use StateLike for flexible call-site typing.
AgentState = _AgentStateSpec
if TYPE_CHECKING:
    StateLike = _AgentStateSpec | Dict[str, Any]
else:
    StateLike = Dict[str, Any]

# ---------------------------------------------------------------------------
# State validation — call at node entry to catch corrupted state early.
# Does NOT raise: logs and returns a list of issues so a bad field doesn't
# crash a live run.
# ---------------------------------------------------------------------------

_INT_OR_NONE_FIELDS: tuple[str, ...] = (
    "rounds",
    "current_step",
    "delegation_depth",
    "debug_attempts",
    "max_debug_attempts",
    "total_debug_attempts",
    "tool_call_count",
    "max_tool_calls",
    "empty_response_count",
    "no_plan_fail_count",
    "current_wave",
    "last_compact_turn",
    "plan_attempts",
    "replan_attempts",
    "turn_count",
    "max_turns",
    "seed",
)


def validate_state(state: Mapping[str, Any]) -> list[str]:
    """Validate AgentState invariants.

    Returns a (possibly empty) list of human-readable issue strings.
    Call at the entry of each graph node — log the issues but do not raise.

    Checks:
    - Numeric fields are ``int | None``, not strings or other types.
    - ``current_step`` is within bounds of ``current_plan`` when both are set.
    - ``turn_count <= max_turns`` when both are set.
    """
    issues: list[str] = []

    # 1. Numeric field type checks
    for field in _INT_OR_NONE_FIELDS:
        val = state.get(field)  # type: ignore[call-overload]
        if val is not None and not isinstance(val, int):
            issues.append(
                f"AgentState.{field} should be int | None but got {type(val).__name__!r}: {val!r}"
            )

    # 2. current_step within bounds of current_plan
    current_step = state.get("current_step")  # type: ignore[call-overload]
    current_plan = state.get("current_plan")  # type: ignore[call-overload]
    if (
        current_step is not None
        and isinstance(current_step, int)
        and current_plan is not None
        and isinstance(current_plan, list)
        and current_step >= len(current_plan)
    ):
        issues.append(
            f"AgentState.current_step={current_step} is out of bounds for "
            f"current_plan of length {len(current_plan)}"
        )

    # 3. turn_count <= max_turns
    turn_count = state.get("turn_count")  # type: ignore[call-overload]
    max_turns = state.get("max_turns")  # type: ignore[call-overload]
    if (
        turn_count is not None
        and isinstance(turn_count, int)
        and max_turns is not None
        and isinstance(max_turns, int)
        and turn_count > max_turns
    ):
        issues.append(
            f"AgentState.turn_count={turn_count} exceeds max_turns={max_turns}"
        )

    if issues:
        _logger.warning("validate_state: %d issue(s) detected: %s", len(issues), issues)

    return issues
