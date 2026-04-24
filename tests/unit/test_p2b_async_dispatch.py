"""P2-B: Async tool dispatch tests.

Verifies that _dispatch_tool() uses asyncio.to_thread for MEDIUM/LARGE/FRONTIER
and calls synchronously for NANO/SMALL.
"""
import threading
from unittest.mock import MagicMock

import pytest

from src.core.orchestration.graph.nodes.execution_node import (
    _dispatch_tool,
    _ASYNC_TOOL_TIERS,
)


class TestDispatchToolTierRouting:
    """_dispatch_tool uses asyncio.to_thread for MEDIUM+ only."""

    @pytest.mark.asyncio
    async def test_medium_uses_thread(self):
        """MEDIUM tier dispatches via asyncio.to_thread (runs in a worker thread)."""
        caller_thread = threading.current_thread().ident
        dispatch_thread: list = []

        def fake_execute_tool(action):
            dispatch_thread.append(threading.current_thread().ident)
            return {"ok": True, "output": "done"}

        orch = MagicMock()
        orch.execute_tool.side_effect = fake_execute_tool

        result = await _dispatch_tool(orch, {"name": "bash", "arguments": {}}, "medium")

        assert result == {"ok": True, "output": "done"}
        assert dispatch_thread, "execute_tool was not called"
        # The tool ran in a worker thread (different from the event loop thread)
        assert dispatch_thread[0] != caller_thread, (
            "MEDIUM tier should dispatch in a worker thread, not the event loop thread"
        )

    @pytest.mark.asyncio
    async def test_large_uses_thread(self):
        """LARGE tier also uses asyncio.to_thread."""
        caller_thread = threading.current_thread().ident
        dispatch_thread: list = []

        def fake_execute_tool(action):
            dispatch_thread.append(threading.current_thread().ident)
            return {"ok": True}

        orch = MagicMock()
        orch.execute_tool.side_effect = fake_execute_tool

        await _dispatch_tool(orch, {"name": "write_file", "arguments": {}}, "large")

        assert dispatch_thread[0] != caller_thread, (
            "LARGE tier should dispatch in a worker thread"
        )

    @pytest.mark.asyncio
    async def test_frontier_uses_thread(self):
        """FRONTIER tier also uses asyncio.to_thread."""
        caller_thread = threading.current_thread().ident
        dispatch_thread: list = []

        def fake_execute_tool(action):
            dispatch_thread.append(threading.current_thread().ident)
            return {"ok": True}

        orch = MagicMock()
        orch.execute_tool.side_effect = fake_execute_tool

        await _dispatch_tool(orch, {"name": "bash", "arguments": {}}, "frontier")

        assert dispatch_thread[0] != caller_thread

    @pytest.mark.asyncio
    async def test_nano_calls_synchronously(self):
        """NANO tier calls execute_tool synchronously (same thread as event loop)."""
        caller_thread = threading.current_thread().ident
        dispatch_thread: list = []

        def fake_execute_tool(action):
            dispatch_thread.append(threading.current_thread().ident)
            return {"ok": True}

        orch = MagicMock()
        orch.execute_tool.side_effect = fake_execute_tool

        await _dispatch_tool(orch, {"name": "read_file", "arguments": {}}, "nano")

        assert dispatch_thread, "execute_tool was not called"
        assert dispatch_thread[0] == caller_thread, (
            "NANO tier should call execute_tool in the same thread (synchronous)"
        )

    @pytest.mark.asyncio
    async def test_small_calls_synchronously(self):
        """SMALL tier also calls synchronously."""
        caller_thread = threading.current_thread().ident
        dispatch_thread: list = []

        def fake_execute_tool(action):
            dispatch_thread.append(threading.current_thread().ident)
            return {"ok": True}

        orch = MagicMock()
        orch.execute_tool.side_effect = fake_execute_tool

        await _dispatch_tool(orch, {"name": "grep", "arguments": {}}, "small")

        assert dispatch_thread[0] == caller_thread, (
            "SMALL tier should call execute_tool synchronously"
        )

    @pytest.mark.asyncio
    async def test_empty_tier_calls_synchronously(self):
        """Unknown/empty tier falls back to synchronous dispatch."""
        caller_thread = threading.current_thread().ident
        dispatch_thread: list = []

        def fake_execute_tool(action):
            dispatch_thread.append(threading.current_thread().ident)
            return {"ok": True}

        orch = MagicMock()
        orch.execute_tool.side_effect = fake_execute_tool

        await _dispatch_tool(orch, {"name": "read_file", "arguments": {}}, "")

        assert dispatch_thread[0] == caller_thread

    def test_async_tool_tiers_constant(self):
        """Confirm the expected tiers are in the async set."""
        assert "medium" in _ASYNC_TOOL_TIERS
        assert "large" in _ASYNC_TOOL_TIERS
        assert "frontier" in _ASYNC_TOOL_TIERS
        assert "nano" not in _ASYNC_TOOL_TIERS
        assert "small" not in _ASYNC_TOOL_TIERS

    @pytest.mark.asyncio
    async def test_result_propagated_correctly(self):
        """Result dict from execute_tool is returned unchanged."""
        expected = {"ok": True, "output": "hello", "status": "ok"}
        orch = MagicMock()
        orch.execute_tool.return_value = expected

        result = await _dispatch_tool(orch, {"name": "bash", "arguments": {}}, "medium")

        assert result == expected
