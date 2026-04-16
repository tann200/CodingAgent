import importlib


def test_invalidate_cache_forwards_to_canonical(monkeypatch):
    ctl = importlib.import_module("src.config.toolsets.loader")
    called = {"ok": False}

    def fake_clear():
        called["ok"] = True

    monkeypatch.setattr(ctl, "clear_cache", fake_clear)
    shim = importlib.import_module("src.tools.toolsets.loader")
    shim.invalidate_cache()
    assert called["ok"] is True


def test_toolset_manager_delegates(monkeypatch):
    # Fake canonical ToolsetManager implementation and ensure shim delegates
    class FakeImpl:
        def __init__(self, base_tools=None):
            self.base_tools = base_tools or []
            self.selected = None

        def select_toolset(self, role):
            self.selected = role
            return ["t1", "t2"]

        def get_current_toolset(self):
            return "fake"

        def get_toolset_tools(self, name):
            return ["a", "b"]

    ctl = importlib.import_module("src.config.toolsets.loader")
    monkeypatch.setattr(ctl, "ToolsetManager", FakeImpl)
    shim = importlib.import_module("src.tools.toolsets.loader")
    tm = shim.ToolsetManager(base_tools=["x"])
    assert tm.select_toolset("role") == ["t1", "t2"]
    assert tm.get_current_toolset() == "fake"
    assert tm.get_toolset_tools("name") == ["a", "b"]
