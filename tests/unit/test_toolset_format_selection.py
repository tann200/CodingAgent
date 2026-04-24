import json
import yaml
from pathlib import Path


from src.config.toolsets import loader


def write_file(path: Path, data: dict, fmt: str = "yaml"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "yaml":
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)


def test_load_toolset_for_model_prefers_yaml_for_small(tmp_path, monkeypatch):
    tools_dir = tmp_path / "toolsets"
    yaml_path = tools_dir / "foo.yaml"
    json_path = tools_dir / "foo.json"
    write_file(yaml_path, {"name": "foo", "tools": ["a"]}, fmt="yaml")
    write_file(json_path, {"name": "foo", "tools": ["b"]}, fmt="json")

    monkeypatch.setattr(loader, "_DIR", tools_dir)

    # small-model heuristics should pick YAML
    ts = loader.load_toolset_for_model("foo", model="gpt-3.5-mini")
    assert ts is not None
    assert ts.get("tools") == ["a"]


def test_load_toolset_for_model_prefers_json_for_big(tmp_path, monkeypatch):
    tools_dir = tmp_path / "toolsets"
    yaml_path = tools_dir / "bar.yaml"
    json_path = tools_dir / "bar.json"
    write_file(yaml_path, {"name": "bar", "tools": ["a"]}, fmt="yaml")
    write_file(json_path, {"name": "bar", "tools": ["b"]}, fmt="json")

    monkeypatch.setattr(loader, "_DIR", tools_dir)

    # big-model heuristics should pick JSON
    ts = loader.load_toolset_for_model("bar", model="gpt-4o-mini")
    assert ts is not None
    assert ts.get("tools") == ["b"]


def test_load_toolset_falls_back_when_preferred_missing(tmp_path, monkeypatch):
    tools_dir = tmp_path / "toolsets"
    json_path = tools_dir / "baz.json"
    write_file(json_path, {"name": "baz", "tools": ["jb"]}, fmt="json")

    monkeypatch.setattr(loader, "_DIR", tools_dir)

    # YAML missing, JSON present
    ts = loader.load_toolset_for_model("baz", model="gpt-3.5-mini")
    assert ts is not None
    assert ts.get("tools") == ["jb"]
