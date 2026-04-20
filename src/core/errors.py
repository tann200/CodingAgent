"""errors.py — Typed error hierarchy for CodingAgent.

S0-B: Provides ``AgentError`` and ``ErrorCode`` so that nodes and adapters can
raise/return structured errors instead of ad-hoc string dicts.

These types are additive — existing ``{"status": "error", "message": "..."}``
returns remain valid during the transition.  New code should use ``AgentError``
and callers should check ``isinstance(exc, AgentError)`` or read ``exc.code``.

TASK-REL-1: Expanded ``ErrorCode`` taxonomy covering all failure categories
(tool, LLM, planning, memory, delegation, system).  ``classify_exception()``
maps any Python exception to a typed code so every ``except Exception`` block
can produce a structured result instead of a raw string.

Usage::

    from src.core.errors import AgentError, ErrorCode, classify_exception

    raise AgentError(ErrorCode.LLM_RATE_LIMITED, "Provider returned 429",
                     retryable=True)

    # In a node:
    try:
        ...
    except Exception as exc:
        code = classify_exception(exc)
        state["last_error_code"] = code.value
        raise AgentError(code, str(exc)) from exc
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class ErrorCode(str, Enum):
    """Structured error codes used by AgentError.

    Grouped by subsystem so the prefix makes the origin immediately obvious:
      tool.*       — tool execution failures
      llm.*        — LLM / inference failures
      plan.*       — planning and validation failures
      memory.*     — memory read/write failures
      delegation.* — subagent delegation failures
      system.*     — catch-all / infrastructure
    """

    # ── Tool execution ────────────────────────────────────────────────────────
    TOOL_PERMISSION_DENIED = "tool.permission_denied"
    TOOL_TIMEOUT = "tool.timeout"
    TOOL_SANDBOX_BLOCKED = "tool.sandbox_blocked"
    TOOL_WORKSPACE_ESCAPE = "tool.workspace_escape"
    TOOL_SSRF_BLOCKED = "tool.ssrf_blocked"
    TOOL_NOT_FOUND = "tool.not_found"
    TOOL_INVALID_ARGS = "tool.invalid_args"
    TOOL_EXECUTION_FAILED = "tool.execution_failed"

    # ── LLM / inference ───────────────────────────────────────────────────────
    LLM_CONTEXT_OVERFLOW = "llm.context_overflow"
    LLM_RATE_LIMITED = "llm.rate_limited"
    LLM_CONNECTION_FAILED = "llm.connection_failed"
    LLM_INVALID_RESPONSE = "llm.invalid_response"
    LLM_CANCELLED = "llm.cancelled"
    LLM_BUDGET_EXCEEDED = "llm.budget_exceeded"
    LLM_MAX_TURNS_REACHED = "llm.max_turns_reached"

    # ── Planning ──────────────────────────────────────────────────────────────
    PLAN_INVALID = "plan.invalid"
    PLAN_MAX_RETRIES = "plan.max_retries"
    PLAN_NO_TOOLS_MATCH = "plan.no_tools"
    PLAN_DOOM_LOOP = "plan.doom_loop"
    PLAN_VALIDATION_FAILED = "plan.validation_failed"

    # ── Memory ────────────────────────────────────────────────────────────────
    MEMORY_INJECTION_BLOCKED = "memory.injection_blocked"
    MEMORY_WRITE_CONFLICT = "memory.write_conflict"
    MEMORY_COMPACTION_FAILED = "memory.compaction_failed"
    MEMORY_NOT_FOUND = "memory.not_found"

    # ── Delegation ────────────────────────────────────────────────────────────
    DELEGATION_DEPTH_EXCEEDED = "delegation.depth_exceeded"
    DELEGATION_TIMEOUT = "delegation.timeout"
    DELEGATION_FAILED = "delegation.failed"

    # ── System / infrastructure ────────────────────────────────────────────────
    SYSTEM_PARSE_FAILURE = "system.parse_failure"
    SYSTEM_VALIDATION_ERROR = "system.validation_error"
    UNKNOWN = "system.unknown"

    # ── Legacy aliases — kept so existing callers that import the old names
    #    continue to work without modification.
    RATE_LIMIT = "llm.rate_limited"
    CONTEXT_OVERFLOW = "llm.context_overflow"
    TOOL_FAILURE = "tool.execution_failed"
    PARSE_FAILURE = "system.parse_failure"
    PERMISSION_DENIED = "tool.permission_denied"
    PROVIDER_UNAVAILABLE = "llm.connection_failed"
    VALIDATION_ERROR = "system.validation_error"
    BUDGET_EXCEEDED = "llm.budget_exceeded"
    MAX_TURNS_REACHED = "llm.max_turns_reached"
    DOOM_LOOP = "plan.doom_loop"
    CANCELLED = "llm.cancelled"
    TIMEOUT = "tool.timeout"
    NOT_FOUND = "tool.not_found"


# ---------------------------------------------------------------------------
# Exception → ErrorCode classifier
# ---------------------------------------------------------------------------

# Patterns matched against the *lower-cased* string representation of the
# exception.  First match wins; order from most-specific to least-specific.
_MESSAGE_PATTERNS: list[tuple[re.Pattern[str], ErrorCode]] = [
    # LLM / provider signals
    (re.compile(r"rate.limit|429|too many requests"), ErrorCode.LLM_RATE_LIMITED),
    (
        re.compile(
            r"context.{0,20}(overflow|length|window|limit|exceed|too long)"
            r"|prompt.{0,20}too long|too long.{0,20}(model|context|prompt)"
        ),
        ErrorCode.LLM_CONTEXT_OVERFLOW,
    ),
    (re.compile(r"exceeds the available context"), ErrorCode.LLM_CONTEXT_OVERFLOW),
    (
        re.compile(
            r"connection.{0,20}(refused|reset|error|failed|abort|closed)|"
            r"(503|502|504|500).{0,30}provider"
        ),
        ErrorCode.LLM_CONNECTION_FAILED,
    ),
    (
        re.compile(r"budget.{0,20}exceed|max.{0,10}tokens"),
        ErrorCode.LLM_BUDGET_EXCEEDED,
    ),
    (re.compile(r"max.{0,5}turns"), ErrorCode.LLM_MAX_TURNS_REACHED),
    (re.compile(r"cancel(led|ed)"), ErrorCode.LLM_CANCELLED),
    # Tool / execution signals
    (
        re.compile(r"permission denied|access denied|not permitted"),
        ErrorCode.TOOL_PERMISSION_DENIED,
    ),
    (
        re.compile(r"ssrf|private.{0,10}address|blocked.*host"),
        ErrorCode.TOOL_SSRF_BLOCKED,
    ),
    (
        re.compile(r"workspace.*escape|outside.*workdir|path.*traversal"),
        ErrorCode.TOOL_WORKSPACE_ESCAPE,
    ),
    (
        re.compile(r"sandbox.{0,20}(block|violation|deny|restrict)"),
        ErrorCode.TOOL_SANDBOX_BLOCKED,
    ),
    (re.compile(r"tool.{0,10}not found|no such tool"), ErrorCode.TOOL_NOT_FOUND),
    (re.compile(r"timed? out|timeout"), ErrorCode.TOOL_TIMEOUT),
    # Planning signals
    (
        re.compile(r"doom.{0,5}loop|infinite.{0,5}loop|repeated tool"),
        ErrorCode.PLAN_DOOM_LOOP,
    ),
    (
        re.compile(r"delegation.{0,10}depth|max.{0,10}delegation"),
        ErrorCode.DELEGATION_DEPTH_EXCEEDED,
    ),
    (re.compile(r"plan.{0,10}(invalid|fail|error)"), ErrorCode.PLAN_INVALID),
    # Memory signals
    (
        re.compile(r"injection.{0,10}(blocked|detected|denied)"),
        ErrorCode.MEMORY_INJECTION_BLOCKED,
    ),
    (re.compile(r"memory.{0,10}(conflict|collision)"), ErrorCode.MEMORY_WRITE_CONFLICT),
]

_EXCEPTION_TYPE_MAP: list[tuple[type, ErrorCode]] = [
    # Standard library / network
    (TimeoutError, ErrorCode.TOOL_TIMEOUT),
    (PermissionError, ErrorCode.TOOL_PERMISSION_DENIED),
    (FileNotFoundError, ErrorCode.TOOL_NOT_FOUND),
    (ConnectionRefusedError, ErrorCode.LLM_CONNECTION_FAILED),
    (ConnectionResetError, ErrorCode.LLM_CONNECTION_FAILED),
    (ConnectionAbortedError, ErrorCode.LLM_CONNECTION_FAILED),
    (ConnectionError, ErrorCode.LLM_CONNECTION_FAILED),
    (ValueError, ErrorCode.SYSTEM_VALIDATION_ERROR),
    (TypeError, ErrorCode.SYSTEM_VALIDATION_ERROR),
]


def classify_exception(exc: BaseException) -> ErrorCode:
    """Map any Python exception to the most specific ``ErrorCode``.

    Matching order:
    1. Already an ``AgentError`` → return its own code.
    2. Message-pattern scan (most specific first).
    3. Exception-type map.
    4. Fall back to ``UNKNOWN``.

    This function never raises.
    """
    if isinstance(exc, AgentError):
        return exc.code

    try:
        msg = str(exc).lower()
    except Exception:
        return ErrorCode.UNKNOWN

    for pattern, code in _MESSAGE_PATTERNS:
        if pattern.search(msg):
            return code

    for exc_type, code in _EXCEPTION_TYPE_MAP:
        if isinstance(exc, exc_type):
            return code

    return ErrorCode.UNKNOWN


def error_code_label(code: ErrorCode) -> str:
    """Return a short human-readable label for display in the TUI or logs.

    Examples::

        error_code_label(ErrorCode.LLM_RATE_LIMITED)   → "Rate limited"
        error_code_label(ErrorCode.TOOL_TIMEOUT)        → "Tool timed out"
    """
    _LABELS: dict[str, str] = {
        ErrorCode.TOOL_PERMISSION_DENIED: "Permission denied",
        ErrorCode.TOOL_TIMEOUT: "Tool timed out",
        ErrorCode.TOOL_SANDBOX_BLOCKED: "Sandbox blocked",
        ErrorCode.TOOL_WORKSPACE_ESCAPE: "Path outside workspace",
        ErrorCode.TOOL_SSRF_BLOCKED: "SSRF blocked",
        ErrorCode.TOOL_NOT_FOUND: "Tool not found",
        ErrorCode.TOOL_INVALID_ARGS: "Invalid tool arguments",
        ErrorCode.TOOL_EXECUTION_FAILED: "Tool execution failed",
        ErrorCode.LLM_CONTEXT_OVERFLOW: "Context window full",
        ErrorCode.LLM_RATE_LIMITED: "Rate limited — retrying",
        ErrorCode.LLM_CONNECTION_FAILED: "Provider unreachable",
        ErrorCode.LLM_INVALID_RESPONSE: "Invalid LLM response",
        ErrorCode.LLM_CANCELLED: "Cancelled",
        ErrorCode.LLM_BUDGET_EXCEEDED: "Token budget exceeded",
        ErrorCode.LLM_MAX_TURNS_REACHED: "Turn limit reached",
        ErrorCode.PLAN_INVALID: "Invalid plan",
        ErrorCode.PLAN_MAX_RETRIES: "Planning max retries",
        ErrorCode.PLAN_NO_TOOLS_MATCH: "No matching tools",
        ErrorCode.PLAN_DOOM_LOOP: "Loop detected",
        ErrorCode.PLAN_VALIDATION_FAILED: "Plan validation failed",
        ErrorCode.MEMORY_INJECTION_BLOCKED: "Memory injection blocked",
        ErrorCode.MEMORY_WRITE_CONFLICT: "Memory write conflict",
        ErrorCode.MEMORY_COMPACTION_FAILED: "Memory compaction failed",
        ErrorCode.MEMORY_NOT_FOUND: "Memory not found",
        ErrorCode.DELEGATION_DEPTH_EXCEEDED: "Delegation depth exceeded",
        ErrorCode.DELEGATION_TIMEOUT: "Delegation timed out",
        ErrorCode.DELEGATION_FAILED: "Delegation failed",
        ErrorCode.SYSTEM_PARSE_FAILURE: "Parse failure",
        ErrorCode.SYSTEM_VALIDATION_ERROR: "Validation error",
        ErrorCode.UNKNOWN: "Unknown error",
    }
    return _LABELS.get(code, code.value)


# ---------------------------------------------------------------------------
# AgentError dataclass
# ---------------------------------------------------------------------------


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
        ctx = {
            k: v
            for k, v in data.items()
            if k not in ("error_code", "message", "retryable")
        }
        return cls(
            code=code,
            message=str(data.get("message", "")),
            retryable=bool(data.get("retryable", True)),
            context=ctx,
        )
