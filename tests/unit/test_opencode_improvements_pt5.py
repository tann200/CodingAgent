"""
Regression tests for tool registration, toolset wiring, and skills system:

  REG-1  — web_search registered in example_registry
  REG-2  — read_web_page registered in example_registry
  REG-3  — ask_user registered in example_registry
  REG-4  — batch registered in example_registry
  REG-5  — multiedit registered in example_registry
  REG-6  — load_skill / list_skills registered in example_registry
  TS-1   — coding toolset includes multiedit, edit_file_atomic, batch
  TS-2   — analysis toolset includes batch, web_search, read_web_page
  TS-3   — planning toolset includes batch, web_search, ask_user
  TS-4   — debug toolset includes edit_file_atomic, multiedit
  TS-5   — review toolset includes batch, git_log, git_diff
  SK-1   — list_skills returns the 5 built-in skills
  SK-2   — load_skill loads debug_checklist
  SK-3   — explore_codebase skill exists
  CACHE-1 — clear_cache() resets the toolset loader cache
"""


# ruff: noqa: E501
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# REG — tool registration in example_registry
# ---------------------------------------------------------------------------

def _registry():
    from src.core.orchestration.orchestrator import example_registry
    return example_registry()


@pytest.mark.parametrize("tool_name", [
    "web_search",
    "read_web_page",
    "ask_user",
    "batch",
    "multiedit",
    "load_skill",
    "list_skills",
    "submit_plan_for_review",
])
def test_tool_registered(tool_name):
    """All new tools must appear in example_registry()."""
    reg = _registry()
    assert reg.get(tool_name) is not None, f"Tool '{tool_name}' not registered"


# ---------------------------------------------------------------------------
# TS — toolset contents
# ---------------------------------------------------------------------------

def _toolset(name: str):
    from src.config.toolsets.loader import get_tools_for_toolset, clear_cache
    clear_cache()
    return set(get_tools_for_toolset(name))


def test_coding_toolset_has_new_write_tools():
    tools = _toolset("coding")
    assert "edit_file_atomic" in tools
    assert "multiedit" in tools
    assert "batch" in tools
    assert "web_search" in tools
    assert "ask_user" in tools
    assert "load_skill" in tools


def test_analysis_toolset_has_batch_and_web():
    tools = _toolset("analysis")
    assert "batch" in tools
    assert "web_search" in tools
    assert "read_web_page" in tools
    assert "load_skill" in tools
    # Must NOT contain write tools
    assert "write_file" not in tools
    assert "delete_file" not in tools
    assert "edit_file_atomic" not in tools


def test_planning_toolset_has_batch_and_web():
    tools = _toolset("planning")
    assert "batch" in tools
    assert "web_search" in tools
    assert "ask_user" in tools
    assert "load_skill" in tools


def test_debug_toolset_has_edit_tools():
    tools = _toolset("debug")
    assert "edit_file_atomic" in tools
    assert "multiedit" in tools
    assert "batch" in tools


def test_review_toolset_has_git_and_batch():
    tools = _toolset("review")
    assert "batch" in tools
    assert "git_log" in tools
    assert "git_diff" in tools
    assert "web_search" in tools


# ---------------------------------------------------------------------------
# SK — skills system
# ---------------------------------------------------------------------------

def test_list_skills_returns_builtins():
    """Built-in skills directory contains at least the 5 shipped skills."""
    from src.tools.skill_tools import list_skills
    result = list_skills()
    assert result["status"] == "ok"
    skills = set(result["skills"])
    expected = {"debug_checklist", "code_review", "write_tests", "refactor", "explore_codebase"}
    missing = expected - skills
    assert not missing, f"Missing built-in skills: {missing}"


def test_load_skill_debug_checklist():
    """load_skill('debug_checklist') returns the skill content."""
    from src.tools.skill_tools import load_skill
    result = load_skill("debug_checklist")
    assert result["status"] == "ok"
    assert "Debug Checklist" in result["content"]
    assert result["name"] == "debug_checklist"


def test_load_skill_explore_codebase():
    """explore_codebase skill mentions batch tool."""
    from src.tools.skill_tools import load_skill
    result = load_skill("explore_codebase")
    assert result["status"] == "ok"
    assert "batch" in result["content"]


def test_load_skill_path_traversal_still_rejected():
    """Confirm path traversal is still blocked after _SKILLS_DIR check was added."""
    from src.tools.skill_tools import load_skill
    result = load_skill("../../../etc/passwd")
    assert result["status"] == "error"
    assert "invalid" in result["error"].lower()


# ---------------------------------------------------------------------------
# CACHE — loader cache invalidation
# ---------------------------------------------------------------------------

def test_clear_cache_allows_reload():
    """clear_cache() removes entries so the next load re-reads the YAML."""
    from src.config.toolsets import loader
    loader.clear_cache()
    tools_before = loader.get_tools_for_toolset("coding")
    # Cache should be populated now
    assert loader._cache.get("coding") is not None
    # Clear and confirm evicted
    loader.clear_cache()
    assert loader._cache.get("coding") is None
    # Re-load should succeed
    tools_after = loader.get_tools_for_toolset("coding")
    assert tools_before == tools_after
