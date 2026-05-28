from __future__ import annotations

"""AgentState — shared state of the LangGraph cognitive pipeline.

Overview
--------
``AgentState`` (alias ``_AgentStateSpec``) is a flat ``TypedDict`` threaded
through every node in the LangGraph graph.  All 79 fields are ``total=False``
(all optional at the TypedDict level); runtime defaults are supplied by node
initialisation code.

Two fields use LangGraph *annotated reducers* so parallel branches can safely
append without clobbering each other:

- ``history``       — ``merge_or_replace_list``  (append; use ``ReplaceList``
                      to replace wholesale, e.g. after compaction)
- ``verified_reads`` — same reducer

All other fields are last-write-wins: the node that finishes last in a parallel
fan-out wins.

Node Execution Order (Standard Graph — NANO/SMALL/MEDIUM model tiers)
----------------------------------------------------------------------
Defined in ``src/core/orchestration/graph/builder.py:compile_agent_graph()``.

::

    ENTRY
      └─ perception ──────────────────────────────────────────────────────────
            │  Reads:  task, history, rounds, turn_count, max_turns,
            │          current_plan, cancel_event, model_tier, …
            │  Writes: history, next_action, rounds, model_tier,
            │          task_complexity, _should_distill, _budget_compaction, …
            ├─► execution          (tool call path / simple task fast-path)
            ├─► analysis           (default: context-gathering)
            ├─► planning           (simple task: bypass analysis)
            └─► memory_sync        (task already complete)

      └─ analysis ─────────────────────────────────────────────────────────────
            │  Reads:  task, working_dir, model_tier
            │  Writes: analysis_summary, relevant_files, key_symbols,
            │          repo_summary_data, call_graph, test_map
            ├─► analyst_delegation (complex task)
            └─► planning           (simple task)

      └─ analyst_delegation ───────────────────────────────────────────────────
            │  Reads:  task, analysis_summary, relevant_files, working_dir
            │  Writes: analyst_findings
            └─► planning

      └─ planning ─────────────────────────────────────────────────────────────
            │  Reads:  task, history, current_plan, analysis_summary,
            │          plan_attempts, model_tier
            │  Writes: current_plan, current_step, task_decomposed, plan_dag,
            │          execution_waves, plan_attempts, affected_files
            └─► plan_validator

      └─ plan_validator ───────────────────────────────────────────────────────
            │  Reads:  current_plan, plan_enforce_warnings, plan_strict_mode
            │  Writes: plan_validation, action_failed, errors
            ├─► execution          (plan valid)
            ├─► planning           (plan invalid → re-plan)
            └─► wait_for_user      (plan-mode: awaiting approval)

      └─ execution ────────────────────────────────────────────────────────────
            │  Reads:  task, working_dir, history, next_action, planned_action,
            │          step_description, tool_call_count, cancel_event, …
            │  Writes: last_result, last_tool_name, verified_reads, history,
            │          next_action, tool_call_count, plan_progress,
            │          replan_required, no_plan_fail_count, snapshots, …
            ├─► wait_for_user      (preview pending)
            ├─► step_controller    (plan step done → advance step)
            ├─► perception         (WR-1 fast-path: read-only round-trip)
            ├─► memory_sync        (tool budget exhausted)
            ├─► replan             (replan_required set)
            └─► analysis           (tool fail → re-analysis)

      └─ wait_for_user ────────────────────────────────────────────────────────
            │  Reads:  awaiting_plan_approval, current_plan,
            │          pending_preview_id, awaiting_user_input
            │  Writes: awaiting_plan_approval, plan_mode_approved,
            │          preview_confirmed, pending_preview_id
            ├─► execution          (confirmed / approved)
            ├─► perception         (preview rejected)
            └─► planning           (plan rejected → re-plan)

      └─ step_controller ──────────────────────────────────────────────────────
            │  Reads:  current_plan, current_step, last_result,
            │          step_retry_counts, model_tier, working_dir
            │  Writes: step_description, planned_action, step_retry_counts,
            │          step_lint_warnings, next_action
            ├─► execution          (execute current step)
            ├─► verification       (plan exhausted)
            ├─► planning           (no plan)
            └─► END                (cancelled / plan empty)

      └─ replan ───────────────────────────────────────────────────────────────
            │  Reads:  replan_required, current_plan, replan_attempts, task
            │  Writes: current_plan, current_step, execution_waves,
            │          replan_required, replan_attempts, history, errors
            ├─► step_controller    (new plan ready)
            ├─► perception         (fallback)
            └─► memory_sync        (recovery cap hit)

      └─ verification ─────────────────────────────────────────────────────────
            │  Reads:  last_result, current_plan, current_step, model_tier
            │  Writes: verification_result, verification_passed
            └─► evaluation

      └─ evaluation ───────────────────────────────────────────────────────────
            │  Reads:  verification_result, verification_passed, errors,
            │          debug_attempts, max_debug_attempts
            │  Writes: evaluation_result, next_action, evaluation_llm_verdict,
            │          replan_required, action_failed, errors
            ├─► memory_sync        (complete)
            ├─► step_controller    (partial completion → next step)
            ├─► debug              (verification failed)
            └─► END

      └─ debug ────────────────────────────────────────────────────────────────
            │  Reads:  debug_attempts, max_debug_attempts, verification_result,
            │          last_result, task, history, cancel_event
            │  Writes: next_action, debug_attempts, total_debug_attempts,
            │          last_debug_error_type, errors
            ├─► execution          (apply fix)
            ├─► memory_sync        (give up)
            └─► END

      └─ memory_sync (memory_update_node) ─────────────────────────────────────
            │  Reads:  working_dir, history, evaluation_result, task,
            │          current_plan, session_id, _should_distill, _force_compact
            │  Writes: _force_compact, errors, analysis_summary, history
            │          (compacted via ReplaceList)
            ├─► delegation         (delegations pending)
            ├─► perception         (loop back for next turn)
            └─► END

      └─ delegation ───────────────────────────────────────────────────────────
            │  Reads:  delegations, working_dir, session_id, delegation_depth
            │  Writes: delegation_results, history
            └─► END

Frontier / Lite Graphs
-----------------------
``_compile_frontier_graph()`` (LARGE/FRONTIER tiers) replaces ``execution``
with a single ``frontier_loop`` node that handles both planning and tool calls
in one pass.

``_compile_lite_graph()`` (LITE/v2 mode) is a minimal
``perception → frontier_loop → memory_sync → END`` pipeline with no
verification or delegation.

Field Lifecycle Quick Reference
--------------------------------
The following fields are written by *multiple* nodes — take care when reading
them mid-graph:

=========================  ====================================================
Field                      Written by
=========================  ====================================================
``next_action``            perception, execution, debug, step_controller,
                           evaluation
``history``                perception, execution, replan, memory_sync,
                           delegation
``current_plan``           planning, replan, execution (plan_advance helper)
``current_step``           planning, replan, execution (plan_advance helper)
``analysis_summary``       analysis, memory_sync (distilled)
``relevant_files``         analysis, planning
``errors``                 perception, plan_validator, execution, replan,
                           debug, memory_sync
``model_tier``             perception (only — set once per turn)
``rounds``                 perception (incremented each turn)
=========================  ====================================================

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
    model_tier: str | None
    deterministic: bool | None
    seed: int | None

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
    # P3-4: Plan versioning — list of previous plan snapshots with timestamps.
    # Populated by planning_node each time current_plan is overwritten.
    # Each entry: {"timestamp": ISO8601, "plan": [...], "reason": str}
    plan_history: List[Dict[str, Any]] | None
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
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
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
