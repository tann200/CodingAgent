"""
Contract tests for Fast-Path stabilization: file lock manager safety nets.

Phase 2 zombie-lock prevention features:
  - reset_instance() provides a clean singleton
  - cancel(owner=) scopes the cancellation signal
  - reset_cancel(owner=) only clears cancel for matching owner
  - _auto_release_after_timeout releases zombie write locks
"""

import asyncio
import time

import pytest

from src.core.orchestration.file_lock_manager import (
    FileLockManager,
    FileLock,
    get_file_lock_manager,
)


class TestFastPathResetInstance:
    """Phase 2 contract: reset_instance() replaces the singleton with a clean state."""

    def setup_method(self):
        FileLockManager._instance = None
        FileLockManager._atexit_installed = False

    def test_reset_instance_returns_new_instance(self):
        manager1 = get_file_lock_manager("/tmp/work1")
        old_id = id(manager1)

        manager2 = FileLockManager.reset_instance("/tmp/work2")
        assert id(manager2) != old_id
        assert manager2 is get_file_lock_manager()

    def test_reset_instance_clears_old_locks(self):
        FileLockManager._instance = None
        manager = FileLockManager.get_instance("/tmp")
        manager._write_lock = FileLock("test.py", "agent1", "write", 0.0)
        manager._read_locks["test.py"] = {"agent2"}

        FileLockManager.reset_instance("/tmp")
        fresh = get_file_lock_manager()
        assert fresh._write_lock is None
        assert fresh._read_locks == {}

    @pytest.mark.asyncio
    async def test_reset_instance_cancels_pending_acquires(self):
        FileLockManager._instance = None
        manager = FileLockManager.get_instance("/tmp")
        await manager.acquire_write_async("test.py", "agent1", timeout=5.0)

        async def try_acquire():
            return await manager.acquire_write_async("test.py", "agent2", timeout=5.0)

        acquire_task = asyncio.ensure_future(try_acquire())
        await asyncio.sleep(0.15)

        FileLockManager.reset_instance("/tmp")
        await asyncio.sleep(0.3)

        if acquire_task.done():
            result = acquire_task.result()
            assert result is False
        else:
            acquire_task.cancel()
            try:
                await acquire_task
            except asyncio.CancelledError:
                pass

    def test_reset_instance_cancels_old_instance(self):
        manager = get_file_lock_manager("/tmp")
        manager.cancel()
        FileLockManager.reset_instance("/tmp/other")
        fresh = get_file_lock_manager()
        assert fresh._cancel_event.is_set() is False


class TestFastPathCancelOwner:
    """Phase 2 contract: cancel(owner=) scopes reset_cancel(owner=)."""

    def test_cancel_with_owner_sets_owner(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")
        assert manager._cancel_owner == "agent1"

    def test_reset_cancel_matching_owner_clears(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")

        manager.reset_cancel(owner="agent1")
        assert manager._cancel_event.is_set() is False
        assert manager._cancel_owner is None

    def test_reset_cancel_wrong_owner_does_not_clear(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")

        manager.reset_cancel(owner="agent2")
        assert manager._cancel_event.is_set() is True
        assert manager._cancel_owner == "agent1"

    def test_cancel_without_owner_allows_any_reset(self):
        manager = FileLockManager("/tmp")
        manager.cancel()

        manager.reset_cancel(owner="agent2")
        assert manager._cancel_event.is_set() is False

    def test_reset_cancel_without_owner_when_owner_set_does_nothing(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")

        manager.reset_cancel()
        assert manager._cancel_event.is_set() is True
        assert manager._cancel_owner == "agent1"

    def test_owner_is_none_by_default(self):
        manager = FileLockManager("/tmp")
        assert manager._cancel_owner is None

    def test_double_cancel_overwrites_owner(self):
        manager = FileLockManager("/tmp")
        manager.cancel(owner="agent1")
        manager.cancel(owner="agent2")
        assert manager._cancel_owner == "agent2"

        manager.reset_cancel(owner="agent2")
        assert manager._cancel_event.is_set() is False


@pytest.mark.asyncio
class TestFastPathAutoRelease:
    """Phase 2 contract: auto-release guard fires after timeout."""

    async def test_auto_release_fires_after_timeout(self):
        manager = FileLockManager("/tmp")
        acquired = await manager.acquire_write_async("zombie.py", "agent1", timeout=0.2)
        assert acquired is True
        assert manager._write_lock is not None

        await asyncio.sleep(0.5)
        assert manager._write_lock is None

    async def test_release_write_prevents_auto_release(self):
        manager = FileLockManager("/tmp")
        await manager.acquire_write_async("safe.py", "agent1", timeout=5.0)
        assert manager._lock_release_guard is not None
        assert manager._lock_release_guard.done() is False

        await manager.release_write("safe.py", "agent1")
        assert manager._write_lock is None

    async def test_auto_release_only_acts_on_matching_owner(self):
        manager = FileLockManager("/tmp")
        await manager.acquire_write_async("shared.py", "agent1", timeout=5.0)
        guard = manager._lock_release_guard
        assert guard is not None

        manager._write_lock = FileLock("shared.py", "agent2", "write", time.time())
        await asyncio.sleep(0.2)
        assert manager._write_lock is not None
        assert manager._write_lock.locked_by == "agent2"

    async def test_normal_release_clears_guard_reference(self):
        manager = FileLockManager("/tmp")
        await manager.acquire_write_async("normal.py", "agent1", timeout=10.0)
        assert manager._lock_release_guard is not None

        await manager.release_write("normal.py", "agent1")
        assert manager._lock_release_guard is None


class TestFastPathAtexit:
    """Phase 2 contract: atexit cleanup handler is installed."""

    def setup_method(self):
        FileLockManager._instance = None
        FileLockManager._atexit_installed = False

    def test_atexit_handler_installed_on_first_instance(self):
        manager = FileLockManager("/tmp")
        assert FileLockManager._atexit_installed is True

    def test_atexit_not_reinstalled(self):
        m1 = FileLockManager("/tmp")
        assert FileLockManager._atexit_installed is True
        m2 = FileLockManager("/tmp")
        assert FileLockManager._atexit_installed is True
