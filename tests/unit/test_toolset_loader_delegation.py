import json
from pathlib import Path


def test_legacy_loader_forwards_to_canonical(monkeypatch, tmp_path):
    # Create a dummy toolset file in a temporary dir and point the canonical
    # loader _DIR to it so the legacy shim delegates to the canonical loader.
    name = "sample_tools"
    tool_yaml = "tools:\n  - a\n  - b\n"

    td = tmp_path / "toolsets"
    td.mkdir()
    (td / f"{name}.yaml").write_text(tool_yaml, encoding="utf-8")

    # Monkeypatch the canonical loader's _DIR and clear caches
    import importlib

    ctl = importlib.import_module("src.config.toolsets.loader")
    monkeypatch.setattr(ctl, "_DIR", Path(td))
    ctl.clear_cache()

    # Now call the legacy shim which should delegate to the canonical loader
    shim = importlib.import_module("src.tools.toolsets.loader")
    ts = shim.load_toolset(name)
    assert ts is not None
    assert isinstance(ts, dict)
    assert "tools" in ts and ts["tools"] == ["a", "b"]


def test_format_cache_behavior_between_loaders(monkeypatch, tmp_path):
    # Prepare both YAML and JSON toolset files with different contents
    name = "multi_format"
    yaml_content = "tools:\n  - yaml_tool\n"
    json_content = {"tools": ["json_tool"]}

    td = tmp_path / "toolsets2"
    td.mkdir()
    (td / f"{name}.yaml").write_text(yaml_content, encoding="utf-8")
    (td / f"{name}.json").write_text(json.dumps(json_content), encoding="utf-8")

    import importlib

    ctl = importlib.import_module("src.config.toolsets.loader")
    monkeypatch.setattr(ctl, "_DIR", Path(td))
    ctl.clear_cache()

    # 1) load_toolset should pick YAML by default and populate both caches
    ts_yaml = ctl.load_toolset(name)
    assert ts_yaml is not None and ts_yaml.get("tools") == ["yaml_tool"]

    dir_key = str(ctl._DIR)
    # format cache should have YAML entry
    assert (name, "yaml", dir_key) in ctl._format_cache

    # 2) load_toolset_for_model with a big-model should prefer JSON
    ts_json = ctl.load_toolset_for_model(name, "gpt-4")
    assert ts_json is not None and ts_json.get("tools") == ["json_tool"]

    # format cache should now have JSON entry too
    assert (name, "json", dir_key) in ctl._format_cache

    # 3) Subsequent model-aware loads should return cached entries
    # small model -> yaml
    ts_yaml2 = ctl.load_toolset_for_model(name, "gpt-3.5-turbo")
    assert ts_yaml2 is not None and ts_yaml2.get("tools") == ["yaml_tool"]
