from pathlib import Path
import yaml

from src.config.toolsets.loader import (
    load_toolset,
    get_tools_for_toolset,
    get_toolset_for_role,
    list_available_toolsets,
    ToolsetManager,
)
from src.config.toolsets import loader


def write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


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


def test_load_toolset_and_get_tools_for_toolset(tmp_path, monkeypatch):
    # Prepare a temporary toolsets directory
    config_dir = tmp_path / "config_toolsets"
    coding = {"name": "coding", "description": "For coding", "tools": ["read_file", "write_file"]}
    write_yaml(config_dir / "coding.yaml", coding)

    monkeypatch.setattr(loader, "_DIR", config_dir)
    monkeypatch.setattr(loader, "_cache", {})  # clear cache so new _DIR is used

    ts = loader.load_toolset("coding")
    assert isinstance(ts, dict)
    assert ts.get("name") == "coding"

    tools = loader.get_tools_for_toolset("coding")
    assert "read_file" in tools
    assert "write_file" in tools


def test_load_toolset_nonexistent_returns_none(monkeypatch):
    monkeypatch.setattr(loader, "_DIR", Path("/nonexistent/path/for/tests"))
    monkeypatch.setattr(loader, "_cache", {})
    assert loader.load_toolset("does-not-exist") is None


def test_list_available_toolsets_lists_dir(tmp_path, monkeypatch):
    toolsets_dir = tmp_path / "toolsets"
    write_yaml(toolsets_dir / "a.yaml", {"tools": ["t1"]})
    write_yaml(toolsets_dir / "b.yaml", {"tools": ["t2"]})

    monkeypatch.setattr(loader, "_DIR", toolsets_dir)

    names = loader.list_available_toolsets()
    assert "a" in names
    assert "b" in names
