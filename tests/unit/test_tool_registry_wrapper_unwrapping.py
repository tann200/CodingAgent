import functools
import types
from src.tools._registry import ToolRegistry
from src.tools._tool import tool, PermissionKind


def test_tool_registry_discovers_wrapped_tools():
    mod = types.ModuleType("mock_tool_module")

    @tool(side_effects=["write"], permission_kind=PermissionKind.WRITE_FILE)
    def my_sample_tool(arg: str) -> str:
        """A sample test tool."""
        return f"result: {arg}"

    def custom_wrapper(fn):
        # Wrapper that forgets @functools.wraps
        def _wrapped(*args, **kwargs):
            return fn(*args, **kwargs)
        _wrapped.__wrapped__ = fn
        return _wrapped

    wrapped_fn = custom_wrapper(my_sample_tool)
    setattr(mod, "my_sample_tool", wrapped_fn)

    registry = ToolRegistry()
    discovered_count = registry.discover(mod)
    assert discovered_count == 1
    assert "my_sample_tool" in registry.list()

    perm = registry.get_permission_kind("my_sample_tool")
    assert perm == PermissionKind.WRITE_FILE
