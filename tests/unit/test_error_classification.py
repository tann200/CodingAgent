"""tests/unit/test_error_classification.py — TASK-REL-1

Tests for the expanded ErrorCode taxonomy and classify_exception() utility.
Verifies that every major error category maps to the correct code and that
legacy aliases still resolve without breaking existing callers.
"""

from __future__ import annotations

import pytest
from src.core.errors import (
    AgentError,
    ErrorCode,
    classify_exception,
    error_code_label,
)


# ---------------------------------------------------------------------------
# classify_exception — message-pattern tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message, expected",
    [
        # LLM / rate-limit variants
        ("Provider returned 429 Too Many Requests", ErrorCode.LLM_RATE_LIMITED),
        ("rate limit exceeded", ErrorCode.LLM_RATE_LIMITED),
        ("RateLimitError: too many requests", ErrorCode.LLM_RATE_LIMITED),
        # Context overflow variants
        ("Context length exceeded", ErrorCode.LLM_CONTEXT_OVERFLOW),
        ("context window is full", ErrorCode.LLM_CONTEXT_OVERFLOW),
        ("exceeds the available context", ErrorCode.LLM_CONTEXT_OVERFLOW),
        ("prompt is too long for this model", ErrorCode.LLM_CONTEXT_OVERFLOW),
        # Connection failures
        ("Connection refused to localhost:11434", ErrorCode.LLM_CONNECTION_FAILED),
        ("ConnectionResetError: [Errno 104]", ErrorCode.LLM_CONNECTION_FAILED),
        # Permission denied
        ("Permission denied: /etc/passwd", ErrorCode.TOOL_PERMISSION_DENIED),
        ("access denied for path /secret", ErrorCode.TOOL_PERMISSION_DENIED),
        # SSRF
        ("SSRF blocked: 169.254.169.254", ErrorCode.TOOL_SSRF_BLOCKED),
        ("private address is blocked", ErrorCode.TOOL_SSRF_BLOCKED),
        # Workspace escape
        ("path traversal outside workdir", ErrorCode.TOOL_WORKSPACE_ESCAPE),
        ("workspace escape attempt detected", ErrorCode.TOOL_WORKSPACE_ESCAPE),
        # Sandbox
        ("Sandbox block: command denied by policy", ErrorCode.TOOL_SANDBOX_BLOCKED),
        ("sandbox violation: rm -rf /", ErrorCode.TOOL_SANDBOX_BLOCKED),
        # Tool not found
        ("tool not found: my_custom_tool", ErrorCode.TOOL_NOT_FOUND),
        ("no such tool: frobulate", ErrorCode.TOOL_NOT_FOUND),
        # Timeout
        ("timed out after 30s", ErrorCode.TOOL_TIMEOUT),
        ("TimeoutError: request timeout", ErrorCode.TOOL_TIMEOUT),
        # Delegation depth
        ("delegation depth limit 3 reached", ErrorCode.DELEGATION_DEPTH_EXCEEDED),
        ("max delegation level exceeded", ErrorCode.DELEGATION_DEPTH_EXCEEDED),
        # Doom loop
        ("doom loop detected after 5 identical calls", ErrorCode.PLAN_DOOM_LOOP),
        ("infinite loop prevention triggered", ErrorCode.PLAN_DOOM_LOOP),
        # Memory injection
        ("injection blocked: prompt exfiltration", ErrorCode.MEMORY_INJECTION_BLOCKED),
        ("injection detected in memory content", ErrorCode.MEMORY_INJECTION_BLOCKED),
    ],
)
def test_classify_by_message(message: str, expected: ErrorCode) -> None:
    exc = RuntimeError(message)
    assert classify_exception(exc) == expected


# ---------------------------------------------------------------------------
# classify_exception — exception-type tests
# ---------------------------------------------------------------------------


def test_classify_permission_error() -> None:
    assert classify_exception(PermissionError("denied")) == ErrorCode.TOOL_PERMISSION_DENIED


def test_classify_timeout_error() -> None:
    assert classify_exception(TimeoutError("took too long")) == ErrorCode.TOOL_TIMEOUT


def test_classify_file_not_found() -> None:
    assert classify_exception(FileNotFoundError("no such file")) == ErrorCode.TOOL_NOT_FOUND


def test_classify_connection_refused() -> None:
    assert classify_exception(ConnectionRefusedError()) == ErrorCode.LLM_CONNECTION_FAILED


def test_classify_connection_reset() -> None:
    assert classify_exception(ConnectionResetError()) == ErrorCode.LLM_CONNECTION_FAILED


def test_classify_value_error() -> None:
    assert classify_exception(ValueError("bad value")) == ErrorCode.SYSTEM_VALIDATION_ERROR


def test_classify_type_error() -> None:
    assert classify_exception(TypeError("wrong type")) == ErrorCode.SYSTEM_VALIDATION_ERROR


def test_classify_generic_exception_returns_unknown() -> None:
    assert classify_exception(Exception("something went wrong")) == ErrorCode.UNKNOWN


def test_classify_agent_error_returns_own_code() -> None:
    err = AgentError(ErrorCode.MEMORY_COMPACTION_FAILED, "compaction failed")
    assert classify_exception(err) == ErrorCode.MEMORY_COMPACTION_FAILED


# ---------------------------------------------------------------------------
# classify_exception — robustness
# ---------------------------------------------------------------------------


def test_classify_exception_with_no_str() -> None:
    """classify_exception must not raise even for pathological exceptions."""

    class WeirdException(Exception):
        def __str__(self):
            raise RuntimeError("str() itself raises!")

    result = classify_exception(WeirdException())
    assert result == ErrorCode.UNKNOWN


# ---------------------------------------------------------------------------
# AgentError — backward-compat legacy aliases
# ---------------------------------------------------------------------------


def test_legacy_rate_limit_alias() -> None:
    err = AgentError(ErrorCode.RATE_LIMIT, "429")
    assert err.code == ErrorCode.LLM_RATE_LIMITED
    assert err.to_dict()["error_code"] == "llm.rate_limited"


def test_legacy_permission_denied_alias() -> None:
    err = AgentError(ErrorCode.PERMISSION_DENIED, "denied")
    assert err.code == ErrorCode.TOOL_PERMISSION_DENIED


def test_legacy_tool_failure_alias() -> None:
    err = AgentError(ErrorCode.TOOL_FAILURE, "failed")
    assert err.code == ErrorCode.TOOL_EXECUTION_FAILED


def test_legacy_context_overflow_alias() -> None:
    err = AgentError(ErrorCode.CONTEXT_OVERFLOW, "too long")
    assert err.code == ErrorCode.LLM_CONTEXT_OVERFLOW


def test_legacy_provider_unavailable_alias() -> None:
    err = AgentError(ErrorCode.PROVIDER_UNAVAILABLE, "down")
    assert err.code == ErrorCode.LLM_CONNECTION_FAILED


# ---------------------------------------------------------------------------
# AgentError — serialisation round-trip
# ---------------------------------------------------------------------------


def test_to_dict_includes_error_code() -> None:
    err = AgentError(
        ErrorCode.LLM_RATE_LIMITED,
        "429",
        retryable=True,
        context={"provider": "lmstudio"},
    )
    d = err.to_dict()
    assert d["error_code"] == "llm.rate_limited"
    assert d["message"] == "429"
    assert d["retryable"] is True
    assert d["provider"] == "lmstudio"


def test_from_dict_round_trip() -> None:
    original = AgentError(ErrorCode.PLAN_DOOM_LOOP, "loop detected", retryable=False)
    restored = AgentError.from_dict(original.to_dict())
    assert restored.code == original.code
    assert restored.message == original.message
    assert restored.retryable == original.retryable


def test_from_dict_unknown_code_falls_back() -> None:
    d = {"error_code": "nonexistent.code", "message": "something"}
    err = AgentError.from_dict(d)
    assert err.code == ErrorCode.UNKNOWN


# ---------------------------------------------------------------------------
# error_code_label — human-readable labels
# ---------------------------------------------------------------------------


def test_error_code_label_rate_limited() -> None:
    assert error_code_label(ErrorCode.LLM_RATE_LIMITED) == "Rate limited — retrying"


def test_error_code_label_context_overflow() -> None:
    assert error_code_label(ErrorCode.LLM_CONTEXT_OVERFLOW) == "Context window full"


def test_error_code_label_tool_timeout() -> None:
    assert error_code_label(ErrorCode.TOOL_TIMEOUT) == "Tool timed out"


def test_error_code_label_unknown() -> None:
    assert error_code_label(ErrorCode.UNKNOWN) == "Unknown error"


def test_error_code_label_covers_all_codes() -> None:
    """Every canonical (non-alias) ErrorCode should have a non-empty label."""
    # Aliases share values with canonical codes so they resolve to the same label.
    seen_values: set[str] = set()
    for member in ErrorCode:
        val = member.value
        if val in seen_values:
            continue  # skip aliases
        seen_values.add(val)
        label = error_code_label(member)
        assert label, f"ErrorCode.{member.name} has no label"
