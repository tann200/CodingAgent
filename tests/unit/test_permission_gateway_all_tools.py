"""tests/unit/test_permission_gateway_all_tools.py — TASK-PERM-1

Tests for src.core.orchestration.permission_gateway:
  - _tool_kind_for_name: every mapped tool returns the right kind
  - _primary_arg_for_tool: extracts the correct arg from various tool call dicts
  - PermissionTable integration in _gate2c_permission_table:
      - "allow always" pre-approves matching calls without gate5
      - "deny always" blocks matching calls
      - no rule falls through to gate5 (returns None from gate2c)
  - Helper function coverage
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.orchestration.permission_gateway import (
    PermissionGateway,
    _primary_arg_for_tool,
    _tool_kind_for_name,
)
from src.core.orchestration.permission_table import PermissionTable


# ---------------------------------------------------------------------------
# _tool_kind_for_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name,expected_kind", [
    ("write_file",         "write"),
    ("edit_file",          "edit"),
    ("edit_by_line_range", "edit"),
    ("apply_patch",        "edit"),
    ("delete_file",        "write"),
    ("rename_file",        "write"),
    ("read_file",          "read"),
    ("list_dir",           "read"),
    ("glob_tool",          "glob"),
    ("grep_tool",          "grep"),
    ("bash",               "bash"),
    ("run_tests",          "bash"),
    ("run_bash",           "bash"),
    ("webfetch",           "webfetch"),
    ("websearch",          "websearch"),
    ("delegate_task",      "delegate_task"),
])
def test_tool_kind_mapping(tool_name: str, expected_kind: str) -> None:
    assert _tool_kind_for_name(tool_name) == expected_kind


def test_tool_kind_fallback_to_name() -> None:
    assert _tool_kind_for_name("custom_unknown_tool") == "custom_unknown_tool"


# ---------------------------------------------------------------------------
# _primary_arg_for_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("args,expected", [
    ({"path": "src/foo.py"},                      "src/foo.py"),
    ({"file_path": "tests/bar.py"},               "tests/bar.py"),
    ({"url": "https://example.com"},              "https://example.com"),
    ({"command": "pytest -q"},                    "pytest -q"),
    ({"bash_command": "echo hello"},              "echo hello"),
    ({"query": "search term"},                    "search term"),
    ({"subtask_description": "Write auth"},       "Write auth"),
    ({"pattern": "**/*.py"},                      "**/*.py"),
    ({},                                          ""),
])
def test_primary_arg_extraction(args: dict, expected: str) -> None:
    result = _primary_arg_for_tool("any_tool", args)
    assert result == expected


def test_primary_arg_truncates_long_value() -> None:
    long_val = "x" * 300
    result = _primary_arg_for_tool("bash", {"command": long_val})
    assert len(result) <= 256


def test_primary_arg_path_takes_priority_over_url() -> None:
    # "path" comes before "url" in the priority list
    result = _primary_arg_for_tool("write_file", {"path": "foo.py", "url": "https://x.com"})
    assert result == "foo.py"


# ---------------------------------------------------------------------------
# _gate2c_permission_table integration
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orch() -> MagicMock:
    orch = MagicMock()
    orch.plan_mode = None
    orch.explore_mode = False
    orch.working_dir = None
    orch._plan_mode_approved = True
    return orch


@pytest.fixture
def tbl(tmp_path: Path) -> PermissionTable:
    return PermissionTable(db_path=tmp_path / "perm.db")


def test_gate2c_allow_rule_pre_approves(
    mock_orch: MagicMock, tbl: PermissionTable
) -> None:
    tbl.add_rule("write", "src/**", "allow", "project")
    gw = PermissionGateway(mock_orch)

    with patch(
        "src.core.orchestration.permission_table.get_permission_table",
        return_value=tbl,
    ):
        result = gw._gate2c_permission_table("write_file", {"path": "src/core/foo.py"})

    assert result is not None
    assert result.allowed is True


def test_gate2c_deny_rule_blocks(
    mock_orch: MagicMock, tbl: PermissionTable
) -> None:
    tbl.add_rule("bash", "*", "deny", "project")
    gw = PermissionGateway(mock_orch)

    with patch(
        "src.core.orchestration.permission_table.get_permission_table",
        return_value=tbl,
    ):
        result = gw._gate2c_permission_table("bash", {"command": "rm -rf /"})

    assert result is not None
    assert result.allowed is False
    assert result.gate == 3


def test_gate2c_no_match_returns_none(
    mock_orch: MagicMock, tbl: PermissionTable
) -> None:
    """No matching rule → gate2c returns None, gate5 handles approval."""
    tbl.add_rule("write", "src/**", "allow", "project")
    gw = PermissionGateway(mock_orch)

    with patch(
        "src.core.orchestration.permission_table.get_permission_table",
        return_value=tbl,
    ):
        # "tests/" path doesn't match "src/**"
        result = gw._gate2c_permission_table("write_file", {"path": "tests/foo.py"})

    assert result is None


def test_gate2c_empty_table_returns_none(
    mock_orch: MagicMock, tbl: PermissionTable
) -> None:
    gw = PermissionGateway(mock_orch)

    with patch(
        "src.core.orchestration.permission_table.get_permission_table",
        return_value=tbl,
    ):
        result = gw._gate2c_permission_table("write_file", {"path": "foo.py"})

    assert result is None


def test_gate2c_exception_returns_none(mock_orch: MagicMock) -> None:
    """Exceptions in gate2c must never block tool execution — returns None."""
    gw = PermissionGateway(mock_orch)

    with patch(
        "src.core.orchestration.permission_table.get_permission_table",
        side_effect=RuntimeError("DB is locked"),
    ):
        result = gw._gate2c_permission_table("write_file", {"path": "foo.py"})

    assert result is None


# ---------------------------------------------------------------------------
# PermissionTable default policies (TASK-PERM-1 acceptance criteria)
# ---------------------------------------------------------------------------


def test_read_tools_have_no_default_deny_rule(tbl: PermissionTable) -> None:
    """Read tools should pass through when no rules are set (None = allow by default)."""
    assert tbl.check("read", "src/foo.py") is None


def test_write_tools_have_no_default_deny_rule(tbl: PermissionTable) -> None:
    """write tools have no built-in deny — gate3/5 handle prompting."""
    assert tbl.check("write", "src/foo.py") is None


def test_allow_always_bypasses_subsequent_calls(tbl: PermissionTable) -> None:
    """After an 'allow always' rule is added, repeated calls are pre-approved."""
    tbl.add_rule("write", "src/**/*.py", "allow", "project")
    for path in ["src/a.py", "src/core/b.py", "src/tools/c.py"]:
        assert tbl.check("write", path) == "allow"


def test_session_allowlist_cleared_on_session_end(tbl: PermissionTable) -> None:
    tbl.add_rule("delegate_task", "*", "allow", "session")
    assert tbl.check("delegate_task", "anything") == "allow"

    tbl.clear_session_rules()
    assert tbl.check("delegate_task", "anything") is None
