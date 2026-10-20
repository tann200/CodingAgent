from pathlib import Path

# ruff: noqa: E501
import yaml

from src.config.toolsets.loader import (
    load_toolset,
    get_tools_for_toolset,
    get_toolset_for_role,
    list_available_toolsets,
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


def test_resolve_toolset_success():
    coding_tools = loader.get_tools_for_toolset("coding")
    resolved = loader.resolve_toolset("coding", coding_tools, fail_closed=True)
    assert resolved == coding_tools


def test_resolve_toolset_fail_closed_when_missing():
    import pytest

    # If any configured tool in the toolset is missing, it must fail closed
    with pytest.raises(KeyError, match="absent from registry"):
        loader.resolve_toolset("coding", ["read_file", "write_file"], fail_closed=True)


def test_resolve_toolset_non_fail_closed_returns_subset():
    resolved = loader.resolve_toolset("coding", ["read_file", "write_file"], fail_closed=False)
    assert resolved == ["read_file", "write_file"]


def test_resolve_nonexistent_toolset_fails_closed():
    import pytest

    with pytest.raises(KeyError, match="not found or contains no tools"):
        loader.resolve_toolset("nonexistent_toolset_xyz", ["read_file"], fail_closed=True)


def test_validate_all_toolsets_against_registry():
    from src.tools._registry import build_registry

    reg = build_registry(validate_toolsets=True)
    errors = loader.validate_all_toolsets(reg, fail_closed=True)
    assert errors == {}
