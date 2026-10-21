"""Graph runtime state contracts and tier capability policies.

Implements the target state contract and tier capability policy defined in the
Engineering Audit (§Appendix & §13):
- Single unified graph topology for 15-30B medium and frontier models.
- Immutable policies resolved once before graph compilation and stored in state.
- Target State Contract: runtime, run, pending_action, last_action, verification.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, TypedDict

from src.tools._capabilities import (
    FRONTIER_FULL_CAPABILITIES,
    MEDIUM_CORE_CAPABILITIES,
)

logger = logging.getLogger(__name__)

TargetTier = Literal["medium", "frontier"]
RunStatus = Literal[
    "running",
    "awaiting_approval",
    "verifying",
    "recovering",
    "completed",
    "failed",
    "cancelled",
]
RunOutcome = Literal[
    "continue",
    "approval_required",
    "verify",
    "recover",
    "compact",
    "complete",
    "failed",
    "cancelled",
]
VerificationMode = Literal["targeted", "full"]


class Policy(TypedDict, total=False):
    """Immutable execution policy for a model tier."""

    turn_limit: int
    tool_limit: int
    recovery_limit: int
    compaction_limit: int
    verification_mode: VerificationMode
    readonly_parallelism: bool
    mutations_serialized: bool
    preflight_required: bool


class RuntimeState(TypedDict, total=False):
    """Target runtime metadata and immutable capability policy."""

    tier: TargetTier
    provider: str
    model: str
    capabilities: List[str]
    policy: Policy


class RunState(TypedDict, total=False):
    """Execution status and monotonic execution counters."""

    status: RunStatus
    outcome: RunOutcome
    turns: int
    tool_calls: int
    recoveries: int
    compactions: int


class PendingAction(TypedDict, total=False):
    """An action awaiting approval or dispatch."""

    name: str
    arguments: Dict[str, Any]
    summary: str
    digest: str


class LastAction(TypedDict, total=False):
    """Normalized result envelope emitted after every tool action."""

    name: str
    arguments: Dict[str, Any]
    result: Any
    side_effects: List[str]
    modified_files: List[str]
    digest: str


class VerificationState(TypedDict, total=False):
    """Evidence-based validation state."""

    required: bool
    passed: Optional[bool]
    commands: List[str]
    evidence: List[Dict[str, Any]]


# Authoritative Tier Capability Policies per Engineering Audit §Appendix
MEDIUM_POLICY: Policy = {
    "turn_limit": 6,
    "tool_limit": 20,
    "recovery_limit": 2,
    "compaction_limit": 1,
    "verification_mode": "targeted",
    "readonly_parallelism": False,
    "mutations_serialized": True,
    "preflight_required": True,
}

FRONTIER_POLICY: Policy = {
    "turn_limit": 12,
    "tool_limit": 40,
    "recovery_limit": 3,
    "compaction_limit": 2,
    "verification_mode": "full",
    "readonly_parallelism": True,
    "mutations_serialized": True,
    "preflight_required": True,
}


def normalize_target_tier(tier: Any) -> TargetTier:
    """Normalize model tier names to the target 'medium' | 'frontier' tier."""
    if not tier:
        return "medium"
    tier_str = str(tier).lower().strip()
    if hasattr(tier, "value"):
        tier_str = str(tier.value).lower().strip()

    if tier_str in ("frontier", "large", "cloud"):
        return "frontier"
    return "medium"


def get_policy_for_tier(tier: Any) -> Policy:
    """Return the authoritative immutable Policy for the specified tier."""
    norm = normalize_target_tier(tier)
    if norm == "frontier":
        return dict(FRONTIER_POLICY)  # type: ignore[return-value]
    return dict(MEDIUM_POLICY)  # type: ignore[return-value]


def get_capabilities_for_tier(tier: Any) -> List[str]:
    """Return the allowed capability tool names for the specified tier."""
    norm = normalize_target_tier(tier)
    if norm == "frontier":
        return sorted(FRONTIER_FULL_CAPABILITIES)
    return sorted(MEDIUM_CORE_CAPABILITIES)


def compute_action_digest(name: str, arguments: Mapping[str, Any] | None) -> str:
    """Compute a canonical SHA-256 digest for an action and its arguments.

    The digest binds an approval to the exact action shape and parameters.
    """
    raw_name = str(name or "").strip()
    args_dict = dict(arguments) if isinstance(arguments, Mapping) else {}
    serialized = json.dumps(args_dict, sort_keys=True, default=str)
    payload = f"{raw_name}:{serialized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_runtime_state(
    *,
    tier: Any,
    provider: str = "",
    model: str = "",
    custom_policy: Optional[Mapping[str, Any]] = None,
) -> RuntimeState:
    """Create the canonical initial runtime state dictionary."""
    norm_tier = normalize_target_tier(tier)
    base_policy = get_policy_for_tier(norm_tier)
    if custom_policy:
        base_policy.update(custom_policy)  # type: ignore[typeddict-item]
    return {
        "tier": norm_tier,
        "provider": str(provider or ""),
        "model": str(model or ""),
        "capabilities": get_capabilities_for_tier(norm_tier),
        "policy": base_policy,
    }


def create_run_state(
    *,
    status: RunStatus = "running",
    outcome: RunOutcome = "continue",
    turns: int = 0,
    tool_calls: int = 0,
    recoveries: int = 0,
    compactions: int = 0,
) -> RunState:
    """Create the canonical run state dictionary with monotonic counters."""
    return {
        "status": status,
        "outcome": outcome,
        "turns": int(turns),
        "tool_calls": int(tool_calls),
        "recoveries": int(recoveries),
        "compactions": int(compactions),
    }


def create_last_action(
    *,
    name: str,
    arguments: Mapping[str, Any] | None,
    result: Any,
    side_effects: Optional[Sequence[str]] = None,
    modified_files: Optional[Sequence[str]] = None,
) -> LastAction:
    """Assemble a normalized last_action envelope."""
    norm_name = str(name or "").strip()
    norm_args = dict(arguments) if isinstance(arguments, Mapping) else {}
    digest = compute_action_digest(norm_name, norm_args)
    return {
        "name": norm_name,
        "arguments": norm_args,
        "result": result,
        "side_effects": list(side_effects or []),
        "modified_files": list(modified_files or []),
        "digest": digest,
    }
