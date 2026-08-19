"""Tests for P3-5: OTel optional extra and span wiring on secondary graph nodes."""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# pyproject.toml declares [otel] optional extra
# ---------------------------------------------------------------------------

class TestOtelExtra:
    def _load_pyproject(self):
        import tomllib
        import pathlib
        path = pathlib.Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(path, "rb") as f:
            return tomllib.load(f)

    def test_otel_extra_exists(self):
        data = self._load_pyproject()
        extras = data.get("project", {}).get("optional-dependencies", {})
        assert "otel" in extras, "Missing [otel] optional-dependency group in pyproject.toml"

    def test_otel_extra_includes_api(self):
        data = self._load_pyproject()
        otel_deps = data["project"]["optional-dependencies"]["otel"]
        assert any("opentelemetry-api" in d for d in otel_deps)

    def test_otel_extra_includes_sdk(self):
        data = self._load_pyproject()
        otel_deps = data["project"]["optional-dependencies"]["otel"]
        assert any("opentelemetry-sdk" in d for d in otel_deps)

    def test_otel_extra_includes_grpc_exporter(self):
        data = self._load_pyproject()
        otel_deps = data["project"]["optional-dependencies"]["otel"]
        assert any("otlp-proto-grpc" in d for d in otel_deps)

    def test_otel_extra_includes_http_exporter(self):
        data = self._load_pyproject()
        otel_deps = data["project"]["optional-dependencies"]["otel"]
        assert any("otlp-proto-http" in d for d in otel_deps)

    def test_otel_packages_have_version_pins(self):
        data = self._load_pyproject()
        otel_deps = data["project"]["optional-dependencies"]["otel"]
        for dep in otel_deps:
            assert ">=" in dep, f"Missing version pin in otel dep: {dep!r}"


# ---------------------------------------------------------------------------
# tracer.py: no-op when OTel is not installed
# ---------------------------------------------------------------------------

class TestTracerNoop:
    def test_span_node_noop_when_no_otel(self):
        """span_node must be a no-op context manager when OTel is unavailable."""
        from src.core.telemetry.tracer import span_node
        # Force the tracer into the uninitialised state so we don't depend on
        # whether opentelemetry is installed in the test environment.
        import src.core.telemetry.tracer as _mod
        orig_tracer = _mod._tracer
        orig_init = _mod._initialised
        _mod._tracer = None
        _mod._initialised = True
        try:
            with span_node("test_node", {"k": "v"}) as sp:
                pass  # must not raise
        finally:
            _mod._tracer = orig_tracer
            _mod._initialised = orig_init

    def test_record_event_noop_with_none_span(self):
        from src.core.telemetry.tracer import record_event
        record_event(None, "test.event", {"k": "v"})  # must not raise

    def test_wire_event_bus_noop_when_no_tracer(self):
        """wire_event_bus should not raise even when tracer is None."""
        from src.core.telemetry.tracer import wire_event_bus
        bus = MagicMock()
        import src.core.telemetry.tracer as _mod
        orig_tracer = _mod._tracer
        _mod._tracer = None
        _mod._initialised = True
        try:
            wire_event_bus(bus)
        finally:
            _mod._tracer = orig_tracer


# ---------------------------------------------------------------------------
# node_utils.span_node — delegates to OTel or falls back to nullcontext
# ---------------------------------------------------------------------------

class TestNodeUtilsSpanNode:
    def test_returns_nullcontext_when_no_otel(self):
        import contextlib
        from src.core.orchestration.graph.nodes import node_utils
        orig = node_utils._otel_span_node
        orig_has = node_utils._HAS_TRACER
        node_utils._otel_span_node = None
        node_utils._HAS_TRACER = False
        try:
            ctx = node_utils.span_node("x")
            assert isinstance(ctx, contextlib._GeneratorContextManager) or hasattr(ctx, "__enter__")
        finally:
            node_utils._otel_span_node = orig
            node_utils._HAS_TRACER = orig_has

    def test_usable_as_context_manager(self):
        from src.core.orchestration.graph.nodes.node_utils import span_node
        with span_node("test") as sp:
            pass  # must not raise


# ---------------------------------------------------------------------------
# Secondary nodes are wrapped with span_node
# ---------------------------------------------------------------------------

def _make_state(**kwargs):
    base = {"rounds": 0, "current_step": 0, "task": "test", "working_dir": "/tmp"}
    base.update(kwargs)
    return base


class TestSecondaryNodeSpans:
    """Verify span_node is called when each secondary node runs."""

    def _make_fake_span(self, calls):
        import contextlib

        @contextlib.contextmanager
        def fake_span(name, attrs=None):
            calls.append(name)
            yield None

        return fake_span

    def test_evaluation_node_calls_span(self):
        from src.core.orchestration.graph.nodes import evaluation_node as _mod
        calls = []

        async def fake_impl(s, c):
            return {"ok": True}

        with patch.object(_mod, "_span_node", self._make_fake_span(calls)), \
             patch.object(_mod, "_evaluation_node_impl", fake_impl):
            asyncio.run(_mod.evaluation_node(_make_state(), {}))

        assert "evaluation" in calls

    def test_replan_node_calls_span(self):
        from src.core.orchestration.graph.nodes import replan_node as _mod
        calls = []

        async def fake_impl(s, c):
            return {"ok": True}

        with patch.object(_mod, "_span_node", self._make_fake_span(calls)), \
             patch.object(_mod, "_replan_node_impl", fake_impl):
            asyncio.run(_mod.replan_node(_make_state(), {}))

        assert "replan" in calls

    def test_verification_node_calls_span(self):
        from src.core.orchestration.graph.nodes import verification_node as _mod
        calls = []

        async def fake_impl(s, c):
            return {"verification_result": {}}

        with patch.object(_mod, "_span_node", self._make_fake_span(calls)), \
             patch.object(_mod, "_verification_node_impl", fake_impl):
            asyncio.run(_mod.verification_node(_make_state(), {}))

        assert "verification" in calls

    def test_memory_update_node_calls_span(self):
        from src.core.orchestration.graph.nodes import memory_update_node as _mod
        calls = []

        async def fake_impl(s, c):
            return {}

        with patch.object(_mod, "_span_node", self._make_fake_span(calls)), \
             patch.object(_mod, "_memory_update_node_impl", fake_impl):
            asyncio.run(_mod.memory_update_node(_make_state(), {}))

        assert "memory_update" in calls

    def test_impl_functions_exist(self):
        """All _impl functions must exist and be coroutine functions."""
        import inspect
        from src.core.orchestration.graph.nodes.evaluation_node import _evaluation_node_impl
        from src.core.orchestration.graph.nodes.replan_node import _replan_node_impl
        from src.core.orchestration.graph.nodes.verification_node import _verification_node_impl
        from src.core.orchestration.graph.nodes.memory_update_node import _memory_update_node_impl
        for fn in [_evaluation_node_impl, _replan_node_impl, _verification_node_impl, _memory_update_node_impl]:
            assert inspect.iscoroutinefunction(fn), f"{fn.__name__} is not async"
