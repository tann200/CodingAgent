"""
Unit tests for wave_coordinator.py - Phase 6: PRSW
"""

import pytest
from unittest.mock import Mock, AsyncMock
from src.core.orchestration.wave_coordinator import (
    WaveCoordinator,
    ExecutionWave,
    create_wave_coordinator,
)
from src.core.orchestration.file_lock_manager import FileLockManager


class TestExecutionWave:
    def test_execution_wave_creation(self):
        wave = ExecutionWave(
            wave_id=0,
            read_agents=[{"result_key": "read1"}],
            write_agents=[{"result_key": "write1"}],
        )
        assert wave.wave_id == 0
        assert len(wave.read_agents) == 1
        assert len(wave.write_agents) == 1

    def test_execution_wave_empty(self):
        wave = ExecutionWave(wave_id=0)
        assert wave.wave_id == 0
        assert wave.read_agents == []
        assert wave.write_agents == []


class TestWaveCoordinator:
    def test_create_wave_coordinator(self):
        lock_manager = Mock()
        event_bus = Mock()
        coordinator = create_wave_coordinator(lock_manager, event_bus)

        assert coordinator.lock_manager is lock_manager
        assert coordinator.event_bus is event_bus
        assert coordinator.current_wave == 0

    @pytest.mark.asyncio
    async def test_execute_wave_empty(self):
        lock_manager = Mock()
        coordinator = WaveCoordinator(lock_manager)

        wave = ExecutionWave(wave_id=0)
        result = await coordinator.execute_wave(wave)

        assert result["wave_complete"] is True

    @pytest.mark.asyncio
    async def test_execute_parallel_reads(self):
        lock_manager = Mock()
        lock_manager.acquire_read_async = AsyncMock(return_value=True)
        lock_manager.release_read = AsyncMock()

        coordinator = WaveCoordinator(lock_manager)

        agents = [
            {"result_key": "read1", "files": ["file1.py"], "agent_id": "agent1"},
            {"result_key": "read2", "files": ["file2.py"], "agent_id": "agent2"},
        ]

        results = await coordinator._execute_parallel_reads(agents)

        assert "read1" in results
        assert "read2" in results

    @pytest.mark.asyncio
    async def test_execute_sequential_writes(self):
        lock_manager = Mock()
        lock_manager.acquire_write_async = AsyncMock(return_value=True)
        lock_manager.release_write = AsyncMock()
        lock_manager.reset_cancel = Mock()

        coordinator = WaveCoordinator(lock_manager)

        agents = [
            {"result_key": "write1", "files": ["file1.py"], "agent_id": "agent1"},
        ]

        results = await coordinator._execute_sequential_writes(agents)

        assert "write1" in results

    @pytest.mark.asyncio
    async def test_run_read_agent(self):
        lock_manager = Mock()
        lock_manager.acquire_read_async = AsyncMock(return_value=True)
        lock_manager.release_read = AsyncMock()

        coordinator = WaveCoordinator(lock_manager)

        agent = {"files": ["file1.py"], "agent_id": "agent1"}

        result = await coordinator._run_read_agent(agent)

        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_write_agent_lock_failure(self):
        lock_manager = Mock()
        lock_manager.acquire_write_async = AsyncMock(return_value=False)

        coordinator = WaveCoordinator(lock_manager)

        agent = {"files": ["file1.py"], "agent_id": "agent1"}

        with pytest.raises(TimeoutError):
            await coordinator._run_write_agent(agent)


class TestPRSWIntegration:
    """Integration tests for PRSW with real lock manager."""

    @pytest.fixture
    def lock_manager(self):
        """Create a FileLockManager for testing."""
        return FileLockManager("/tmp")

    @pytest.mark.asyncio
    async def test_parallel_reads_no_conflict(self, lock_manager):
        """Test that multiple read agents can read the same file."""
        result1 = await lock_manager.acquire_read_async("file.py", "agent1")
        result2 = await lock_manager.acquire_read_async("file.py", "agent2")

        assert result1 is True
        assert result2 is True

        await lock_manager.release_read("file.py", "agent1")
        await lock_manager.release_read("file.py", "agent2")

    @pytest.mark.asyncio
    async def test_write_blocks_read(self, lock_manager):
        """Test that write lock blocks read."""
        result_write = await lock_manager.acquire_write_async("file.py", "writer")
        assert result_write is True

        result_read = await lock_manager.acquire_read_async("file.py", "reader")
        assert result_read is False

        await lock_manager.release_write("file.py", "writer")

    @pytest.mark.asyncio
    async def test_write_blocks_write(self, lock_manager):
        """Test that write lock blocks another write."""
        result_write1 = await lock_manager.acquire_write_async("file.py", "writer1")
        assert result_write1 is True

        result_write2 = await lock_manager.acquire_write_async("file.py", "writer2")
        assert result_write2 is False

        await lock_manager.release_write("file.py", "writer1")

    @pytest.mark.asyncio
    async def test_read_allows_write_after_release(self, lock_manager):
        """Test that after read releases, write can proceed."""
        await lock_manager.acquire_read_async("file.py", "reader")
        await lock_manager.release_read("file.py", "reader")

        result = await lock_manager.acquire_write_async("file.py", "writer")
        assert result is True

    @pytest.mark.asyncio
    async def test_can_write(self, lock_manager):
        """Test can_write returns correct values."""
        assert lock_manager.can_write("file.py") is True

        await lock_manager.acquire_write_async("file.py", "writer")
        assert lock_manager.can_write("file.py") is False

        await lock_manager.release_write("file.py", "writer")
        assert lock_manager.can_write("file.py") is True

    @pytest.mark.asyncio
    async def test_cancel_event(self, lock_manager):
        """Test cancel event stops acquisition."""
        lock_manager.cancel()
        assert lock_manager._cancel_event.is_set() is True

        lock_manager.reset_cancel()
        assert lock_manager._cancel_event.is_set() is False
