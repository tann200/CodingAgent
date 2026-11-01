"""Focused node-result schemas and graph-boundary transition enforcement.

STATE-01: Instead of only logging state issues, each cognitive node has a
declared *output schema* describing the keys its result dict may write into
``AgentState``.  The graph boundary wrapper validates the dict a node returns
and either raises a structured ``NodeResultViolation`` (fail-closed/strict) or
logs + emits a typed ``NodeResultValidationFailed`` event (fail-open/default),
so an invalid transition cannot silently pollute shared state.

Canonical 2026 6-Node Architecture:
- initialize
- agent_loop (alias frontier_loop)
- approval (delegate wait_for_user)
- verify (delegate verification)
- compact
- finalize (delegate memory_sync)
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from langchain_core.runnables import RunnableConfig

from src.core.messaging.event_types import NodeResultValidationFailed

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeOutputSchema:
    """Declared output contract for a single cognitive node."""

    allowed_keys: frozenset[str]
    core_keys: tuple[str, ...] = ()


_CANONICAL_LOOP_KEYS = frozenset(
    {
        "history",
        "tool_call_count",
        "last_result",
        "last_tool_name",
        "errors",
        "_frontier_loop_turns",
        "awaiting_plan_approval",
        "plan_mode_blocked_tool",
        "next_action",
        "inline_verification_failures",
        "replan_required",
        "action_failed",
        "_budget_compaction",
        "_should_distill",
        "plan_completed",
        "modified_files",
        "_verified_file_hashes",
        "retrieved_snippets",
        "active_role",
        "runtime",
        "run",
        "pending_action",
        "last_action",
        "verification",
    }
)

_CANONICAL_APPROVAL_KEYS = frozenset(
    {
        "pending_action",
        "next_action",
        "last_action",
        "run",
        "awaiting_plan_approval",
        "awaiting_user_input",
        "plan_mode_approved",
        "plan_mode_blocked_tool",
        "last_result",
        "preview_confirmed",
        "pending_preview_id",
        "errors",
    }
)

_CANONICAL_VERIFY_KEYS = frozenset(
    {
        "verification",
        "verification_result",
        "verification_passed",
        "run",
        "errors",
        "modified_files",
        "history",
        "_verified_file_hashes",
    }
)

_NODE_OUTPUT_SCHEMAS: dict[str, NodeOutputSchema] = {
    "initialize": NodeOutputSchema(
        frozenset(
            {
                "runtime",
                "run",
                "pending_action",
                "last_action",
                "verification",
                "modified_files",
                "errors",
                "model_tier",
            }
        ),
        core_keys=("runtime", "run"),
    ),
    "agent_loop": NodeOutputSchema(_CANONICAL_LOOP_KEYS, core_keys=()),
    "frontier_loop": NodeOutputSchema(_CANONICAL_LOOP_KEYS, core_keys=()),
    "approval": NodeOutputSchema(_CANONICAL_APPROVAL_KEYS, core_keys=()),
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
    "verify": NodeOutputSchema(_CANONICAL_VERIFY_KEYS, core_keys=()),
    "verification": NodeOutputSchema(
        frozenset(
            {
                "verification_result",
                "verification_passed",
                "verification",
                "run",
            }
        ),
        core_keys=("verification_result",),
    ),
    "compact": NodeOutputSchema(
        frozenset(
            {
                "history",
                "run",
                "errors",
                "_budget_compaction",
                "_should_distill",
                "_force_compact",
                "_compacted_history",
                "last_compact_turn",
                "last_compact_at",
                "context_degradation_detected",
            }
        ),
        core_keys=(),
    ),
    "finalize": NodeOutputSchema(
        frozenset(
            {
                "run",
                "history",
                "errors",
                "_force_compact",
                "analysis_summary",
                "evaluation_result",
                "session_cost_usd",
            }
        ),
        core_keys=(),
    ),
    "memory_sync": NodeOutputSchema(
        frozenset({"_force_compact", "errors", "analysis_summary", "history"}),
        core_keys=("_force_compact", "errors"),
    ),
}


@dataclass
class NodeResultViolation(Exception):
    """Structured error for an invalid node->state transition."""

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
    """Register (or override) the output schema for a node. For tests/extensions."""
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
    """Wrap a graph node so its returned state update is validated at the boundary."""
    if node_name not in _NODE_OUTPUT_SCHEMAS:
        return fn

    if inspect.iscoroutinefunction(fn):

        async def _async_wrapper(
            state: Mapping[str, Any],
            config: Optional[RunnableConfig] = None,
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
        config: Optional[RunnableConfig] = None,
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
    """Publish a ``NodeResultValidationFailed`` typed event via the orchestrator."""
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
        try:
            from src.core.observability.metrics import metrics

            metrics.increment_counter("graph.node_validation_failed")
            metrics.increment_counter(
                f"graph.node_validation_failed.node.{violation.node_name}"
            )
            metrics.increment_counter(
                f"graph.node_validation_failed.reason.{violation.reason}"
            )
        except Exception:
            pass
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
        except Exception:
            _logger.debug("state_schemas: publish_violation failed", exc_info=True)
