import types
from pathlib import Path

import pytest


def test_legacy_loader_delegates_to_config_loader(monkeypatch, tmp_path):
    # Make a fake canonical loader module with predictable behaviour
    fake_loader = types.ModuleType("src.config.toolsets.loader")

    def fake_load_toolset(name: str):
        return {"name": name, "tools": ["x"]}

    fake_loader.load_toolset = fake_load_toolset

    # Insert the fake module into sys.modules under the canonical path
    import sys

    monkeypatch.setitem(sys.modules, "src.config.toolsets.loader", fake_loader)

    # Now import the legacy loader and call load_toolset
    from src.tools.toolsets import loader as legacy_loader

    res = legacy_loader.load_toolset("anything")
    assert res is not None
    assert res.get("tools") == ["x"]
