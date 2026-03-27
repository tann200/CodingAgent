from typing import TypedDict, List, Dict, Any, Annotated, Optional, Literal
import operator

# Import the authoritative PlanDAG from dag_parser — state.py previously had
# an incompatible duplicate dataclass definition (different fields) which
# caused AttributeErrors at runtime when code mixed both.
from src.core.orchestration.dag_parser import PlanDAG  # noqa: F401


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
    next_action: Optional[Dict[str, Any]]
    last_result: Optional[Dict[str, Any]]
    rounds: int
    working_dir: str
    system_prompt: str
    errors: List[str]
    # Session tracking for correlation across operations
    session_id: Optional[str]
    # Delegation results from spawned subagents
    delegation_results: Optional[Dict[str, Any]]
    # Planning and deterministic hints
    current_plan: Optional[List[Dict[str, Any]]]
    current_step: Optional[int]
    deterministic: Optional[bool]
    seed: Optional[int]
    # Analysis phase output
    analysis_summary: Optional[str]
    relevant_files: Optional[List[str]]
    key_symbols: Optional[List[str]]
    # Analyst subagent findings injected before planning (#56 early delegation)
    analyst_findings: Optional[str]
    # Set by planning_node when a saved plan is resumed from last_plan.json
    plan_resumed: Optional[bool]
    # Delegations to spawn for background tasks
    delegations: Optional[List[Dict[str, Any]]]
    # HR-5: Depth counter to prevent unbounded recursive delegation (max 3)
    delegation_depth: Optional[int]
    # Debug retry tracking
    debug_attempts: Optional[int]
    max_debug_attempts: int
    # W4: Global debug attempts cap across all error types (prevents 3×N loop on alternating errors)
    total_debug_attempts: Optional[int]
    # Tracks error type from previous debug attempt for W6 fix (reset attempts on error-type change)
    last_debug_error_type: Optional[str]
    # Verification result
    verification_passed: Optional[bool]
    verification_result: Optional[Dict[str, Any]]
    # Step controller
    step_controller_enabled: bool
    # Task decomposition
    task_decomposed: Optional[bool]
    # Tool call budget — enforced in should_after_execution (W12 fix: bails to memory_sync when count >= max)
    tool_call_count: int
    max_tool_calls: int
    # Last tool name executed (W1: used by verification_node to detect side-effecting tools)
    last_tool_name: Optional[str]
    # Repo summary data (automatically generated in analysis phase)
    repo_summary_data: Optional[str]
    # Patch size guard - replan required when patch exceeds 200 lines
    replan_required: Optional[str]
    action_failed: Optional[bool]
    # Plan progress tracking for UI dashboard
    plan_progress: Optional[Dict[str, Any]]
    # Evaluation result for task completion
    evaluation_result: Optional[str]
    # Cancel event for interrupting LLM generation
    cancel_event: Optional[Any]
    # Infinite loop prevention: track consecutive empty/no-tool responses
    empty_response_count: int
    # Original task before step-level decomposition focuses task on sub-step
    original_task: Optional[str]
    # Step controller: current step description and action hint for execution_node
    step_description: Optional[str]
    planned_action: Optional[Dict[str, Any]]
    # Plan validator: result dict written by plan_validator_node, read by builder routing
    plan_validation: Optional[Dict[str, Any]]
    # Plan validator: external overrides for warn/strict mode (passed via initial state)
    plan_enforce_warnings: Optional[bool]
    plan_strict_mode: Optional[bool]
    # Snapshot history for rollback manager (create_state_checkpoint)
    task_history: Optional[List[Dict[str, Any]]]
    # H2: Per-step retry counter keyed by str(step_index) — prevents infinite retry on a broken step
    step_retry_counts: Optional[Dict[str, int]]
    # Tool cooldown: keyed by "tool_name:path_arg", value = tool_call_count at last use.
    # Prevents repeated identical read-tool calls (spam) within COOLDOWN_GAP tool executions.
    tool_last_used: Optional[Dict[str, int]]
    # Fast read-before-edit lookup: maps resolved_abs_path → True when file has been read.
    # Complements verified_reads (cumulative list) with O(1) dict access for MODIFYING_TOOLS check.
    files_read: Optional[Dict[str, bool]]
    # Phase A: Dependency DAG (replaces flat current_plan)
    plan_dag: Optional[Dict[str, Any]]
    execution_waves: Optional[List[List[str]]]
    current_wave: int
    # Phase 3: Preview Mode
    pending_preview_id: Optional[str]
    preview_mode_enabled: bool
    awaiting_user_input: bool
    preview_confirmed: Optional[bool]
    # Token Auto-Compact triggers
    _should_distill: Optional[bool]
    _force_compact: Optional[bool]
    _budget_compaction: Optional[bool]
    # P2P context buffering
    _p2p_context: Optional[List[Dict[str, Any]]]
    # Plan Mode: plan-first development gate
    plan_mode_enabled: bool  # True: write tools blocked until plan approved
    awaiting_plan_approval: bool  # True: graph suspended pending user plan approval
    plan_mode_approved: Optional[bool]  # Set by wait_for_user_node after user decision
    plan_mode_blocked_tool: Optional[str]  # Which tool triggered the plan mode gate
    # PRSW: FileLockManager reference for parallel read / sequential write coordination
    _file_lock_manager: Optional[Any]
    # PRSW: Pending write operations queued for sequential execution
    _write_queue: Optional[List[Dict[str, Any]]]
    # Phase B: P2P session tracking references (singletons, not serialised)
    _agent_session_manager: Optional[Any]
    _agent_messages: Optional[List[Dict[str, Any]]]
    _context_controller: Optional[Any]
    # Phase 4: Token auto-compact tracking
    last_compact_at: Optional[
        Any
    ]  # datetime | None — avoids importing datetime at module level
    last_compact_turn: int  # turn counter when last compaction occurred
    context_degradation_detected: (
        bool  # True when model quality degradation is detected
    )
    # P1-2: planning→validator→planning inner-loop counter (separate from rounds)
    plan_attempts: int
    # P1-3: evaluation→replan inner-loop counter
    replan_attempts: int
    # P3-1: Structured call graph and test map from analysis phase (JSON dicts, not prose)
    call_graph: Optional[Dict[str, Any]]
    test_map: Optional[Dict[str, Any]]
