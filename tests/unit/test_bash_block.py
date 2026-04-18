"""tests/unit/test_bash_block.py — TASK-TUI-3

Tests for tui/src/ui/components/bash_block.py — BashBlock widget logic.
Tests use the public Python API (output_lines, is_truncated, collapsed)
without launching a Textual app.
"""

from __future__ import annotations

import pytest

from tui.src.ui.components.bash_block import BashBlock, MAX_VISIBLE_LINES


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_bash_block_initial_state() -> None:
    block = BashBlock(description="Run tests", command="pytest -q")
    assert block._description == "Run tests"
    assert block._command == "pytest -q"
    assert block.output_lines == []
    assert block.collapsed is True


def test_bash_block_no_output_not_truncated() -> None:
    block = BashBlock()
    assert not block.is_truncated


# ---------------------------------------------------------------------------
# append_output
# ---------------------------------------------------------------------------


def test_append_output_accumulates_lines() -> None:
    block = BashBlock()
    block.append_output("line 1")
    block.append_output("line 2")
    assert block.output_lines == ["line 1", "line 2"]


def test_append_output_five_lines_no_truncation() -> None:
    block = BashBlock()
    for i in range(5):
        block.append_output(f"output line {i}")
    assert len(block.output_lines) == 5
    assert not block.is_truncated


def test_append_output_exceeds_limit_triggers_truncation() -> None:
    block = BashBlock()
    for i in range(MAX_VISIBLE_LINES + 10):
        block.append_output(f"line {i}")
    assert block.is_truncated  # collapsed=True by default


def test_append_output_exactly_at_limit_no_truncation() -> None:
    block = BashBlock()
    for i in range(MAX_VISIBLE_LINES):
        block.append_output(f"line {i}")
    assert not block.is_truncated


# ---------------------------------------------------------------------------
# set_output
# ---------------------------------------------------------------------------


def test_set_output_replaces_existing() -> None:
    block = BashBlock()
    block.append_output("old")
    block.set_output(["new1", "new2"])
    assert block.output_lines == ["new1", "new2"]


def test_set_output_50_lines_triggers_truncation() -> None:
    block = BashBlock()
    block.set_output([f"L{i}" for i in range(50)])
    assert block.is_truncated


# ---------------------------------------------------------------------------
# Toggle collapsed state
# ---------------------------------------------------------------------------


def test_collapse_toggle_removes_truncation() -> None:
    block = BashBlock()
    for i in range(MAX_VISIBLE_LINES + 5):
        block.append_output(f"line {i}")

    assert block.is_truncated  # default collapsed=True

    block.collapsed = False
    assert not block.is_truncated  # expanded: all lines visible


def test_collapse_toggle_back_reinstates_truncation() -> None:
    block = BashBlock()
    for i in range(MAX_VISIBLE_LINES + 5):
        block.append_output(f"line {i}")

    block.collapsed = False
    block.collapsed = True
    assert block.is_truncated


# ---------------------------------------------------------------------------
# is_truncated: exact boundary conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_lines,collapsed,expect_truncated", [
    (MAX_VISIBLE_LINES - 1, True,  False),
    (MAX_VISIBLE_LINES,     True,  False),
    (MAX_VISIBLE_LINES + 1, True,  True),
    (MAX_VISIBLE_LINES + 1, False, False),
    (100,                   True,  True),
    (100,                   False, False),
])
def test_is_truncated_boundaries(n_lines: int, collapsed: bool, expect_truncated: bool) -> None:
    block = BashBlock()
    block.set_output([f"L{i}" for i in range(n_lines)])
    block.collapsed = collapsed
    assert block.is_truncated == expect_truncated


# ---------------------------------------------------------------------------
# Output lines are preserved faithfully
# ---------------------------------------------------------------------------


def test_output_lines_preserves_whitespace() -> None:
    block = BashBlock()
    block.append_output("  indented output  ")
    assert block.output_lines[0] == "  indented output  "


def test_output_lines_copy_is_returned() -> None:
    block = BashBlock()
    block.append_output("x")
    lines = block.output_lines
    lines.append("mutate")
    # Mutation of returned list must not affect internal state
    assert len(block.output_lines) == 1
