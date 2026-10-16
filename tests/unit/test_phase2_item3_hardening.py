"""Comprehensive unit tests for Phase 2 Item 3 hardening:
1. Per-owner file lock release guards preventing stale owner cancellation.
2. Bounded session shutdown hooks enforcing declared timeouts.
3. Cooperative cancellation preventing delayed writers from modifying disk after timeout.
4. Strict state schema registration and boundary enforcement for frontier_loop.
"""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.orchestration.file_lock_manager import FileLockManager
from src.core.orchestration.session_lifecycle import (
    SessionLifecycleManager,
    ShutdownReason,
)
from src.core.orchestration.tool_execution_pipeline import (
    _CURRENT_TOOL_CANCEL_EVENT,
    is_current_tool_cancelled,
)
from src.core.orchestration.graph.state_schemas import (
    NodeResultViolation,
    get_node_output_schema,
    validate_node_result,
    wrap_node,
)
from src.tools._file_io import write_file, delete_file, rename_file, create_directory
from src.tools._edit_tools import _atomic_write


# ---------------------------------------------------------------------------
# 1. Per-Owner File Lock Guards
# ---------------------------------------------------------------------------


class TestPerOwnerLockGuards:
    @pytest.mark.asyncio
    async def test_stale_owner_cannot_cancel_new_owner_guard(self, tmp_path):
        """When owner A's lock is acquired with a short timeout, and then owner B acquires
        the lock, owner A releasing afterwards must NOT cancel owner B's auto-release guard.
        """
        manager = FileLockManager(str(tmp_path))
        file_path = str(tmp_path / "shared.txt")

        # Owner A acquires with 10s timeout
        acq_a = await manager.acquire_write_async(file_path, "agent_A", timeout=10.0)
        assert acq_a is True
        guard_a = manager._lock_release_guards.get((file_path, "agent_A"))
        assert guard_a is not None and not guard_a.done()

        # Simulate lock transition to agent_B (e.g. after lock expiry or transfer)
        async with manager._async_lock:
            from src.core.orchestration.file_lock_manager import FileLock
            manager._write_lock = FileLock(file_path, "agent_B", "write", time.time())
            # Start guard for agent_B
            guard_b = asyncio.ensure_future(
                manager._auto_release_after_timeout(file_path, "agent_B", 10.0)
            )
            manager._lock_release_guards[(file_path, "agent_B")] = guard_b

        # Now stale owner agent_A attempts to release
        await manager.release_write(file_path, "agent_A")

        # Guard B must NOT have been cancelled by agent_A's release!
        assert not guard_b.cancelled()
        assert (file_path, "agent_B") in manager._lock_release_guards

        # And agent_B releasing properly cancels guard_b
        await manager.release_write(file_path, "agent_B")
        assert guard_b.cancelling() or guard_b.cancelled()
        assert (file_path, "agent_B") not in manager._lock_release_guards

        # Cleanup guard_a
        guard_a.cancel()


# ---------------------------------------------------------------------------
# 2. Bounded Lifecycle Shutdown Hooks
# ---------------------------------------------------------------------------


class TestBoundedShutdownHooks:
    def test_hanging_shutdown_hook_times_out(self, tmp_path):
        """A shutdown hook that blocks longer than the declared timeout must be aborted
        and recorded as hook:<name>:timeout, without blocking shutdown indefinitely.
        """
        manager = SessionLifecycleManager(workdir=str(tmp_path))
        session_id = "test_hang_session"

        def fast_hook(sid):
            pass

        def hanging_hook(sid):
            time.sleep(2.0)

        manager.register_shutdown_hook("fast", fast_hook)
        manager.register_shutdown_hook("hanging", hanging_hook)

        start = time.time()
        # Call shutdown with a 0.2 second timeout
        result = manager.shutdown_session(
            session_id, reason=ShutdownReason.TIMEOUT, timeout=0.2
        )
        elapsed = time.time() - start

        # Must return reasonably close to 0.2s, definitely less than the 2s hanging hook
        assert elapsed < 1.0, f"Shutdown took too long: {elapsed:.2f}s"
        assert "hook:fast" in result.cleanup_actions
        assert "hook:hanging:timeout" in result.cleanup_actions
        assert "registry:unregistered" in result.cleanup_actions or "operations:cancelled" in result.cleanup_actions


# ---------------------------------------------------------------------------
# 3. Timeout Cooperative Cancellation Prevents Delayed Writers
# ---------------------------------------------------------------------------


class TestCooperativeCancellation:
    def test_delayed_writer_does_not_create_file(self, tmp_path):
        """When a tool times out and sets the cancellation event, a delayed writer
        must detect the cancellation and abort without creating or modifying the target file.
        """
        target_file = tmp_path / "delayed_test.txt"
        assert not target_file.exists()

        import threading

        cancel_ev = threading.Event()
        token = _CURRENT_TOOL_CANCEL_EVENT.set(cancel_ev)
        try:
            # Simulate timeout occurring before file is committed
            cancel_ev.set()
            assert is_current_tool_cancelled() is True

            # Attempt write_file
            res = write_file(
                path="delayed_test.txt",
                content="This should not be written",
                workdir=tmp_path,
            )
            assert res.get("cancelled") is True or res.get("status") == "error"
            # Target file must NOT exist on disk!
            assert not target_file.exists()

            # Attempt _atomic_write
            with pytest.raises(TimeoutError):
                _atomic_write(target_file, "Atomic content should not write")
            assert not target_file.exists()

            # Attempt create_directory
            new_dir = tmp_path / "delayed_dir"
            res_dir = create_directory("delayed_dir", workdir=tmp_path)
            assert res_dir.get("cancelled") is True or res_dir.get("status") == "error"
            assert not new_dir.exists()
        finally:
            _CURRENT_TOOL_CANCEL_EVENT.reset(token)


# ---------------------------------------------------------------------------
# 4. Strict State Schemas & Frontier Loop Boundary
# ---------------------------------------------------------------------------


class TestFrontierLoopSchemaEnforcement:
    def test_frontier_loop_schema_registered(self):
        """frontier_loop must have a registered output schema."""
        schema = get_node_output_schema("frontier_loop")
        assert schema is not None
        assert "history" in schema.allowed_keys
        assert "last_result" in schema.allowed_keys
        assert "tool_call_count" in schema.allowed_keys

    def test_valid_frontier_loop_result_passes_validation(self):
        """A valid frontier loop result has zero violations."""
        valid_res = {
            "history": [{"role": "assistant", "content": "Done."}],
            "tool_call_count": 1,
            "last_result": {"ok": True},
            "errors": [],
            "_frontier_loop_turns": 1,
            "awaiting_plan_approval": False,
        }
        violations = validate_node_result("frontier_loop", valid_res)
        assert violations == []

    def test_invalid_frontier_loop_result_fails_in_strict_mode(self):
        """In strict mode, an invalid frontier loop output raises NodeResultViolation."""
        async def _bad_node(state, config):
            return {
                "history": [],
                "tool_call_count": 0,
                "errors": [],
                "awaiting_plan_approval": False,
                "bogus_unauthorized_key": 123,
            }

        wrapped = wrap_node("frontier_loop", _bad_node, strict=True)
        with pytest.raises(NodeResultViolation) as exc_info:
            asyncio.run(wrapped({}, {}))

        assert exc_info.value.node_name == "frontier_loop"
        assert exc_info.value.reason == "unknown_key"
        assert any("bogus_unauthorized_key" in d for d in exc_info.value.details)
