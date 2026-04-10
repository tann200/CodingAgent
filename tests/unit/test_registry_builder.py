"""Phase B tests: ToolRegistry and example_registry extracted to dedicated modules."""

from __future__ import annotations


class TestToolRegistryModule:
    """ToolRegistry is importable from its new home and from orchestrator (re-export)."""

    def test_importable_from_tool_registry(self):
        from src.core.orchestration.tool_registry import ToolRegistry

        assert ToolRegistry is not None

    def test_importable_from_orchestrator(self):
        from src.core.orchestration.orchestrator import ToolRegistry

        assert ToolRegistry is not None

    def test_same_class(self):
        from src.core.orchestration.tool_registry import ToolRegistry as TR1
        from src.core.orchestration.orchestrator import ToolRegistry as TR2

        assert TR1 is TR2

    def test_register_and_get(self):
        from src.core.orchestration.tool_registry import ToolRegistry

        reg = ToolRegistry()

        def my_tool(x: int) -> dict:
            return {"x": x}

        reg.register("my_tool", my_tool, description="my_tool(x) -> echo x")
        meta = reg.get("my_tool")
        assert meta is not None
        assert meta["fn"] is my_tool

    def test_list_returns_names(self):
        from src.core.orchestration.tool_registry import ToolRegistry

        reg = ToolRegistry()
        reg.register("a", lambda: None, description="a")
        reg.register("b", lambda: None, description="b")
        assert set(reg.list()) == {"a", "b"}

    def test_filter_by_names(self):
        from src.core.orchestration.tool_registry import ToolRegistry

        reg = ToolRegistry()
        reg.register("keep", lambda: None, description="keep")
        reg.register("drop", lambda: None, description="drop")
        filtered = reg.filter_by_names(["keep"])
        assert "keep" in filtered.list()
        assert "drop" not in filtered.list()

    def test_get_openai_functions_returns_list(self):
        from src.core.orchestration.tool_registry import ToolRegistry

        reg = ToolRegistry()
        reg.register("my_fn", lambda x: x, description="my_fn(x: str) -> str")
        fns = reg.get_openai_functions()
        assert isinstance(fns, list)
        assert len(fns) == 1
        assert fns[0]["type"] == "function"
        assert fns[0]["function"]["name"] == "my_fn"


class TestRegistryBuilder:
    """example_registry() is importable from its new home and from orchestrator."""

    def test_importable_from_registry_builder(self):
        from src.core.orchestration.registry_builder import example_registry

        assert callable(example_registry)

    def test_importable_from_orchestrator(self):
        from src.core.orchestration.orchestrator import example_registry

        assert callable(example_registry)

    def test_same_function(self):
        from src.core.orchestration.registry_builder import example_registry as f1
        from src.core.orchestration.orchestrator import example_registry as f2

        assert f1 is f2

    def test_returns_tool_registry(self):
        from src.core.orchestration.registry_builder import example_registry
        from src.core.orchestration.tool_registry import ToolRegistry

        reg = example_registry()
        assert isinstance(reg, ToolRegistry)

    def test_contains_core_tools(self):
        from src.core.orchestration.registry_builder import example_registry

        reg = example_registry()
        tools = set(reg.list())
        for expected in ("read_file", "write_file", "edit_file", "bash", "glob"):
            assert expected in tools, f"{expected!r} missing from example_registry()"
