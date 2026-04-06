"""errors.py — Typed error hierarchy for CodingAgent.

S0-B: Provides ``AgentError`` and ``ErrorCode`` so that nodes and adapters can
raise/return structured errors instead of ad-hoc string dicts.

These types are additive — existing ``{"status": "error", "message": "..."}``
returns remain valid during the transition.  New code should use ``AgentError``
and callers should check ``isinstance(exc, AgentError)`` or read ``exc.code``.

Usage::

    from src.core.errors import AgentError, ErrorCode

    raise AgentError(ErrorCode.RATE_LIMIT, "Provider returned 429", retryable=True)

    # In a node:
    try:
        ...
    except AgentError as exc:
        state["last_error"] = exc.to_dict()
        if not exc.retryable:
            return {**state, "next_action": "fail"}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class ErrorCode(str, Enum):
    """Structured error codes used by AgentError."""

    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    TOOL_FAILURE = "tool_failure"
    PARSE_FAILURE = "parse_failure"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    VALIDATION_ERROR = "validation_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    MAX_TURNS_REACHED = "max_turns_reached"
    DOOM_LOOP = "doom_loop"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass
class AgentError(Exception):
    """Structured agent error with error code and retry hint.

    Attributes:
        code:      Enum value identifying the error category.
        message:   Human-readable description.
        retryable: Whether the operation should be retried.
        context:   Extra key/value pairs for debugging (provider name, tool name, etc.).
    """

    code: ErrorCode
    message: str
    retryable: bool = True
    context: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        suffix = f" [{self.code.value}]"
        if not self.retryable:
            suffix += " (non-retryable)"
        return self.message + suffix

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON or AgentState storage."""
        return {
            "error_code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            **self.context,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentError":
        """Deserialise from a ``to_dict()`` payload."""
        code_str = data.get("error_code", ErrorCode.UNKNOWN.value)
        try:
            code = ErrorCode(code_str)
        except ValueError:
            code = ErrorCode.UNKNOWN
        ctx = {k: v for k, v in data.items() if k not in ("error_code", "message", "retryable")}
        return cls(
            code=code,
            message=str(data.get("message", "")),
            retryable=bool(data.get("retryable", True)),
            context=ctx,
        )
