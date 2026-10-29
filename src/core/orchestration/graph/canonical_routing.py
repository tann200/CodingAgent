"""Canonical routing for the unified 7-node LangGraph cognitive pipeline.

Implements conditional routing per Engineering Audit §Appendix:
- START -> initialize -> agent_loop
- agent_loop -> [agent_loop | approval | verify | recover | compact | finalize]
- approval -> [agent_loop | finalize]
- verify -> [finalize | recover]
- recover -> [agent_loop | finalize]
- compact -> [agent_loop | finalize]
- finalize -> END
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)


def route_after_agent_loop(
    state: Mapping[str, Any],
) -> Literal["agent_loop", "approval", "verify", "recover", "compact", "finalize"]:
    """Route exit of agent_loop based on run outcome and action state."""
    run = state.get("run") or {}
    outcome = run.get("outcome")

    if outcome == "approval_required" or state.get("awaiting_plan_approval"):
        return "approval"

    if outcome == "compact" or "context_overflow" in (state.get("errors") or []):
        return "compact"

    if outcome == "recover" or state.get("replan_required"):
        if state.get("use_replan") is False:
            return "finalize"
        return "recover"

    if outcome == "verify":
        if state.get("use_verification") is False:
            return "finalize"
        return "verify"

    if outcome in ("complete", "failed", "cancelled"):
        return "finalize"

    if outcome == "continue":
        return "agent_loop"

    # Fallback heuristics for transitional states
    if state.get("modified_files") or state.get("last_action"):
        if state.get("use_verification") is False:
            return "finalize"
        return "verify"

    return "finalize"


def route_after_approval(
    state: Mapping[str, Any],
) -> Literal["agent_loop", "finalize"]:
    """Route after approval node."""
    run = state.get("run") or {}
    outcome = run.get("outcome")

    approved = (
        outcome == "continue"
        or bool(state.get("plan_mode_approved"))
        or bool(state.get("preview_confirmed"))
    )
    if approved:
        return "agent_loop"
    return "finalize"


def route_after_verify(
    state: Mapping[str, Any],
) -> Literal["recover", "finalize"]:
    """Route after verification node."""
    run = state.get("run") or {}
    outcome = run.get("outcome")

    if outcome == "recover" or state.get("verification_passed") is False:
        recoveries = int(run.get("recoveries") or state.get("debug_attempts") or 0)
        policy = (state.get("runtime") or {}).get("policy") or {}
        recovery_limit = int(policy.get("recovery_limit") or 2)
        if recoveries < recovery_limit:
            return "recover"
        return "finalize"

    return "finalize"


def route_after_recover(
    state: Mapping[str, Any],
) -> Literal["agent_loop", "finalize"]:
    """Route after recovery node."""
    run = state.get("run") or {}
    outcome = run.get("outcome")

    if (
        outcome == "continue"
        or state.get("pending_action")
        or state.get("next_action")
    ):
        return "agent_loop"
    return "finalize"


def route_after_compact(
    state: Mapping[str, Any],
) -> Literal["agent_loop", "finalize"]:
    """Route after compaction node."""
    run = state.get("run") or {}
    outcome = run.get("outcome")
    errors = state.get("errors") or []

    if outcome == "continue" and "context_overflow_exhausted" not in errors:
        return "agent_loop"
    return "finalize"
