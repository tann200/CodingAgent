import pytest
import tempfile
import os
from pathlib import Path
from src.tools.toolsets.loader import (
    load_toolset,
    get_tools_for_toolset,
    get_toolset_for_role,
    list_available_toolsets,
    ToolsetManager,
)


def test_list_available_toolsets():
    toolsets = list_available_toolsets()
    assert "coding" in toolsets
    assert "debug" in toolsets
    assert "review" in toolsets
    assert "planning" in toolsets


def test_get_tools_for_toolset():
    coding_tools = get_tools_for_toolset("coding")
    assert "read_file" in coding_tools
    assert "write_file" in coding_tools
    assert "edit_file" in coding_tools


def test_get_toolset_for_role():
    assert get_toolset_for_role("coder") == "coding"
    assert get_toolset_for_role("planner") == "planning"
    assert get_toolset_for_role("reviewer") == "review"
    assert get_toolset_for_role("debugger") == "debug"


def test_toolset_manager():
    manager = ToolsetManager(base_tools=["base_tool"])

    coding_tools = manager.select_toolset("coder")
    assert "read_file" in coding_tools
    assert manager.get_current_toolset() == "coding"

    planning_tools = manager.select_toolset("planner")
    assert "read_file" in planning_tools
    assert manager.get_current_toolset() == "planning"


def test_load_toolset():
    toolset = load_toolset("coding")
    assert toolset is not None
    assert toolset["name"] == "coding"
    assert "tools" in toolset


def test_load_nonexistent_toolset():
    toolset = load_toolset("nonexistent")
    assert toolset is None
