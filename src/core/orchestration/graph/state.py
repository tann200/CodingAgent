from __future__ import annotations

import logging
from typing import TypedDict, List, Dict, Any, Annotated, Literal, Mapping
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


class AgentState(TypedDict):
    """
    Represents the shared state of the LangGraph cognitive pipeline.

    Fields:
        task: The primary goal or user prompt.
        history: The cumulative message history (appended automatically).
        verified_reads: programmatically tracked files that have been inspected.
        next_action: The tool call extracted from the LLM.
        last_result: The raw output of the last tool execution.
        rounds: Loop counter to prevent runaway execution.
        working_dir: Absolute path to the workspace sandbox.
        system_prompt: Base instructions loaded from agent-brain.
        errors: List of logic or system violations encountered.
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
    # Session tracking for correlation across operations
    session_id: str | None
    # Delegation results from spawned subagents
    delegation_results: Dict[str, Any] | None
    # Planning and deterministic hints
    current_plan: List[Dict[str, Any]] | None
    current_step: int | None
    deterministic: bool | None
    seed: int | None
    # Analysis phase output
    analysis_summary: str | None
    relevant_files: List[str] | None
    key_symbols: List[str] | None
    # Analyst subagent findings injected before planning (#56 early delegation)
    analyst_findings: str | None
    # Set by planning_node when a saved plan is resumed from last_plan.json
    plan_resumed: bool | None
    # Delegations to spawn for background tasks
    delegations: List[Dict[str, Any]] | None
    # HR-5: Depth counter to prevent unbounded recursive delegation (max 3)
    delegation_depth: int | None
    # Debug retry tracking
    debug_attempts: int | None
    max_debug_attempts: int | None  # defaults to 3 at runtime
    # W4: Global debug attempts cap across all error types (prevents 3×N loop on alternating errors)
    total_debug_attempts: int | None
    # Tracks error type from previous debug attempt for W6 fix (reset attempts on error-type change)
    last_debug_error_type: str | None
    # Verification result
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
    # Step controller
    step_controller_enabled: bool | None  # defaults to True at runtime
    # Task decomposition
    task_decomposed: bool | None
    # Tool call budget — enforced in should_after_execution (W12 fix: bails to memory_sync when count >= max)
    tool_call_count: int | None  # defaults to 0 at runtime
    max_tool_calls: int | None  # defaults to 50 at runtime
    # Last tool name executed (W1: used by verification_node to detect side-effecting tools)
    last_tool_name: str | None
    # Repo summary data (automatically generated in analysis phase)
    repo_summary_data: str | None
    # Patch size guard - replan required when patch exceeds 200 lines
    replan_required: str | None
    action_failed: bool | None
    # Plan progress tracking for UI dashboard
    plan_progress: Dict[str, Any] | None
    # Evaluation result for task completion
    evaluation_result: str | None
    # Cancel event for interrupting LLM generation
    cancel_event: Any | None
    # Infinite loop prevention: track consecutive empty/no-tool responses
    empty_response_count: int | None  # defaults to 0 at runtime
    # Original task before step-level decomposition focuses task on sub-step
    original_task: str | None
    # Step controller: current step description and action hint for execution_node
    step_description: str | None
    planned_action: Dict[str, Any] | None
    # Plan validator: result dict written by plan_validator_node, read by builder routing
    plan_validation: Dict[str, Any] | None
    # Plan validator: external overrides for warn/strict mode (passed via initial state)
    plan_enforce_warnings: bool | None
    plan_strict_mode: bool | None
    # Snapshot history for rollback manager (create_state_checkpoint)
    task_history: List[Dict[str, Any]] | None
    # H2: Per-step retry counter keyed by str(step_index) — prevents infinite retry on a broken step
    step_retry_counts: Dict[str, int] | None
    # HR-4: No-plan execution consecutive failure counter — prevents unbounded retry loops
    # when executing without a plan (fast-path). Reset on success, cap at 3.
    no_plan_fail_count: int | None
    # Tool cooldown: keyed by "tool_name:path_arg", value = tool_call_count at last use.
    # Prevents repeated identical read-tool calls (spam) within COOLDOWN_GAP tool executions.
    tool_last_used: Dict[str, int] | None
    # Fast read-before-edit lookup: maps resolved_abs_path → True when file has been read.
    # Complements verified_reads (cumulative list) with O(1) dict access for MODIFYING_TOOLS check.
    files_read: Dict[str, bool] | None
    # Phase A: Dependency DAG (replaces flat current_plan)
    plan_dag: Dict[str, Any] | None
    execution_waves: List[List[str]] | None
    current_wave: int | None  # defaults to 0 at runtime
    # Phase 3: Preview Mode
    pending_preview_id: str | None
    preview_mode_enabled: bool | None  # defaults to False at runtime
    awaiting_user_input: bool | None  # defaults to False at runtime
    preview_confirmed: bool | None
    # Token Auto-Compact triggers
    _should_distill: bool | None
    _force_compact: bool | None
    _budget_compaction: bool | None
    # P2P context buffering
    _p2p_context: List[Dict[str, Any]] | None
    # Plan Mode: plan-first development gate
    plan_mode_enabled: bool | None  # True: write tools blocked until plan approved
    awaiting_plan_approval: (
        bool | None
    )  # True: graph suspended pending user plan approval
    plan_mode_approved: bool | None  # Set by wait_for_user_node after user decision
    plan_mode_blocked_tool: str | None  # Which tool triggered the plan mode gate
    # PRSW: FileLockManager reference for parallel read / sequential write coordination
    _file_lock_manager: Any | None
    # PRSW: Pending write operations queued for sequential execution
    _write_queue: List[Dict[str, Any]] | None
    # Phase B: P2P session tracking references (singletons, not serialised)
    _agent_session_manager: Any | None
    _agent_messages: List[Dict[str, Any]] | None
    _context_controller: Any | None
    # Phase 4: Token auto-compact tracking
    last_compact_at: (
        Any | None
    )  # datetime | None — avoids importing datetime at module level
    last_compact_turn: int | None  # turn counter when last compaction occurred
    context_degradation_detected: (
        bool | None
    )  # True when model quality degradation is detected
    # P1-2: planning→validator→planning inner-loop counter (separate from rounds)
    plan_attempts: int | None  # defaults to 0 at runtime
    # P1-3: evaluation→replan inner-loop counter
    replan_attempts: int | None  # defaults to 0 at runtime
    # P3-1: Structured call graph and test map from analysis phase (JSON dicts, not prose)
    call_graph: Dict[str, Any] | None
    test_map: Dict[str, Any] | None
    # Independent turn counter (separate from rounds/tool_call_count)
    turn_count: int | None  # incremented once per perception pass
    max_turns: int | None  # default 50; configurable via providers.json
    # Doom loop detection: fingerprints of the last N tool calls (name + args hash).
    # Consecutive identical fingerprints trigger doom loop guard (DOOM_LOOP_THRESHOLD=3).
    recent_tool_calls: List[str] | None
    # S1-A: Model capability tier (set in perception_node from active model name).
    # Used by ContextBuilder and execution_node for tool list pruning and format selection.
    model_tier: (
        str | None
    )  # One of the ModelTier enum values ("nano","small","medium","large","frontier")
    # S6-A: Cumulative session cost in USD (accumulated from estimate_cost_usd after each LLM call).
    session_cost_usd: float | None
    # S4-A: Git snapshot tree hashes — one entry appended per perception pass by GitSnapshotManager.
    # Allows session diff (full workspace delta from first to last snapshot) and revert.
    snapshots: List[str] | None
    # ORCH-W4: Current agent operating mode — "execution" (default) or "planning".
    # Set by plan_enter / plan_exit tool calls via orchestrator._agent_mode.
    # perception_node maps "planning" → role "strategic", otherwise "operational".
    agent_mode: str | None
    # SPAWN-W1: Parent session ID for delegated (child) sessions.
    # Set to the parent's session_id when this state is part of a spawned subagent.
    # None for top-level sessions.
    parent_session_id: str | None
    # GAP-S2: Workspace scope guard — files the agent is allowed to write.
    # Populated by planning_node from file-path patterns extracted from plan steps.
    # Empty list means "no plan yet, guard is bypassed".
    # ask_user approval can expand this set at runtime.
    affected_files: List[str] | None
    # WF-1: Complexity flag set by perception_node so route_after_perception reads
    # pre-computed context instead of re-running the keyword heuristic.
    # Values: "simple" | "complex" | None (unknown / not yet set)
    task_complexity: str | None
    # WF-3: Per-step lint warnings from step_controller_node post-step check.
    step_lint_warnings: List[str] | None
    # WF-2: Semantic LLM verdict from evaluation_node.
    # Values: "pass" | "fail" | None (not yet evaluated)
    evaluation_llm_verdict: str | None
    # WF-2: Human-readable reason from the LLM verdict (first 200 chars of response).
    evaluation_llm_reason: str | None
    # WF-4: SHA-256 of the last plan seen by replan_node; used to detect plan divergence.
    last_plan_hash: str | None


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
