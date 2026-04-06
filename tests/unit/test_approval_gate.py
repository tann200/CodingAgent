"""
tests/unit/test_approval_gate.py — Unit tests for approval_gate.py (S7-B).

Covers:
- register_bash_gate returns an AsyncGate (S7-B upgrade from threading.Event).
- resolve_bash_gate sets the gate and marks denied when approved=False.
- is_bash_denied returns True only after a deny resolution.
- register_tool_gate / resolve_tool_gate / is_tool_denied — same contract.
- Resolving a gate that was never registered is a no-op (no KeyError).
- Module-level state is isolated between tests via explicit cleanup.
- AsyncGate.wait() works synchronously (falls back when no loop).
- AsyncGate.wait_async() works in async context.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import src.core.orchestration.approval_gate as ag
from src.core.orchestration.approval_gate import AsyncGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cleanup():
    """Clear all gate state between tests."""
    ag._pending_bash.clear()
    ag._bash_denied.clear()
    ag._pending_tool.clear()
    ag._tool_denied.clear()


@pytest.fixture(autouse=True)
def clean_gate_state():
    _cleanup()
    yield
    _cleanup()


# ---------------------------------------------------------------------------
# AsyncGate unit tests
# ---------------------------------------------------------------------------


class TestAsyncGate:
    def test_default_not_set(self):
        gate = AsyncGate()
        assert not gate.is_set()

    def test_set_marks_as_set(self):
        gate = AsyncGate()
        gate.set()
        assert gate.is_set()

    def test_sync_wait_returns_true_when_set(self):
        gate = AsyncGate()
        gate.set()
        result = gate.wait(timeout=1.0)
        assert result is True

    def test_sync_wait_timeout_returns_false(self):
        gate = AsyncGate()
        result = gate.wait(timeout=0.01)
        assert result is False

    def test_set_from_other_thread(self):
        gate = AsyncGate()
        t = threading.Thread(target=gate.set)
        t.start()
        t.join()
        assert gate.is_set()

    def test_wait_returns_true_after_background_set(self):
        gate = AsyncGate()

        def _resolve():
            import time

            time.sleep(0.05)
            gate.set()

        t = threading.Thread(target=_resolve)
        t.start()
        result = gate.wait(timeout=2.0)
        t.join()
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_async_returns_true(self):
        loop = asyncio.get_running_loop()
        gate = AsyncGate(loop=loop)
        gate.set()
        result = await gate.wait_async(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_async_timeout(self):
        loop = asyncio.get_running_loop()
        gate = AsyncGate(loop=loop)
        result = await gate.wait_async(timeout=0.01)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_async_set_before_wait(self):
        loop = asyncio.get_running_loop()
        gate = AsyncGate(loop=loop)

        # Set the gate in the background
        async def _set_later():
            await asyncio.sleep(0.05)
            loop.call_soon_threadsafe(gate._async_event.set)

        asyncio.create_task(_set_later())
        result = await gate.wait_async(timeout=2.0)
        assert result is True


# ---------------------------------------------------------------------------
# Bash gate
# ---------------------------------------------------------------------------


class TestBashGate:
    def test_register_returns_async_gate(self):
        gate = ag.register_bash_gate("id-1")
        assert isinstance(gate, AsyncGate)

    def test_gate_not_set_before_resolve(self):
        gate = ag.register_bash_gate("id-2")
        assert not gate.is_set()

    def test_resolve_approved_sets_gate(self):
        gate = ag.register_bash_gate("id-3")
        ag.resolve_bash_gate("id-3", approved=True)
        assert gate.is_set()

    def test_resolve_approved_does_not_mark_denied(self):
        ag.register_bash_gate("id-4")
        ag.resolve_bash_gate("id-4", approved=True)
        assert not ag.is_bash_denied("id-4")

    def test_resolve_denied_sets_gate(self):
        gate = ag.register_bash_gate("id-5")
        ag.resolve_bash_gate("id-5", approved=False)
        assert gate.is_set()

    def test_resolve_denied_marks_denied(self):
        ag.register_bash_gate("id-6")
        ag.resolve_bash_gate("id-6", approved=False)
        assert ag.is_bash_denied("id-6")

    def test_resolve_removes_from_pending(self):
        ag.register_bash_gate("id-7")
        ag.resolve_bash_gate("id-7", approved=True)
        assert "id-7" not in ag._pending_bash

    def test_resolve_unknown_id_is_noop(self):
        ag.resolve_bash_gate("nonexistent", approved=False)

    def test_is_bash_denied_false_for_unresolved(self):
        ag.register_bash_gate("id-8")
        assert not ag.is_bash_denied("id-8")

    def test_is_bash_denied_false_for_unknown_id(self):
        assert not ag.is_bash_denied("never-seen")

    def test_multiple_gates_independent(self):
        g1 = ag.register_bash_gate("a")
        g2 = ag.register_bash_gate("b")
        ag.resolve_bash_gate("a", approved=False)
        assert g1.is_set()
        assert not g2.is_set()
        assert ag.is_bash_denied("a")
        assert not ag.is_bash_denied("b")

    def test_wait_returns_true_after_resolve(self):
        gate = ag.register_bash_gate("w-1")

        def _resolve():
            import time

            time.sleep(0.05)
            ag.resolve_bash_gate("w-1", approved=True)

        t = threading.Thread(target=_resolve)
        t.start()
        result = gate.wait(timeout=2.0)
        t.join()
        assert result is True


# ---------------------------------------------------------------------------
# Tool gate
# ---------------------------------------------------------------------------


class TestToolGate:
    def test_register_returns_async_gate(self):
        gate = ag.register_tool_gate("t-1")
        assert isinstance(gate, AsyncGate)

    def test_gate_not_set_before_resolve(self):
        gate = ag.register_tool_gate("t-2")
        assert not gate.is_set()

    def test_resolve_approved_sets_gate(self):
        gate = ag.register_tool_gate("t-3")
        ag.resolve_tool_gate("t-3", approved=True)
        assert gate.is_set()

    def test_resolve_approved_does_not_mark_denied(self):
        ag.register_tool_gate("t-4")
        ag.resolve_tool_gate("t-4", approved=True)
        assert not ag.is_tool_denied("t-4")

    def test_resolve_denied_sets_gate(self):
        gate = ag.register_tool_gate("t-5")
        ag.resolve_tool_gate("t-5", approved=False)
        assert gate.is_set()

    def test_resolve_denied_marks_denied(self):
        ag.register_tool_gate("t-6")
        ag.resolve_tool_gate("t-6", approved=False)
        assert ag.is_tool_denied("t-6")

    def test_resolve_removes_from_pending(self):
        ag.register_tool_gate("t-7")
        ag.resolve_tool_gate("t-7", approved=True)
        assert "t-7" not in ag._pending_tool

    def test_resolve_unknown_id_is_noop(self):
        ag.resolve_tool_gate("nonexistent", approved=False)

    def test_is_tool_denied_false_for_unresolved(self):
        ag.register_tool_gate("t-8")
        assert not ag.is_tool_denied("t-8")

    def test_is_tool_denied_false_for_unknown_id(self):
        assert not ag.is_tool_denied("never-seen")


# ---------------------------------------------------------------------------
# Cross-isolation: bash and tool gates don't share state
# ---------------------------------------------------------------------------


class TestGateIsolation:
    def test_bash_denial_does_not_affect_tool(self):
        ag.register_bash_gate("shared-id")
        ag.resolve_bash_gate("shared-id", approved=False)
        assert ag.is_bash_denied("shared-id")
        assert not ag.is_tool_denied("shared-id")

    def test_tool_denial_does_not_affect_bash(self):
        ag.register_tool_gate("shared-id")
        ag.resolve_tool_gate("shared-id", approved=False)
        assert ag.is_tool_denied("shared-id")
        assert not ag.is_bash_denied("shared-id")


# ---------------------------------------------------------------------------
# Thread-safety smoke test
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_register_and_resolve(self):
        """Registers 50 gates from multiple threads; all should resolve cleanly."""
        results: list = []
        lock = threading.Lock()

        def _worker(i: int) -> None:
            gate_id = f"gate-{i}"
            gate = ag.register_bash_gate(gate_id)
            ag.resolve_bash_gate(gate_id, approved=(i % 2 == 0))
            with lock:
                results.append(gate.is_set())

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        assert all(results)  # every gate must be set
