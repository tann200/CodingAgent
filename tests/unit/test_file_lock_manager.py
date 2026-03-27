"""
Unit tests for file_lock_manager.py - Phase 6: PRSW
"""

import pytest
from src.core.orchestration.file_lock_manager import (
    FileLockManager,
    FileLock,
    get_file_lock_manager,
)


class TestFileLockManager:
    def test_singleton(self):
        manager1 = get_file_lock_manager("/tmp")
        manager2 = get_file_lock_manager("/tmp")
        assert manager1 is manager2

    def test_can_write_no_locks(self):
        manager = FileLockManager("/tmp")
        assert manager.can_write("test.py") is True

    def test_can_write_with_write_lock(self):
        manager = FileLockManager("/tmp")
        manager._write_lock = FileLock("test.py", "agent1", "write", 0.0)
        assert manager.can_write("test.py") is False

    def test_can_write_with_read_lock(self):
        manager = FileLockManager("/tmp")
        manager._read_locks["test.py"] = {"agent1"}
        assert manager.can_write("test.py") is False

    def test_can_read_no_locks(self):
        manager = FileLockManager("/tmp")
        assert manager.can_read("test.py") is True

    def test_can_read_with_write_lock(self):
        manager = FileLockManager("/tmp")
        manager._write_lock = FileLock("test.py", "agent1", "write", 0.0)
        assert manager.can_read("test.py") is False

    def test_get_lock_status(self):
        manager = FileLockManager("/tmp")
        manager._read_locks["test.py"] = {"agent1", "agent2"}

        status = manager.get_lock_status("test.py")
        assert status["path"] == "test.py"
        assert len(status["readers"]) == 2
        assert status["writer"] is None

    @pytest.mark.asyncio
    async def test_acquire_read_async(self):
        manager = FileLockManager("/tmp")

        result = await manager.acquire_read_async("test.py", "agent1")
        assert result is True
        assert "agent1" in manager._read_locks["test.py"]

    @pytest.mark.asyncio
    async def test_acquire_write_async(self):
        manager = FileLockManager("/tmp")

        result = await manager.acquire_write_async("test.py", "agent1")
        assert result is True
        assert manager._write_lock is not None

    @pytest.mark.asyncio
    async def test_release_read(self):
        manager = FileLockManager("/tmp")
        await manager.acquire_read_async("test.py", "agent1")

        await manager.release_read("test.py", "agent1")
        assert "agent1" not in manager._read_locks.get("test.py", set())

    @pytest.mark.asyncio
    async def test_release_write(self):
        manager = FileLockManager("/tmp")
        await manager.acquire_write_async("test.py", "agent1")

        await manager.release_write("test.py", "agent1")
        assert manager._write_lock is None

    @pytest.mark.asyncio
    async def test_cancel_event(self):
        manager = FileLockManager("/tmp")
        manager.cancel()

        assert manager._cancel_event.is_set() is True

    @pytest.mark.asyncio
    async def test_reset_cancel(self):
        manager = FileLockManager("/tmp")
        manager.cancel()
        manager.reset_cancel()

        assert manager._cancel_event.is_set() is False
