import importlib
import types

from src.core.orchestration import task_lifecycle


def test_get_tools_for_role_uses_model_aware_loader(monkeypatch):
    # Create a fake toolset loader module with load_toolset_for_model
    fake_loader = types.SimpleNamespace()

    def get_toolset_for_role(role: str) -> str:
        return "fake"

    def load_toolset_for_model(name: str, model: str):
        # Return a toolset dict depending on the model string
        if model and "big" in model:
            return {"tools": ["big_tool"]}
        return {"tools": ["small_tool"]}

    def get_tools_for_toolset(name: str):
        return ["small_tool"]

    fake_loader.get_toolset_for_role = get_toolset_for_role
    fake_loader.load_toolset_for_model = load_toolset_for_model
    fake_loader.get_tools_for_toolset = get_tools_for_toolset

    # Monkeypatch importlib to return our fake module for the tools loader
    real_import_module = importlib.import_module

    def fake_import(name, package=None):
        if name == "src.config.toolsets.loader":
            return fake_loader
        return real_import_module(name, package=package)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    # Minimal orch with a tool registry that contains both tools
    class DummyReg:
        def __init__(self):
            self.tools = {
                "small_tool": {"description": "s"},
                "big_tool": {"description": "b"},
            }

    class Orch:
        def __init__(self, model):
            self._model = model
            self.tool_registry = DummyReg()

    # Small model -> small_tool
    orch_small = Orch(model="gpt-3.5-mini")
    res_small = task_lifecycle.get_tools_for_role_impl(orch_small, "whatever")
    names_small = [r["name"] for r in res_small]
    assert "small_tool" in names_small

    # Big model hint -> big_tool
    orch_big = Orch(model="gpt-4o-large")
    res_big = task_lifecycle.get_tools_for_role_impl(orch_big, "whatever")
    names_big = [r["name"] for r in res_big]
    assert "big_tool" in names_big
