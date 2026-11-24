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
    TYPE_CHECKING,
)

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


# ---------------------------------------------------------------------------
# AgentState — shared state of the cognitive pipeline (TASK-25 slimmed).
# All Optional[X] annotations are written as X | None (Python 3.10+ union
# syntax) for consistency with the rest of the codebase.
# ---------------------------------------------------------------------------


class _AgentStateSpec(TypedDict, total=False):
    """Shared state of the 6-node canonical cognitive pipeline."""

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
    history: Annotated[List[Dict[str, Any]], merge_or_replace_list]
    verified_reads: Annotated[List[str], merge_or_replace_list]
    task_history: List[Dict[str, Any]] | None
    errors: List[str]

    # ── Plan & step ────────────────────────────────────────────────────────
    current_plan: List[Dict[str, Any]] | None
    current_step: int | None
    plan_completed: bool | None
    planned_action: Dict[str, Any] | None
    replan_required: str | None
    step_retry_counts: Dict[str, int] | None
    no_plan_fail_count: int | None
    affected_files: List[str] | None

    # ── Plan approval / preview ────────────────────────────────────────────
    plan_validation: Dict[str, Any] | None
    plan_enforce_warnings: bool | None
    awaiting_plan_approval: bool | None
    plan_mode_approved: bool | None
    plan_mode_blocked_tool: str | None
    pending_preview_id: str | None
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
    last_error_code: str | None
    needs_clarification: bool | None

    # ── Verification ───────────────────────────────────────────────────────
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
    inline_verification_failures: List[Dict[str, Any]] | None
    evaluation_result: str | None

    # ── Analysis & context ─────────────────────────────────────────────────
    analysis_summary: str | None
    relevant_files: List[str] | None
    analyst_findings: str | None
    repo_summary_data: str | None

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

    # ── Cost & telemetry ───────────────────────────────────────────────────
    session_cost_usd: float | None

    # ── Internal / private ─────────────────────────────────────────────────
    _p2p_context: List[Dict[str, Any]] | None
    _file_lock_manager: Any | None
    _write_queue: List[Dict[str, Any]] | None
    _agent_session_manager: Any | None
    _context_controller: Any | None
    _pending_injections_source: Any | None

    # ── Control flow ───────────────────────────────────────────────────────
    cancel_event: Any | None
    modified_files: List[str] | None
    _verified_file_hashes: Dict[str, str] | None
    retrieved_snippets: List[Dict[str, Any]] | None
    _frontier_loop_turns: int | None

    # ── Target State Contract (Engineering Audit §Appendix) ────────────────
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
    "no_plan_fail_count",
    "turn_count",
    "max_turns",
    "seed",
)
