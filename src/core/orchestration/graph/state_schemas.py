"""Focused node-result schemas and graph-boundary transition enforcement.

STATE-01: Instead of only logging state issues, each cognitive node now has a
declared *output schema* describing the keys its result dict may write into
``AgentState``.  The graph boundary wrapper validates the dict a node returns
and either raises a structured ``NodeResultViolation`` (fail-closed/strict) or
logs + emits a typed ``NodeResultValidationFailed`` event (fail-open/default),
so an invalid transition cannot silently pollute shared state.

Contract (per node):
- ``allowed_keys`` — the complete set of top-level keys the node is known to
  write.  Keys outside this set are *unknown* and rejected.  These allow-lists
  are exhaustive supersets gathered from every return path of each node, so a
  correct result can never be rejected.
- ``core_keys`` — top-level keys present in *every* return shape of the node.
  A result missing a core key is *broken*.  ``execution`` has divergent shapes
  (the preview-mode early return shares no keys with the tool-call success
  path), so its core is deliberately empty and only the structural allow-list
  is enforced.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.messaging.event_types import NodeResultValidationFailed

_logger = logging.getLogger(__name__)

# Per-node output schemas (see module docstring for the contract).  The
# allow-list unions were derived from an exhaustive inventory of every return
# path (see docs/ARCHITECTURAL_REVIEW_AND_ROADMAP.md, STATE-01).
@dataclass(frozen=True)
class NodeOutputSchema:
    """Declared output contract for a single cognitive node."""

    allowed_keys: frozenset[str]
    core_keys: tuple[str, ...] = ()


_NODE_OUTPUT_SCHEMAS: dict[str, NodeOutputSchema] = {
    "perception": NodeOutputSchema(
        frozenset(
            {
                "history",
                "next_action",
                "rounds",
                "errors",
                "turn_count",
                "last_result",
                "empty_response_count",
                "needs_clarification",
                "model_tier",
                "_compacted_history",
                "_budget_compaction",
                "_should_distill",
                "session_cost_usd",
                "snapshots",
                "current_plan",
                "current_step",
                "task_decomposed",
                "original_task",
                "agent_mode",
                "task_complexity",
                "_compaction_last_round",
            }
        ),
        core_keys=("history", "next_action", "rounds"),
    ),
    "planning": NodeOutputSchema(
        frozenset(
            {
                "current_plan",
                "current_step",
                "plan_attempts",
                "plan_mode_approved",
                "errors",
                "task_decomposed",
                "plan_resumed",
                "step_description",
                "next_action",
                "plan_dag",
                "execution_waves",
                "current_wave",
                "affected_files",
                "relevant_files",
                "key_symbols",
                "plan_history",
            }
        ),
        core_keys=("current_plan", "current_step", "plan_attempts", "plan_mode_approved"),
    ),
    "execution": NodeOutputSchema(
        frozenset(
            {
                "last_result",
                "errors",
                "next_action",
                "history",
                "_compacted_history",
                "_budget_compaction",
                "_should_distill",
                "step_retry_counts",
                "current_step",
                "current_plan",
                "task",
                "tool_call_count",
                "doom_loop_behavior",
                "recent_tool_calls",
                "awaiting_plan_approval",
                "awaiting_user_input",
                "plan_mode_blocked_tool",
                "pending_preview_id",
                "preview_confirmed",
                "last_tool_name",
                "verified_reads",
                "tool_last_used",
                "files_read",
                "current_wave",
                "replan_required",
                "action_failed",
                "plan_progress",
                "plan_mode_approved",
                "no_plan_fail_count",
                "affected_files",
                "snapshots",
            }
        ),
        # The preview-mode early return shares no keys with the tool-call
        # success path, so there is no single key present in every shape.
        # Enforcement here is structural (allow-list) only.
        core_keys=(),
    ),
    "verification": NodeOutputSchema(
        frozenset({"verification_result", "verification_passed"}),
        core_keys=("verification_result",),
    ),
    "analysis": NodeOutputSchema(
        frozenset(
            {
                "analysis_summary",
                "relevant_files",
                "key_symbols",
                "repo_summary_data",
                "call_graph",
                "test_map",
                "analysis_failed",
            }
        ),
        core_keys=("analysis_summary", "relevant_files", "key_symbols"),
    ),
    "step_controller": NodeOutputSchema(
        frozenset(
            {
                "next_action",
                "step_description",
                "planned_action",
                "step_retry_counts",
                "step_lint_warnings",
            }
        ),
        # Path A returns {} (disabled/no plan), so there is no single key in
        # every return shape.
        core_keys=(),
    ),
    "evaluation": NodeOutputSchema(
        frozenset(
            {
                "evaluation_result",
                "evaluation_llm_verdict",
                "evaluation_llm_reason",
                "next_action",
                "errors",
                "replan_required",
                "action_failed",
            }
        ),
        core_keys=("evaluation_result",),
    ),
    "debug": NodeOutputSchema(
        frozenset(
            {
                "next_action",
                "errors",
                "debug_attempts",
                "total_debug_attempts",
                "total_recovery_attempts",
                "last_debug_error_type",
            }
        ),
        core_keys=("next_action",),
    ),
    "replan": NodeOutputSchema(
        frozenset(
            {
                "replan_required",
                "action_failed",
                "replan_attempts",
                "total_recovery_attempts",
                "errors",
                "current_plan",
                "current_step",
                "execution_waves",
                "last_plan_hash",
                "history",
            }
        ),
        core_keys=("replan_required", "action_failed", "replan_attempts", "total_recovery_attempts"),
    ),
    "delegation": NodeOutputSchema(
        frozenset({"delegation_results", "history", "_file_lock_manager"}),
        # Path A returns {} (no delegations), so core is empty.
        core_keys=(),
    ),
    "analyst_delegation": NodeOutputSchema(
        frozenset({"analyst_findings"}),
        core_keys=("analyst_findings",),
    ),
    "wait_for_user": NodeOutputSchema(
        frozenset(
            {
                "awaiting_plan_approval",
                "awaiting_user_input",
                "plan_mode_approved",
                "plan_mode_blocked_tool",
                "last_result",
                "preview_confirmed",
                "pending_preview_id",
            }
        ),
        core_keys=("awaiting_user_input",),
    ),
    "memory_sync": NodeOutputSchema(
        frozenset({"_force_compact", "errors", "analysis_summary", "history"}),
        core_keys=("_force_compact", "errors"),
    ),
    "plan_validator": NodeOutputSchema(
        frozenset({"plan_validation", "errors", "action_failed"}),
        core_keys=("plan_validation",),
    ),
}


@dataclass
class NodeResultViolation(Exception):
    """Structured error for an invalid node->state transition.

    Attributes:
        node_name: the graph node that produced the invalid result.
        reason: short machine-readable reason (``unknown_key``, ``missing_core_key``).
        details: a list of human-readable detail messages.
    """

    node_name: str
    reason: str
    details: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"NodeResultViolation(node={self.node_name!r}, reason={self.reason!r}, "
            f"details={self.details!r})"
        )


def get_node_output_schema(node_name: str) -> NodeOutputSchema | None:
    """Return the declared output schema for *node_name*, or None if unknown."""
    return _NODE_OUTPUT_SCHEMAS.get(node_name)


def register_node_output_schema(node_name: str, schema: NodeOutputSchema) -> None:
    """Register (or override) the output schema for a node.  For tests/extensions."""
    _NODE_OUTPUT_SCHEMAS[node_name] = schema


def validate_node_result(node_name: str, result: Mapping[str, Any]) -> list[NodeResultViolation]:
    """Validate *result* (a node's returned state update) against its schema.

    Returns a (possibly empty) list of structured ``NodeResultViolation``s.
    Does not raise; callers decide whether to surface as error or as event.
    """
    if not isinstance(result, Mapping):
        return [
            NodeResultViolation(
                node_name=node_name,
                reason="non_mapping_result",
                details=[f"node returned {type(result).__name__}, expected a Mapping"],
            )
        ]

    schema = _NODE_OUTPUT_SCHEMAS.get(node_name)
    if schema is None:
        return []  # no declared contract for this node → nothing to enforce

    violations: list[NodeResultViolation] = []

    unknown = [k for k in result if k not in schema.allowed_keys]
    if unknown:
        violations.append(
            NodeResultViolation(
                node_name=node_name,
                reason="unknown_key",
                details=[f"unknown output key {k!r}" for k in sorted(unknown)],
            )
        )

    missing = [k for k in schema.core_keys if k not in result]
    if missing:
        violations.append(
            NodeResultViolation(
                node_name=node_name,
                reason="missing_core_key",
                details=[f"missing required core key {k!r}" for k in missing],
            )
        )

    return violations


def wrap_node(
    node_name: str,
    fn: Callable[..., Any],
    *,
    strict: bool = False,
    publish_violation: Callable[[NodeResultViolation], None] | None = None,
) -> Callable[..., Any]:
    """Wrap a graph node so its returned state update is validated at the boundary.

    Parameters
    ----------
    node_name:
        The graph node name (must have a registered schema, else passes through).
    fn:
        The node implementation (sync or async).
    strict:
        When True, an invalid result raises the first ``NodeResultViolation``
        (fail-closed).  When False (default), violations are logged and emitted
        as a typed ``NodeResultValidationFailed`` event via the orchestrator's
        event bus (resolved from state/config) — the live graph runs fail-open
        by default so a bad transition is surfaced, not fatal.
    publish_violation:
        Optional callback invoked with each violation.  Overrides the default
        orchestrator-event-bus publisher.  Called only in non-strict mode.

    Returns a wrapper that preserves sync/async semantics of ``fn``.
    """
    if node_name not in _NODE_OUTPUT_SCHEMAS:
        return fn

    if inspect.iscoroutinefunction(fn):

        async def _async_wrapper(
            state: Mapping[str, Any],
            config: RunnableConfig | None,
            *args: Any,
            **kwargs: Any,
        ):
            result = await fn(state, config, *args, **kwargs)
            _enforce(
                node_name,
                result,
                state=state,
                config=config,
                strict=strict,
                publish_violation=publish_violation,
            )
            return result

        return _async_wrapper

    def _sync_wrapper(
        state: Mapping[str, Any],
        config: RunnableConfig | None,
        *args: Any,
        **kwargs: Any,
    ):
        result = fn(state, config, *args, **kwargs)
        _enforce(
            node_name,
            result,
            state=state,
            config=config,
            strict=strict,
            publish_violation=publish_violation,
        )
        return result

    return _sync_wrapper


def _default_publish_violation(
    state: Mapping[str, Any],
    config: Any,
    violation: NodeResultViolation,
) -> None:
    """Publish a ``NodeResultValidationFailed`` typed event via the orchestrator.

    Resolves the orchestrator from ``state``/``config`` (same strategy as the
    nodes) and falls back to the global singleton EventBus.  Never raises.
    """
    try:
        from src.core.orchestration.graph.nodes.node_utils import (
            _resolve_orchestrator,
        )

        orchestrator = _resolve_orchestrator(state, config)
        event_bus = getattr(orchestrator, "event_bus", None) if orchestrator else None
        if event_bus is None:
            from src.core.orchestration.event_bus import get_event_bus

            event_bus = get_event_bus()
        if event_bus is not None and hasattr(event_bus, "publish_typed"):
            session_id = state.get("session_id", "")
            event_bus.publish_typed(
                NodeResultValidationFailed(
                    node_name=violation.node_name,
                    reason=violation.reason,
                    details=list(violation.details),
                    session_id=session_id if isinstance(session_id, str) else "",
                )
            )
    except Exception:
        _logger.debug("state_schemas: default publish failed", exc_info=True)


def _enforce(
    node_name: str,
    result: Any,
    *,
    state: Mapping[str, Any],
    config: Any,
    strict: bool,
    publish_violation: Callable[[NodeResultViolation], None] | None,
) -> None:
    violations = validate_node_result(node_name, result) if isinstance(result, Mapping) else [
        NodeResultViolation(node_name=node_name, reason="non_mapping_result", details=[])
    ]
    if not violations:
        return

    publisher = publish_violation
    if publisher is None:
        publisher = lambda v: _default_publish_violation(state, config, v)  # noqa: E731

    for violation in violations:
        _logger.error(
            "state_schemas: invalid %s transition (%s): %s",
            node_name,
            violation.reason,
            violation.details,
        )
        if strict:
            raise violation
        try:
            publisher(violation)
        except Exception:  # publisher failure must not break the node
            _logger.debug("state_schemas: publish_violation failed", exc_info=True)
