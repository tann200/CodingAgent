"""tests/unit/test_permission_table.py — TASK-PERM-3

Tests for src.core.orchestration.permission_table.PermissionTable.
Verifies: add rule, check match, no-match, glob patterns, session vs project
scope, clear_session_rules, and thread-safety basics.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.core.orchestration.permission_table import PermissionTable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tbl(tmp_path: Path) -> PermissionTable:
    return PermissionTable(db_path=tmp_path / "perm.db")


# ---------------------------------------------------------------------------
# Basic add / check
# ---------------------------------------------------------------------------


def test_add_and_check_allow(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "src/**/*.py", "allow", "project")
    assert tbl.check("write", "src/foo/bar.py") == "allow"


def test_add_and_check_deny(tbl: PermissionTable) -> None:
    tbl.add_rule("bash", "*", "deny", "project")
    assert tbl.check("bash", "rm -rf /") == "deny"


def test_check_no_match_returns_none(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "src/**/*.py", "allow", "project")
    assert tbl.check("write", "tests/test_foo.py") is None


def test_check_unknown_kind_returns_none(tbl: PermissionTable) -> None:
    assert tbl.check("completely_unknown_tool", "anything") is None


# ---------------------------------------------------------------------------
# Glob pattern matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("src/foo/bar.py",        "allow"),   # matches src/**/*.py
    ("src/bar.py",            None),      # ** requires at least one segment between
    ("tests/test_bar.py",     None),      # wrong prefix
    ("src/a/b/c/d.py",        "allow"),   # deep nesting
])
def test_glob_pattern_src_star_star_star_py(tbl: PermissionTable, path: str, expected) -> None:
    tbl.add_rule("write", "src/**/*.py", "allow", "project")
    assert tbl.check("write", path) == expected


def test_glob_star_matches_everything(tbl: PermissionTable) -> None:
    tbl.add_rule("webfetch", "*", "allow", "project")
    assert tbl.check("webfetch", "https://example.com/api") == "allow"
    assert tbl.check("webfetch", "anything") == "allow"


def test_glob_exact_match(tbl: PermissionTable) -> None:
    tbl.add_rule("bash", "pytest", "allow", "project")
    assert tbl.check("bash", "pytest") == "allow"
    assert tbl.check("bash", "pytest -q") is None


# ---------------------------------------------------------------------------
# Newest-first / last-added-wins semantics
# ---------------------------------------------------------------------------


def test_later_rule_overrides_earlier(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "*", "deny", "project")
    tbl.add_rule("write", "src/**", "allow", "project")
    # src/ path should get "allow" (later rule wins, newest-first iteration)
    assert tbl.check("write", "src/foo.py") == "allow"


def test_earlier_deny_not_overridden_by_later_for_different_pattern(
    tbl: PermissionTable,
) -> None:
    tbl.add_rule("write", "tests/**", "deny", "project")
    tbl.add_rule("write", "src/**", "allow", "project")
    assert tbl.check("write", "tests/foo.py") == "deny"


# ---------------------------------------------------------------------------
# Session vs project scope
# ---------------------------------------------------------------------------


def test_session_rules_cleared_by_clear_session_rules(tbl: PermissionTable) -> None:
    tbl.add_rule("bash", "*", "allow", "session")
    assert tbl.check("bash", "ls") == "allow"

    cleared = tbl.clear_session_rules()
    assert cleared == 1
    assert tbl.check("bash", "ls") is None


def test_project_rules_survive_clear_session_rules(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "*", "allow", "project")
    tbl.add_rule("bash", "*", "deny", "session")

    tbl.clear_session_rules()
    assert tbl.check("write", "anything") == "allow"


def test_clear_session_rules_count(tbl: PermissionTable) -> None:
    tbl.add_rule("bash", "*", "allow", "session")
    tbl.add_rule("write", "*", "allow", "session")
    tbl.add_rule("read", "*", "allow", "project")

    count = tbl.clear_session_rules()
    assert count == 2


def test_clear_session_rules_noop_when_none(tbl: PermissionTable) -> None:
    count = tbl.clear_session_rules()
    assert count == 0


# ---------------------------------------------------------------------------
# list_rules
# ---------------------------------------------------------------------------


def test_list_rules_all(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "*.py", "allow", "project")
    tbl.add_rule("bash", "*", "deny", "session")
    rules = tbl.list_rules()
    assert len(rules) == 2
    kinds = {r["tool_kind"] for r in rules}
    assert kinds == {"write", "bash"}


def test_list_rules_filtered_by_kind(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "*.py", "allow", "project")
    tbl.add_rule("bash",  "*",    "deny",  "project")
    rules = tbl.list_rules("write")
    assert len(rules) == 1
    assert rules[0]["tool_kind"] == "write"


def test_list_rules_empty(tbl: PermissionTable) -> None:
    assert tbl.list_rules() == []


def test_list_rules_contains_expected_keys(tbl: PermissionTable) -> None:
    tbl.add_rule("bash", "*", "allow", "project")
    rule = tbl.list_rules()[0]
    for key in ("id", "tool_kind", "pattern", "action", "scope", "created_at"):
        assert key in rule


# ---------------------------------------------------------------------------
# delete_rule
# ---------------------------------------------------------------------------


def test_delete_rule_removes_entry(tbl: PermissionTable) -> None:
    rule_id = tbl.add_rule("write", "*", "allow", "project")
    deleted = tbl.delete_rule(rule_id)
    assert deleted is True
    assert tbl.check("write", "foo.py") is None


def test_delete_rule_unknown_id_returns_false(tbl: PermissionTable) -> None:
    assert tbl.delete_rule(99999) is False


# ---------------------------------------------------------------------------
# clear_all (used in tests)
# ---------------------------------------------------------------------------


def test_clear_all(tbl: PermissionTable) -> None:
    tbl.add_rule("write", "*", "allow", "project")
    tbl.add_rule("bash", "*", "deny", "project")
    tbl.clear_all()
    assert tbl.list_rules() == []


# ---------------------------------------------------------------------------
# Validation: invalid action/scope raises
# ---------------------------------------------------------------------------


def test_invalid_action_raises(tbl: PermissionTable) -> None:
    with pytest.raises(ValueError, match="action"):
        tbl.add_rule("write", "*", "grant", "project")


def test_invalid_scope_raises(tbl: PermissionTable) -> None:
    with pytest.raises(ValueError, match="scope"):
        tbl.add_rule("write", "*", "allow", "workspace")


# ---------------------------------------------------------------------------
# Thread-safety: 10 threads add rules concurrently
# ---------------------------------------------------------------------------


def test_concurrent_adds(tbl: PermissionTable) -> None:
    errors: list[str] = []

    def _add(i: int) -> None:
        try:
            tbl.add_rule("write", f"src/t{i}.py", "allow", "project")
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=_add, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(tbl.list_rules()) == 10


# ---------------------------------------------------------------------------
# Persistence: rules survive re-opening the DB
# ---------------------------------------------------------------------------


def test_project_rules_survive_restart(tmp_path: Path) -> None:
    db = tmp_path / "perm.db"
    tbl1 = PermissionTable(db_path=db)
    tbl1.add_rule("write", "*.py", "allow", "project")

    # Open a second instance pointing at the same DB
    tbl2 = PermissionTable(db_path=db)
    assert tbl2.check("write", "foo.py") == "allow"
