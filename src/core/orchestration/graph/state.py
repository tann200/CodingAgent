from __future__ import annotations

"""AgentState — shared state of the LangGraph cognitive pipeline.

Overview
--------
``AgentState`` (alias ``_AgentStateSpec``) is a flat ``TypedDict`` threaded
through every node in the LangGraph graph. All fields are ``total=False``
(optional at the TypedDict level); runtime defaults are supplied by node
initialisation code.

Two fields use LangGraph *annotated reducers* so parallel branches can safely
append without clobbering each other:

- ``history``       — ``merge_or_replace_list``  (append; use ``ReplaceList``
                      to replace wholesale, e.g. after compaction)
- ``verified_reads`` — same reducer

All other fields are last-write-wins: the node that finishes last in a parallel
fan-out wins.

Node Execution Order (Canonical 6-Node Deterministic-Boundary Graph)
---------------------------------------------------------------------
Defined in ``src/core/orchestration/graph/builder.py:compile_agent_graph()``.

::

    ENTRY
      │
      ▼
    initialize  ──►  agent_loop  ──►  [ conditional router: route_agent_loop_exit ]
                         ▲             ├──► approval  ──► agent_loop
                         │             ├──► compact   ──► agent_loop
                         │             ├──► verify    ──► [ conditional router: route_verify_exit ]
                         │             │                    ├──► agent_loop (on verify failure)
                         │             │                    └──► finalize   (on verify pass)
                         │             └──► finalize  ──► END
                         └────────────────────────────────┘

1. ``initialize``:
   Validates working directory, initializes or normalizes model tier & runtime capabilities,
   ensures valid ``runtime`` and ``run`` structures, and sets initial session state.

2. ``agent_loop``:
   Single iterative cognitive node that performs perception, context assembly, tool
   planning, and multi-turn dispatch. Evaluates completion contracts and routes to
   downstream deterministic boundary nodes.

3. ``approval``:
   Deterministic HITL approval checkpoint for protected files, plan approval, and dangerous operations.

4. ``verify``:
   Deterministic verification gate: runs test suite, syntax checks, and evidence collection.
   Guards completion; on pass routes to ``finalize``, on failure routes back to ``agent_loop`` with diagnostic context.

5. ``compact``:
   Deterministic memory & context compaction node. Truncates older tool outputs, rolls up history
   via ``merge_or_replace_list``, and preserves critical decision provenance.

6. ``finalize``:
   Deterministic transaction settlement and clean up node. Commits or rolls back deferred
   transactions, generates work summary, records telemetry/costs, and marks run status as completed.

Target State Contracts
----------------------
- ``runtime``: Normalized tier policy, capabilities, and provider metadata.
- ``run``: Current run status (running/completed/failed) and transition outcome.
- ``pending_action``: Pending tool or plan awaiting execution or approval.
- ``last_action``: Most recent executed tool action, result digest, and exit status.
- ``verification``: Verification results, pass/fail status, and diagnostic evidence.

Reducer Semantics
-----------------
``history`` and ``verified_reads`` both use ``merge_or_replace_list``:

- Normal append: return a plain ``list`` — LangGraph merges it with the
  existing state by concatenation.
- Full replacement (e.g. after compaction): return ``replace_state_list(items)``
  which wraps the list in ``ReplaceList``; the reducer then returns only those
  items discarding previous history.

See Also
--------
- ``src/core/orchestration/graph/builder.py`` — graph compilation & routing
- ``src/core/orchestration/graph/nodes/`` — individual node implementations
- ``src/core/orchestration/graph/state_schemas.py`` — node result schema enforcement
- ``src/core/orchestration/graph/state.py:validate_state()`` — runtime checks
"""

import logging  # noqa: E402
from typing import (  # noqa: E402
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
from src.core.orchestration.dag_parser import PlanDAG  # noqa: F401, E402
from src.core.orchestration.graph.policy import (  # noqa: F401, E402
    LastAction,
    PendingAction,
    Policy,
    RunOutcome,
    RunState,
    RunStatus,
    RuntimeState,
    VerificationMode,
    VerificationState,
    compute_action_digest,
    create_last_action,
    create_run_state,
    create_runtime_state,
    get_capabilities_for_tier,
    get_policy_for_tier,
    normalize_target_tier,
)

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


# P2-4: plan_history needs a capped-append reducer so LangGraph appends
# entries rather than replacing the whole list on each state update.
# Cap at 10 to prevent unbounded growth across many replan cycles.
_PLAN_HISTORY_CAP = 10


def _append_plan_history(
    left: List[Dict[str, Any]] | None,
    right: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """Append new plan-history entries from *right* onto *left*, capped at
    _PLAN_HISTORY_CAP.  When *right* is a ReplaceList it replaces *left*.
    """
    if isinstance(right, ReplaceList):
        result = list(right)
    else:
        result = list(left or []) + list(right or [])
    # Keep only the most recent entries.
    if len(result) > _PLAN_HISTORY_CAP:
        result = result[-_PLAN_HISTORY_CAP:]
    return result

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
    # LIFECYCLE: Set by orchestrator_bootstrap / builder; read by every node.
    #   task, original_task, working_dir — immutable after bootstrap
    #   session_id, parent_session_id — set once by bootstrap
    #   turn_count, max_turns — incremented by perception_node, read by loop_guards
    #   rounds — incremented by frontier_loop_node
    #   agent_mode, model_tier, deterministic, seed — set by workflow_selector
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
    active_role: str | None
    model_tier: str | None
    deterministic: bool | None
    seed: int | None
    tool_limit: int | None
    context_limit: int | None
    use_verification: bool | None
    use_replan: bool | None
    use_vector_memory: bool | None
    workflow_type: str | None
    thinking_mode: str | None
    workflow_config: Any | None

    # ── Conversation history ───────────────────────────────────────────────
    # LIFECYCLE: Written by perception_node, execution_node; read by all nodes.
    #   history — primary message list; appended by perception & execution
    #   verified_reads — written by verification_node
    #   task_history — snapshot by memory_sync_node
    #   recent_tool_calls — updated by execution_node, read by loop_guards
    #   errors — appended by any node on failure; cleared on new turn
    history: Annotated[List[Dict[str, Any]], merge_or_replace_list]
    verified_reads: Annotated[List[str], merge_or_replace_list]
    task_history: List[Dict[str, Any]] | None
    recent_tool_calls: List[str] | None
    errors: List[str]

    # ── Plan & step ────────────────────────────────────────────────────────
    # LIFECYCLE: Written by planning_node, plan_validator; read by execution_node.
    #   current_plan, plan_dag — set by planning_node
    #   current_step — incremented by execution_node step completion
    #   plan_completed — set by frontier_loop_node when the loop covered the
    #     whole plan and the model declared done; read by evaluation_node and
    #     persisted so a finished plan is not regenerated next turn (P3.3)
    #   planned_action — set during plan breakdown
    #   plan_progress — updated by execution_node
    #   execution_waves, current_wave — set by planner, read by execution
    #   plan_attempts, replan_attempts — incremented on replan
    #   replan_required — set by execution_node when stuck
    #   plan_resumed, last_plan_hash — set on session resume
    #   task_decomposed, task_complexity — set by planning_node
    #   step_retry_counts — dict keyed by step index; updated by execution
    #   no_plan_fail_count — incremented on consecutive no-plan failures
    #   step_lint_warnings — set by linter after tool execution
    #   affected_files — aggregated by execution_node
    current_plan: List[Dict[str, Any]] | None
    current_step: int | None
    plan_completed: bool | None
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
    # P3-4 / P2-4: Plan versioning — list of previous plan snapshots with timestamps.
    # Uses _append_plan_history reducer so LangGraph appends rather than replaces.
    # Each entry: {"timestamp": ISO8601, "plan": [...], "reason": str}
    plan_history: Annotated[List[Dict[str, Any]], _append_plan_history] | None
    step_lint_warnings: List[str] | None
    affected_files: List[str] | None

    # ── Plan approval / preview ────────────────────────────────────────────
    # LIFECYCLE: Written by plan_mode, preview_service; read by execution_node.
    #   plan_validation — set by plan_validator
    #   plan_enforce_warnings, plan_strict_mode — config flags, immutable
    #   plan_mode_enabled, awaiting_plan_approval — managed by plan_mode
    #   plan_mode_approved — set true on user approval
    #   plan_mode_blocked_tool — set when tool blocked in plan mode
    #   pending_preview_id — set by preview_service on /preview
    #   preview_mode_enabled, preview_confirmed — toggle flags
    #   awaiting_user_input — set by any node needing user input
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
    # LIFECYCLE: Written by execution_node / frontier_loop_node; read by verification_node, loop_guards.
    #   next_action — the pending tool call (set then consumed each iteration)
    #   last_result — outcome of last tool call
    #   last_tool_name — name of last executed tool
    #   action_failed — bool flag for recovery branching
    #   tool_call_count, max_tool_calls — counter / cap for tool loop
    #   tool_last_used — dict[user_tool_name -> turn_number] for throttling
    #   files_read — tracking for read-before-write guard
    #   snapshots — fork snapshot IDs created during session
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
    # LIFECYCLE: Written by recovery_node / loop_guards; read by perception_node, execution_node.
    #   debug_attempts, max_debug_attempts — incrementing loop counter / cap
    #   total_debug_attempts — lifetime counter (never reset)
    #   last_debug_error_type, total_recovery_attempts — diagnostic fields
    #   last_error_code — set on tool failure; cleared on success
    #   needs_clarification — set when user clarification required
    debug_attempts: int | None
    max_debug_attempts: int | None
    total_debug_attempts: int | None
    last_debug_error_type: str | None
    total_recovery_attempts: int | None
    last_error_code: str | None
    needs_clarification: bool | None

    # ── Verification ───────────────────────────────────────────────────────
    # LIFECYCLE: Written by verification_node; read by evaluation_node, memory_sync_node.
    #   verification_passed, verification_result — linter/check verification
    #   evaluation_result — overall pass/fail from LLM evaluator
    #   evaluation_llm_verdict, evaluation_llm_reason — LLM rationale
    #   inline_verification_failures — P3.1 per-step checkpoint failures from
    #     frontier_loop_node; the failures are also injected into history so the
    #     model can self-correct within the loop.
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
    inline_verification_failures: List[Dict[str, Any]] | None
    evaluation_result: str | None
    evaluation_llm_verdict: str | None
    evaluation_llm_reason: str | None

    # ── Analysis & context ─────────────────────────────────────────────────
    # LIFECYCLE: Written by analysis_node, analyst_delegation_node; read by planning_node, execution_node.
    #   analysis_summary — LLM distillation of repo context
    #   relevant_files, key_symbols — file/symbol lists from repo analysis
    #   analyst_findings — free-text findings from analyst subagent
    #   repo_summary_data — cached repo structure overview
    #   call_graph, test_map — dependency graphs from analysis_node
    #   step_controller_enabled — config flag for step-by-step mode
    #   empty_response_count — counter for LLM returning empty responses
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
    # LIFECYCLE: Written by delegation_node / analyst_delegation_node; read by wait_for_delegations.
    #   delegation_results — merged results after all sub-delegations complete
    #   delegations — list of pending delegation tasks
    #   delegation_depth — current nesting depth (prevents infinite recursion)
    delegation_results: Dict[str, Any] | None
    delegations: List[Dict[str, Any]] | None
    delegation_depth: int | None

    # ── Memory / distillation ──────────────────────────────────────────────
    # LIFECYCLE: Written by perception_node, memory_sync_node; read by perception_node.
    #   _should_distill — set when context budget exceeds threshold
    #   _force_compact — set true by /compact command
    #   _budget_compaction — set when token budget is tight
    #   _compacted_history — result of last compaction
    #   _compaction_last_round — turn number of last compaction
    #   last_compact_at, last_compact_turn — diagnostic timestamps
    #   context_degradation_detected — set when compaction quality is low
    _should_distill: bool | None
    _force_compact: bool | None
    _budget_compaction: bool | None
    _compacted_history: List[Dict[str, Any]] | None
    _compaction_last_round: int | None
    last_compact_at: Any | None
    last_compact_turn: int | None
    context_degradation_detected: bool | None

    # ── Cost & telemetry ───────────────────────────────────────────────────
    # LIFECYCLE: Written by evaluation_node / memory_sync_node; read by TUI bridge.
    #   session_cost_usd — cumulative cost accumulator
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
    modified_files: List[str] | None
    retrieved_snippets: List[Dict[str, Any]] | None
    _frontier_loop_turns: int | None

    # ── Target State Contract (Engineering Audit §Appendix) ────────────────
    # Target 7-node architecture: runtime, run, pending_action, last_action, verification.
    runtime: Dict[str, Any] | None
    run: Dict[str, Any] | None
    pending_action: Dict[str, Any] | None
    last_action: Dict[str, Any] | None
    verification: Dict[str, Any] | None


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
    "tool_limit",
    "context_limit",
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
