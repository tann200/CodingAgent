"""
tests/unit/test_truncate.py — Unit tests for TOOLS-01: _truncate.py

Covers:
- Empty / within-limit text passes through unchanged.
- Line overflow triggers truncation at MAX_LINES.
- Byte overflow triggers truncation at MAX_BYTES.
- Correct hint appended: delegate hint when agent_context is None or
  allowed_tools is None or includes 'delegate_task'; search hint otherwise.
- truncate_dict_values: recurse into dicts and lists; scalars pass through.
- Constants exported at expected values.
"""


# ruff: noqa: E501
from __future__ import annotations

import pytest

from src.tools._truncate import (
    MAX_BYTES,
    MAX_LINES,
    Truncate,
    _HINT_DELEGATE,
    _HINT_SEARCH,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _Agent:
    """Minimal AgentDefinition-compatible fake."""

    def __init__(self, allowed_tools):
        self.allowed_tools = allowed_tools


def _big_line_text(n_lines: int) -> str:
    return "\n".join(f"line {i}" for i in range(n_lines))


def _big_byte_text(n_bytes: int) -> str:
    # Each 'x' is 1 byte in UTF-8; add a newline so line count stays low.
    return "x" * n_bytes


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_lines(self):
        assert MAX_LINES == 2_000

    def test_max_bytes(self):
        assert MAX_BYTES == 100 * 1024


# ---------------------------------------------------------------------------
# output() — within limits
# ---------------------------------------------------------------------------


class TestOutputWithinLimits:
    def test_empty_string_returned_unchanged(self):
        assert Truncate.output("") == ""

    def test_none_like_empty_string(self):
        # empty string is falsy — returned as-is
        assert Truncate.output("") == ""

    def test_short_text_unchanged(self):
        text = "hello world"
        assert Truncate.output(text) == text

    def test_exactly_max_lines_unchanged(self):
        text = _big_line_text(MAX_LINES)
        result = Truncate.output(text)
        assert result == text

    def test_exactly_max_bytes_unchanged(self):
        text = _big_byte_text(MAX_BYTES)
        result = Truncate.output(text)
        assert result == text


# ---------------------------------------------------------------------------
# output() — line overflow
# ---------------------------------------------------------------------------


class TestOutputLineOverflow:
    def test_line_overflow_truncated(self):
        text = _big_line_text(MAX_LINES + 100)
        result = Truncate.output(text)
        # The truncated content is the first MAX_LINES lines joined (with keepends).
        # The hint is appended after those lines; just verify result is shorter than input.
        assert len(result.splitlines()) < len(text.splitlines())
        # And the original first line is present
        assert "line 0" in result

    def test_line_overflow_appends_hint(self):
        text = _big_line_text(MAX_LINES + 1)
        result = Truncate.output(text)
        assert "[Output truncated" in result

    def test_line_overflow_delegate_hint_when_no_context(self):
        text = _big_line_text(MAX_LINES + 1)
        result = Truncate.output(text, agent_context=None)
        assert "delegate" in result.lower() or "delegate_task" in result

    def test_line_overflow_search_hint_when_restricted_agent(self):
        agent = _Agent(allowed_tools={"read_file", "grep"})  # no delegate_task
        text = _big_line_text(MAX_LINES + 1)
        result = Truncate.output(text, agent_context=agent)
        assert "Grep" in result or "offset" in result.lower()

    def test_line_overflow_delegate_hint_when_unrestricted_agent(self):
        agent = _Agent(allowed_tools=None)  # None means unrestricted
        text = _big_line_text(MAX_LINES + 1)
        result = Truncate.output(text, agent_context=agent)
        assert "delegate" in result.lower() or "delegate_task" in result

    def test_line_overflow_delegate_hint_when_delegate_task_allowed(self):
        agent = _Agent(allowed_tools={"delegate_task", "read_file"})
        text = _big_line_text(MAX_LINES + 1)
        result = Truncate.output(text, agent_context=agent)
        assert "delegate" in result.lower() or "delegate_task" in result


# ---------------------------------------------------------------------------
# output() — byte overflow
# ---------------------------------------------------------------------------


class TestOutputByteOverflow:
    def test_byte_overflow_truncated(self):
        text = _big_byte_text(MAX_BYTES + 1000)
        result = Truncate.output(text)
        # Result should be shorter than original
        assert len(result.encode("utf-8")) < len(text.encode("utf-8"))

    def test_byte_overflow_appends_hint(self):
        text = _big_byte_text(MAX_BYTES + 1)
        result = Truncate.output(text)
        assert "[Output truncated" in result

    def test_byte_overflow_search_hint_when_restricted_agent(self):
        agent = _Agent(allowed_tools={"bash"})
        text = _big_byte_text(MAX_BYTES + 1)
        result = Truncate.output(text, agent_context=agent)
        assert "Grep" in result or "offset" in result.lower()

    def test_byte_overflow_multibyte_characters_safe(self):
        """Truncation at byte boundary should not produce invalid UTF-8."""
        # Use 2-byte chars (é = U+00E9) to create a text slightly over MAX_BYTES
        char = "\u00e9"  # 2 bytes in UTF-8
        count = (MAX_BYTES // 2) + 50
        text = char * count
        result = Truncate.output(text)
        # Result should be decodable (no UnicodeDecodeError)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _choose_hint
# ---------------------------------------------------------------------------


class TestChooseHint:
    def test_none_context_returns_delegate(self):
        assert Truncate._choose_hint(None) == _HINT_DELEGATE

    def test_unrestricted_allowed_tools_returns_delegate(self):
        agent = _Agent(allowed_tools=None)
        assert Truncate._choose_hint(agent) == _HINT_DELEGATE

    def test_delegate_task_in_allowed_returns_delegate(self):
        agent = _Agent(allowed_tools={"delegate_task", "read_file"})
        assert Truncate._choose_hint(agent) == _HINT_DELEGATE

    def test_no_delegate_task_in_allowed_returns_search(self):
        agent = _Agent(allowed_tools={"read_file", "grep"})
        assert Truncate._choose_hint(agent) == _HINT_SEARCH

    def test_empty_allowed_tools_returns_search(self):
        agent = _Agent(allowed_tools=set())
        assert Truncate._choose_hint(agent) == _HINT_SEARCH

    def test_non_iterable_allowed_tools_returns_search(self):
        """If allowed_tools raises TypeError on 'in', fall back to search hint."""
        agent = _Agent(allowed_tools=42)  # int, not iterable
        assert Truncate._choose_hint(agent) == _HINT_SEARCH


# ---------------------------------------------------------------------------
# truncate_dict_values
# ---------------------------------------------------------------------------


class TestTruncateDictValues:
    def test_scalar_int_unchanged(self):
        assert Truncate.truncate_dict_values(42) == 42

    def test_scalar_none_unchanged(self):
        assert Truncate.truncate_dict_values(None) is None

    def test_short_string_unchanged(self):
        assert Truncate.truncate_dict_values("hello") == "hello"

    def test_long_string_truncated(self):
        big = _big_line_text(MAX_LINES + 10)
        result = Truncate.truncate_dict_values(big)
        assert isinstance(result, str)
        assert "[Output truncated" in result

    def test_dict_values_truncated_recursively(self):
        from typing import cast, Any

        big = _big_line_text(MAX_LINES + 10)
        data = {"key": big, "nested": {"inner": big}}
        result = cast(Any, Truncate.truncate_dict_values(data))
        assert "[Output truncated" in result["key"]
        assert "[Output truncated" in result["nested"]["inner"]

    def test_list_items_truncated_recursively(self):
        from typing import cast, Any

        big = _big_line_text(MAX_LINES + 10)
        data = [big, "short", {"k": big}]
        result = cast(Any, Truncate.truncate_dict_values(data))
        assert "[Output truncated" in result[0]
        assert result[1] == "short"
        assert "[Output truncated" in result[2]["k"]

    def test_dict_keys_preserved(self):
        from typing import cast, Any

        data = {"alpha": "short", "beta": "also short"}
        result = cast(Any, Truncate.truncate_dict_values(data))
        assert set(result.keys()) == {"alpha", "beta"}
