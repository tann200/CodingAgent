import json
import yaml
from pathlib import Path

from src.config.toolsets import loader as cfg_loader


def write_file(path: Path, data: dict, fmt: str = "yaml"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "yaml":
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)


def test_format_cache_isolated_by_dir(tmp_path, monkeypatch):
    # Create two separate toolset dirs with different content for the same name
    dir1 = tmp_path / "toolsets1"
    dir2 = tmp_path / "toolsets2"

    write_file(dir1 / "foo.yaml", {"name": "foo", "tools": ["a"]}, fmt="yaml")
    write_file(dir2 / "foo.json", {"name": "foo", "tools": ["b"]}, fmt="json")

    # Load from dir1 as small model (prefer yaml)
    monkeypatch.setattr(cfg_loader, "_DIR", dir1)
    cfg_loader.clear_cache()
    ts1 = cfg_loader.load_toolset_for_model("foo", model="gpt-3.5-mini")
    assert ts1 is not None
    assert ts1.get("tools") == ["a"]

    # Load from dir2 as big model (prefer json)
    monkeypatch.setattr(cfg_loader, "_DIR", dir2)
    cfg_loader.clear_cache()
    ts2 = cfg_loader.load_toolset_for_model("foo", model="gpt-4o")
    assert ts2 is not None
    assert ts2.get("tools") == ["b"]
