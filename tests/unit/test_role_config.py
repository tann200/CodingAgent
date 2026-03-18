import pytest
from src.core.orchestration.role_config import (
    RoleManager,
    get_role_config,
    is_tool_allowed_for_role,
    list_roles,
    get_allowed_tools,
    get_denied_tools,
)


def test_list_roles():
    roles = list_roles()
    # canonical roles per gap-analysis
    assert "strategic" in roles
    assert "operational" in roles
    assert "reviewer" in roles
    assert "analyst" in roles


def test_get_role_config():
    planner_config = get_role_config("planner")
    assert planner_config is not None
    assert "description" in planner_config
    assert "allowed_tools" in planner_config
    assert "denied_tools" in planner_config


def test_planner_role_blocks_writes():
    assert not is_tool_allowed_for_role("write_file", "planner")
    assert not is_tool_allowed_for_role("edit_file", "planner")
    assert is_tool_allowed_for_role("read_file", "planner")
    assert is_tool_allowed_for_role("search_code", "planner")


def test_coder_role_allows_writes():
    # legacy name 'coder' should map to canonical 'operational'
    assert is_tool_allowed_for_role("write_file", "coder")
    assert is_tool_allowed_for_role("edit_file", "coder")
    assert is_tool_allowed_for_role("read_file", "coder")


def test_reviewer_role_denies_writes():
    assert not is_tool_allowed_for_role("write_file", "reviewer")
    assert not is_tool_allowed_for_role("edit_file", "reviewer")
    assert is_tool_allowed_for_role("run_tests", "reviewer")
    assert is_tool_allowed_for_role("run_linter", "reviewer")


def test_researcher_role_allows_search():
    # legacy 'researcher' maps to 'analyst'
    assert is_tool_allowed_for_role("search_code", "researcher")
    assert is_tool_allowed_for_role("find_symbol", "researcher")
    assert not is_tool_allowed_for_role("write_file", "researcher")
    assert not is_tool_allowed_for_role("run_tests", "researcher")


def test_get_allowed_tools():
    planner_tools = get_allowed_tools("planner")
    assert "read_file" in planner_tools
    assert "search_code" in planner_tools
    assert "write_file" not in planner_tools


def test_get_denied_tools():
    planner_denied = get_denied_tools("planner")
    assert "write_file" in planner_denied
    assert "edit_file" in planner_denied


def test_role_manager_set_role():
    rm = RoleManager()
    assert rm.set_role("planner")
    # normalized to canonical
    assert rm.get_current_role() == "strategic"


def test_role_manager_invalid_role():
    rm = RoleManager()
    assert not rm.set_role("invalid_role")
    assert rm.get_current_role() is None


def test_role_manager_tool_check():
    rm = RoleManager()
    rm.set_role("planner")
    assert rm.is_tool_allowed("read_file")
    assert not rm.is_tool_allowed("write_file")


def test_role_manager_get_config():
    rm = RoleManager()
    rm.set_role("coder")
    config = rm.get_role_config()
    assert config is not None
    assert (
        config["description"]
        == "Implements code changes based on plans from the planner."
    )
