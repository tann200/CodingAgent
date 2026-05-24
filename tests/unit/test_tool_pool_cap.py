"""Tests for ToolRegistry plugin cap (P2-6)."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.tools._registry import ToolRegistry, _DEFAULT_MAX_PLUGIN_TOOLS


def _make_fn(name: str):
    def fn(**kwargs):
        return {"ok": True}
    fn.__name__ = name
    fn.__doc__ = f"{name} tool"
    return fn


# ---------------------------------------------------------------------------
# Basic cap enforcement
# ---------------------------------------------------------------------------

class TestPluginCap:
    def test_default_cap_is_set(self):
        reg = ToolRegistry()
        assert reg.max_plugin_tools == _DEFAULT_MAX_PLUGIN_TOOLS

    def test_explicit_cap(self):
        reg = ToolRegistry(max_plugin_tools=3)
        assert reg.max_plugin_tools == 3

    def test_zero_cap_means_unlimited(self):
        reg = ToolRegistry(max_plugin_tools=0)
        for i in range(100):
            reg.register(f"plug_{i}", _make_fn(f"plug_{i}"), origin="plugin")
        assert reg.plugin_count == 100

    def test_builtins_not_counted_against_cap(self):
        reg = ToolRegistry(max_plugin_tools=2)
        for i in range(20):
            reg.register(f"builtin_{i}", _make_fn(f"builtin_{i}"), origin="builtin")
        assert reg.plugin_count == 0

    def test_plugin_count_increments(self):
        reg = ToolRegistry(max_plugin_tools=10)
        reg.register("p1", _make_fn("p1"), origin="plugin")
        reg.register("p2", _make_fn("p2"), origin="plugin")
        assert reg.plugin_count == 2

    def test_cap_raises_runtime_error(self):
        reg = ToolRegistry(max_plugin_tools=2)
        reg.register("p1", _make_fn("p1"), origin="plugin")
        reg.register("p2", _make_fn("p2"), origin="plugin")
        with pytest.raises(RuntimeError, match="ToolPool cap reached"):
            reg.register("p3", _make_fn("p3"), origin="plugin")

    def test_cap_error_mentions_tool_name(self):
        reg = ToolRegistry(max_plugin_tools=1)
        reg.register("p1", _make_fn("p1"), origin="plugin")
        with pytest.raises(RuntimeError, match="p2"):
            reg.register("p2", _make_fn("p2"), origin="plugin")

    def test_cap_error_mentions_env_var(self):
        reg = ToolRegistry(max_plugin_tools=1)
        reg.register("p1", _make_fn("p1"), origin="plugin")
        with pytest.raises(RuntimeError, match="TOOL_POOL_MAX_PLUGINS"):
            reg.register("p2", _make_fn("p2"), origin="plugin")

    def test_re_registering_same_plugin_does_not_increment(self):
        reg = ToolRegistry(max_plugin_tools=3)
        fn = _make_fn("p1")
        reg.register("p1", fn, origin="plugin")
        reg.register("p1", fn, origin="plugin")  # re-register same name
        assert reg.plugin_count == 1

    def test_cap_not_exceeded_on_re_register(self):
        reg = ToolRegistry(max_plugin_tools=1)
        fn = _make_fn("p1")
        reg.register("p1", fn, origin="plugin")
        # re-registering same name should NOT raise
        reg.register("p1", _make_fn("p1"), origin="plugin")
        assert reg.plugin_count == 1


# ---------------------------------------------------------------------------
# Cap via environment variable
# ---------------------------------------------------------------------------

class TestPluginCapEnv:
    def test_env_var_sets_cap(self, monkeypatch):
        monkeypatch.setenv("TOOL_POOL_MAX_PLUGINS", "7")
        reg = ToolRegistry()
        assert reg.max_plugin_tools == 7

    def test_invalid_env_var_uses_default(self, monkeypatch):
        monkeypatch.setenv("TOOL_POOL_MAX_PLUGINS", "not_a_number")
        reg = ToolRegistry()
        assert reg.max_plugin_tools == _DEFAULT_MAX_PLUGIN_TOOLS

    def test_env_var_zero_is_unlimited(self, monkeypatch):
        monkeypatch.setenv("TOOL_POOL_MAX_PLUGINS", "0")
        reg = ToolRegistry()
        assert reg.max_plugin_tools == 0


# ---------------------------------------------------------------------------
# Interaction with discover()
# ---------------------------------------------------------------------------

class TestCapWithDiscover:
    def test_discover_stops_at_cap(self):
        """discover() catches ValueError from register_definition; cap RuntimeError
        should propagate out of discover() so callers are aware."""
        reg = ToolRegistry(max_plugin_tools=0)  # unlimited — just count

        class FakeModule:
            pass

        from src.tools._tool import TOOL_ATTR, ToolDefinition

        # Create 3 fake @tool-decorated callables
        for i in range(3):
            fn = _make_fn(f"disc_{i}")

            def _fn(**kwargs):
                return {}

            _fn.__name__ = f"disc_{i}"
            defn = ToolDefinition(
                name=f"disc_{i}",
                fn=_fn,
                description=f"disc {i}",
                side_effects=[],
                tags=[],
            )
            setattr(_fn, TOOL_ATTR, defn)
            setattr(FakeModule, f"disc_{i}", _fn)

        count = reg.discover(FakeModule, origin="plugin")
        assert count == 3
        assert reg.plugin_count == 3

    def test_discover_raises_when_cap_hit(self):
        reg = ToolRegistry(max_plugin_tools=1)

        class FakeModule:
            pass

        from src.tools._tool import TOOL_ATTR, ToolDefinition

        for i in range(2):
            fn = _make_fn(f"d_{i}")
            defn = ToolDefinition(name=f"d_{i}", fn=fn, description="", side_effects=[], tags=[])
            setattr(fn, TOOL_ATTR, defn)
            setattr(FakeModule, f"d_{i}", fn)

        # discover() logs ValueError but propagates RuntimeError (cap)
        with pytest.raises(RuntimeError, match="ToolPool cap reached"):
            reg.discover(FakeModule, origin="plugin")


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_plugin_count_property(self):
        reg = ToolRegistry(max_plugin_tools=10)
        assert reg.plugin_count == 0
        reg.register("x", _make_fn("x"), origin="plugin")
        assert reg.plugin_count == 1

    def test_max_plugin_tools_property(self):
        reg = ToolRegistry(max_plugin_tools=42)
        assert reg.max_plugin_tools == 42

    def test_negative_cap_treated_as_zero(self):
        reg = ToolRegistry(max_plugin_tools=-5)
        assert reg.max_plugin_tools == 0
