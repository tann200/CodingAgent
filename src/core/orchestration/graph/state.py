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

# Import the authoritative PlanDAG from dag_parser — state.py previously had
# an incompatible duplicate dataclass definition (different fields) which
# caused AttributeErrors at runtime when code mixed both.
from src.core.orchestration.dag_parser import PlanDAG  # noqa: F401

_logger = logging.getLogger(__name__)


class ReplaceList(list):
    """Marker list for LangGraph reducers that should replace existing state."""


def merge_or_replace_list(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """Append by default, but allow explicit replacement for compaction paths."""
    if isinstance(right, ReplaceList):
        return list(right)
    if left is None:
        return list(right or [])
    if right is None:
        return list(left)
    return list(left) + list(right)


def replace_state_list(items: list[Any] | None) -> ReplaceList:
    """Wrap a list so LangGraph reducers replace instead of append."""
    return ReplaceList(items or [])

# ---------------------------------------------------------------------------
# AgentState — shared state of the LangGraph cognitive pipeline.
# All Optional[X] annotations are written as X | None (Python 3.10+ union
# syntax) for consistency with the rest of the codebase.
# ---------------------------------------------------------------------------


class _AgentStateSpec(TypedDict, total=False):
    """Shared state of the LangGraph cognitive pipeline.

    Fields are organized into logical sections with section-comment headers
    (P4-T5). LangGraph requires a flat TypedDict — nested sub-states are NOT
    used as they break the reducer. Section comments are documentation only.
    """

    # ── Core task ──────────────────────────────────────────────────────────
    task: str
    original_task: str | None
    working_dir: str
    system_prompt: str
    session_id: str | None
    parent_session_id: str | None
    turn_count: int | None
    max_turns: int | None
    rounds: int
    agent_mode: str | None
    model_tier: str | None
    deterministic: bool | None
    seed: int | None

    # ── Conversation history ───────────────────────────────────────────────
    history: Annotated[List[Dict[str, Any]], merge_or_replace_list]
    verified_reads: Annotated[List[str], merge_or_replace_list]
    task_history: List[Dict[str, Any]] | None
    recent_tool_calls: List[str] | None
    errors: List[str]

    # ── Plan & step ────────────────────────────────────────────────────────
    current_plan: List[Dict[str, Any]] | None
    current_step: int | None
    step_description: str | None
    planned_action: Dict[str, Any] | None
    plan_progress: Dict[str, Any] | None
    plan_dag: Dict[str, Any] | None
    execution_waves: List[List[str]] | None
    current_wave: int | None
    plan_attempts: int | None
    replan_attempts: int | None
    replan_required: str | None
    plan_resumed: bool | None
    last_plan_hash: str | None
    task_decomposed: bool | None
    task_complexity: str | None
    step_retry_counts: Dict[str, int] | None
    no_plan_fail_count: int | None
    step_lint_warnings: List[str] | None
    affected_files: List[str] | None

    # ── Plan approval / preview ────────────────────────────────────────────
    plan_validation: Dict[str, Any] | None
    plan_enforce_warnings: bool | None
    plan_strict_mode: bool | None
    plan_mode_enabled: bool | None
    awaiting_plan_approval: bool | None
    plan_mode_approved: bool | None
    plan_mode_blocked_tool: str | None
    pending_preview_id: str | None
    preview_mode_enabled: bool | None
    preview_confirmed: bool | None
    awaiting_user_input: bool | None

    # ── Tool execution ─────────────────────────────────────────────────────
    next_action: Dict[str, Any] | None
    last_result: Dict[str, Any] | None
    last_tool_name: str | None
    action_failed: bool | None
    tool_call_count: int | None
    max_tool_calls: int | None
    tool_last_used: Dict[str, int] | None
    files_read: Dict[str, bool] | None
    snapshots: List[str] | None

    # ── Debug & recovery ───────────────────────────────────────────────────
    debug_attempts: int | None
    max_debug_attempts: int | None
    total_debug_attempts: int | None
    last_debug_error_type: str | None
    total_recovery_attempts: int | None
    last_error_code: str | None
    needs_clarification: bool | None

    # ── Verification ───────────────────────────────────────────────────────
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
    evaluation_result: str | None
    evaluation_llm_verdict: str | None
    evaluation_llm_reason: str | None

    # ── Analysis & context ─────────────────────────────────────────────────
    analysis_summary: str | None
    relevant_files: List[str] | None
    key_symbols: List[str] | None
    analyst_findings: str | None
    repo_summary_data: str | None
    call_graph: Dict[str, Any] | None
    test_map: Dict[str, Any] | None
    step_controller_enabled: bool | None
    empty_response_count: int | None

    # ── Delegation ─────────────────────────────────────────────────────────
    delegation_results: Dict[str, Any] | None
    delegations: List[Dict[str, Any]] | None
    delegation_depth: int | None

    # ── Memory / distillation ──────────────────────────────────────────────
    _should_distill: bool | None
    _force_compact: bool | None
    _budget_compaction: bool | None
    _compacted_history: List[Dict[str, Any]] | None
    _compaction_last_round: int | None
    last_compact_at: Any | None
    last_compact_turn: int | None
    context_degradation_detected: bool | None

    # ── Cost & telemetry ───────────────────────────────────────────────────
    session_cost_usd: float | None

    # ── Internal / private ─────────────────────────────────────────────────
    _p2p_context: List[Dict[str, Any]] | None
    _file_lock_manager: Any | None
    _write_queue: List[Dict[str, Any]] | None
    _agent_session_manager: Any | None
    _agent_messages: List[Dict[str, Any]] | None
    _context_controller: Any | None
    _pending_injections_source: Any | None

    # ── Control flow ───────────────────────────────────────────────────────
    cancel_event: Any | None


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
