"""Structured tool result type for uniform error handling across all tools.

Replaces ad-hoc ``{"status": "error", "error": "..."}`` dicts with a typed,
immutable ``ToolResult`` dataclass. Tools should return ``ToolResult`` objects;
callers receive the normalised dict via ``to_dict()``.

Usage::

    from src.tools._result import ToolResult, ErrorCode

    # Success
    return ToolResult.success({"content": file_text})

    # Failure with specific error code
    return ToolResult.failure("File not found: foo.py", ErrorCode.NOT_FOUND)

    # Convert to dict for the model/orchestrator
    result.to_dict()
    # → {"ok": True, "output": ..., "error": "", "error_code": "none"}
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Canonical error codes for tool failures.

    Using ``str`` as a mixin keeps values JSON-serialisable without extra
    conversion and makes them comparable to plain strings in legacy code.
    """

    NOT_FOUND = "not_found"
    PERMISSION = "permission_denied"
    VALIDATION = "validation_error"
    TIMEOUT = "timeout"
    PROVIDER = "provider_error"
    CANCELLED = "cancelled"
    RATE_LIMITED = "rate_limited"
    NO_OP = "no_op"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolResult:
    """Immutable tool execution result.

    Attributes:
        ok:         ``True`` on success, ``False`` on any failure.
        output:     Tool-specific payload on success; ``None`` on failure.
        error:      Human-readable error message; empty string on success.
        error_code: Structured error code; ``ErrorCode.UNKNOWN`` by default.
    """

    ok: bool
    output: Any = None
    error: str = ""
    error_code: ErrorCode = ErrorCode.UNKNOWN

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def success(cls, output: Any = None) -> "ToolResult":
        """Create a successful result with the given output payload."""
        return cls(ok=True, output=output, error="", error_code=ErrorCode.UNKNOWN)

    @classmethod
    def failure(cls, error: str, code: ErrorCode = ErrorCode.UNKNOWN) -> "ToolResult":
        """Create a failure result with an error message and optional code."""
        return cls(ok=False, output=None, error=error, error_code=code)

    @classmethod
    def no_op(cls, message: str = "no-op: content unchanged") -> "ToolResult":
        """Create a no-op success (content already matches; write skipped)."""
        return cls(
            ok=True, output={"message": message}, error="", error_code=ErrorCode.NO_OP
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict suitable for JSON serialisation and model consumption.

        The envelope is::

            {
                "ok": True | False,
                "output": <payload>,   # always present; None on failure
                "error": "",           # always present; "" on success
                "error_code": "none"   # always present; ErrorCode value string
            }
        """
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "error_code": self.error_code.value,
        }

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def __bool__(self) -> bool:
        """Allow ``if result:`` as shorthand for ``if result.ok:``."""
        return self.ok
