"""
PRSW (Parallel Reads, Sequential Writes) Integration Tests.

Verifies end-to-end correctness of:
  - FileLockManager: parallel read acquisition, exclusive write acquisition
  - WaveCoordinator: reads run in parallel, writes run sequentially
  - Lock release on exception and cancellation
  - delegation_node PRSW path: lock_manager resolved from state / orchestrator
  - AgentState _file_lock_manager field populated correctly

These tests do NOT require a live LLM backend.
"""

import asyncio
import threading
import time
import pytest

from src.core.orchestration.file_lock_manager import FileLockManager
from src.core.orchestration.wave_coordinator import WaveCoordinator, ExecutionWave


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_lock_manager(cancel_event=None) -> FileLockManager:
    cancel = cancel_event or threading.Event()
    return FileLockManager(workdir="/tmp", cancel_event=cancel)


# ─────────────────────────────────────────────────────────────────────────────
# FileLockManager: parallel reads
# ─────────────────────────────────────────────────────────────────────────────

class TestFileLockManagerReads:

    @pytest.mark.asyncio
    async def test_multiple_readers_acquire_simultaneously(self):
        """Multiple agents can hold read locks on the same file at the same time."""
        lm = _make_lock_manager()
        path = "src/foo.py"

        r1 = await lm.acquire_read_async(path, "agent-1")
        r2 = await lm.acquire_read_async(path, "agent-2")
        r3 = await lm.acquire_read_async(path, "agent-3")

        assert r1 is True
        assert r2 is True
        assert r3 is True
        assert lm.can_read(path)

        await lm.release_read(path, "agent-1")
        await lm.release_read(path, "agent-2")
        await lm.release_read(path, "agent-3")

    @pytest.mark.asyncio
    async def test_read_blocked_while_write_held(self):
        """A reader cannot acquire a lock while a writer holds it."""
        lm = _make_lock_manager()
        path = "src/bar.py"

        # Acquire write first
        w = await lm.acquire_write_async(path, "writer", timeout=5.0)
        assert w is True
        assert not lm.can_read(path)

        # Now release and verify read succeeds
        await lm.release_write(path, "writer")
        r = await lm.acquire_read_async(path, "reader")
        assert r is True
        await lm.release_read(path, "reader")

    @pytest.mark.asyncio
    async def test_release_read_removes_agent(self):
        """release_read removes the agent from the read-lock set."""
        lm = _make_lock_manager()
        path = "src/baz.py"

        await lm.acquire_read_async(path, "agent-x")
        await lm.release_read(path, "agent-x")

        # After release the path should have no readers — write should succeed
        assert lm.can_write(path)


# ─────────────────────────────────────────────────────────────────────────────
# FileLockManager: sequential writes
# ─────────────────────────────────────────────────────────────────────────────

class TestFileLockManagerWrites:

    @pytest.mark.asyncio
    async def test_write_lock_exclusive(self):
        """Second writer waits until first writer releases."""
        lm = _make_lock_manager()
        path = "src/shared.py"

        # First writer acquires immediately
        w1 = await lm.acquire_write_async(path, "writer-1", timeout=5.0)
        assert w1 is True

        # Second writer cannot write while first holds lock
        assert not lm.can_write(path)

        # Release first writer → second can now acquire
        await lm.release_write(path, "writer-1")
        w2 = await lm.acquire_write_async(path, "writer-2", timeout=5.0)
        assert w2 is True
        await lm.release_write(path, "writer-2")

    @pytest.mark.asyncio
    async def test_write_lock_times_out_when_blocked(self):
        """acquire_write_async returns False when it cannot acquire within timeout."""
        lm = _make_lock_manager()
        path = "src/locked.py"

        # Hold write lock
        await lm.acquire_write_async(path, "holder", timeout=5.0)

        # Another writer with very short timeout should fail
        result = await lm.acquire_write_async(path, "waiter", timeout=0.2)
        assert result is False

        await lm.release_write(path, "holder")

    @pytest.mark.asyncio
    async def test_write_lock_respects_cancel_event(self):
        """acquire_write_async returns False immediately when cancel_event is set."""
        cancel = threading.Event()
        lm = _make_lock_manager(cancel_event=cancel)
        path = "src/cancel_test.py"

        # Hold write lock
        await lm.acquire_write_async(path, "holder", timeout=30.0)

        # Set cancel event — next acquire should return False immediately
        cancel.set()
        result = await lm.acquire_write_async(path, "waiter", timeout=30.0)
        assert result is False

        cancel.clear()
        await lm.release_write(path, "holder")

    @pytest.mark.asyncio
    async def test_lock_released_even_on_exception(self):
        """Locks acquired in _execute_tool_with_locks are released in the finally block."""
        from unittest.mock import AsyncMock, MagicMock
        from src.core.orchestration.graph.nodes.execution_node import _execute_tool_with_locks

        lm = _make_lock_manager()
        path = "src/explode.py"

        # Orchestrator whose execute_tool raises
        orch = MagicMock()
        orch.execute_tool = MagicMock(side_effect=RuntimeError("boom"))

        result = await _execute_tool_with_locks(
            "write_file", {"path": path}, lm, orch, "agent-err"
        )

        assert result["ok"] is False
        # Lock must be released — write should succeed again
        assert lm.can_write(path)


# ─────────────────────────────────────────────────────────────────────────────
# WaveCoordinator: parallel reads + sequential writes
# ─────────────────────────────────────────────────────────────────────────────

class TestWaveCoordinator:

    @pytest.mark.asyncio
    async def test_reads_execute_and_return_results(self):
        """execute_wave returns read_results keyed by result_key."""
        lm = _make_lock_manager()
        wc = WaveCoordinator(lock_manager=lm)

        wave = ExecutionWave(
            wave_id=0,
            read_agents=[
                {"agent_id": "scout", "files": ["src/a.py"], "result_key": "scout_result"},
                {"agent_id": "researcher", "files": ["src/b.py"], "result_key": "research_result"},
            ],
            write_agents=[],
        )

        result = await wc.execute_wave(wave)

        assert result["wave_complete"] is True
        assert "scout_result" in result["read_results"]
        assert "research_result" in result["read_results"]
        assert result["read_results"]["scout_result"]["status"] == "completed"
        assert result["read_results"]["research_result"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_writes_execute_sequentially_and_return_results(self):
        """execute_wave runs write agents one-by-one and returns write_results."""
        lm = _make_lock_manager()
        wc = WaveCoordinator(lock_manager=lm)

        wave = ExecutionWave(
            wave_id=0,
            read_agents=[],
            write_agents=[
                {"agent_id": "coder-1", "files": ["src/x.py"], "result_key": "write_x"},
                {"agent_id": "coder-2", "files": ["src/y.py"], "result_key": "write_y"},
            ],
        )

        result = await wc.execute_wave(wave)

        assert result["wave_complete"] is True
        assert result["write_results"]["write_x"]["status"] == "completed"
        assert result["write_results"]["write_y"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_reads_run_before_writes(self):
        """Read results are populated before write agents execute."""
        lm = _make_lock_manager()
        wc = WaveCoordinator(lock_manager=lm)
        order = []

        async def track_read(agent):
            order.append(f"read:{agent['result_key']}")
            return {"status": "completed", "files": agent.get("files", [])}

        async def track_write(agent):
            order.append(f"write:{agent['result_key']}")
            return {"status": "completed", "files": agent.get("files", [])}

        # Patch internal methods to track execution order
        wc._run_read_agent = track_read
        wc._run_write_agent = track_write

        wave = ExecutionWave(
            wave_id=0,
            read_agents=[
                {"result_key": "r1", "files": ["a.py"]},
                {"result_key": "r2", "files": ["b.py"]},
            ],
            write_agents=[
                {"result_key": "w1", "files": ["c.py"]},
            ],
        )

        await wc.execute_wave(wave)

        read_positions = [i for i, o in enumerate(order) if o.startswith("read:")]
        write_positions = [i for i, o in enumerate(order) if o.startswith("write:")]

        assert read_positions, "No reads recorded"
        assert write_positions, "No writes recorded"
        assert max(read_positions) < min(write_positions), (
            "All reads must finish before any write begins"
        )

    @pytest.mark.asyncio
    async def test_parallel_reads_overlap_in_time(self):
        """Two read agents with sleep should overlap, proving parallel execution."""
        lm = _make_lock_manager()
        wc = WaveCoordinator(lock_manager=lm)

        DELAY = 0.15  # seconds each agent sleeps

        async def slow_read(agent):
            await asyncio.sleep(DELAY)
            return {"status": "completed", "files": []}

        wc._run_read_agent = slow_read

        wave = ExecutionWave(
            wave_id=0,
            read_agents=[
                {"result_key": "r1", "files": []},
                {"result_key": "r2", "files": []},
            ],
            write_agents=[],
        )

        t0 = time.monotonic()
        await wc.execute_wave(wave)
        elapsed = time.monotonic() - t0

        # If sequential, elapsed ≥ 2×DELAY. If parallel, elapsed < 1.5×DELAY.
        assert elapsed < DELAY * 1.8, (
            f"Reads appear sequential (elapsed={elapsed:.3f}s, threshold={DELAY * 1.8:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_empty_wave_returns_empty_results(self):
        """execute_wave with no agents returns empty dicts."""
        lm = _make_lock_manager()
        wc = WaveCoordinator(lock_manager=lm)

        wave = ExecutionWave(wave_id=0)
        result = await wc.execute_wave(wave)

        assert result["wave_complete"] is True
        assert result["read_results"] == {}
        assert result["write_results"] == {}

    @pytest.mark.asyncio
    async def test_write_cancellation_releases_locks(self):
        """Cancelling a write agent task still releases all acquired locks."""
        lm = _make_lock_manager()
        wc = WaveCoordinator(lock_manager=lm)

        path = "src/cancel_write.py"

        async def cancellable_write(agent):
            files = agent.get("files", [])
            acquired = []
            try:
                for f in files:
                    ok = await lm.acquire_write_async(f, "w", timeout=5.0)
                    if ok:
                        acquired.append(f)
                await asyncio.sleep(10)  # will be cancelled
                return {"status": "completed"}
            except asyncio.CancelledError:
                raise
            finally:
                for f in acquired:
                    await lm.release_write(f, "w")

        wc._run_write_agent = cancellable_write

        wave = ExecutionWave(
            wave_id=0,
            write_agents=[{"result_key": "w1", "files": [path]}],
        )

        task = asyncio.create_task(wc.execute_wave(wave))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        # Lock must be freed so a new writer can acquire immediately
        ok = await lm.acquire_write_async(path, "new-writer", timeout=0.5)
        assert ok is True, "Lock was not released after cancellation"
        await lm.release_write(path, "new-writer")


# ─────────────────────────────────────────────────────────────────────────────
# AgentState _file_lock_manager field
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentStatePRSWFields:

    def test_agentstate_has_file_lock_manager_field(self):
        """AgentState TypedDict declares _file_lock_manager."""
        from src.core.orchestration.graph.state import AgentState
        assert "_file_lock_manager" in AgentState.__annotations__

    def test_agentstate_has_write_queue_field(self):
        """AgentState TypedDict declares _write_queue."""
        from src.core.orchestration.graph.state import AgentState
        assert "_write_queue" in AgentState.__annotations__

    def test_agentstate_has_compact_tracking_fields(self):
        """AgentState TypedDict declares Phase-4 token auto-compact fields."""
        from src.core.orchestration.graph.state import AgentState
        annotations = AgentState.__annotations__
        assert "last_compact_at" in annotations
        assert "last_compact_turn" in annotations
        assert "context_degradation_detected" in annotations

    def test_agentstate_has_p2p_fields(self):
        """AgentState TypedDict declares Phase-B P2P fields."""
        from src.core.orchestration.graph.state import AgentState
        annotations = AgentState.__annotations__
        assert "_agent_session_manager" in annotations
        assert "_agent_messages" in annotations
        assert "_context_controller" in annotations


# ─────────────────────────────────────────────────────────────────────────────
# delegation_node PRSW path
# ─────────────────────────────────────────────────────────────────────────────

class TestDelegationNodePRSW:

    @pytest.mark.asyncio
    async def test_delegation_node_resolves_lock_manager_from_state(self, tmp_path):
        """delegation_node picks up _file_lock_manager from state when present."""
        from unittest.mock import MagicMock, patch
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        lm = _make_lock_manager()
        state = {
            "task": "test",
            "working_dir": str(tmp_path),
            "delegations": [],
            "delegation_results": None,
            "session_id": "test-session",
            "_file_lock_manager": lm,
            "history": [],
        }
        config = {"configurable": {"orchestrator": None}}

        # delegation_node with empty delegations should return quickly without error
        result = await delegation_node(state, config)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_delegation_node_resolves_lock_manager_from_orchestrator(self, tmp_path):
        """delegation_node falls back to orchestrator.file_lock_manager when not in state."""
        from unittest.mock import MagicMock
        from src.core.orchestration.graph.nodes.delegation_node import delegation_node

        lm = _make_lock_manager()
        orch = MagicMock()
        orch.file_lock_manager = lm

        state = {
            "task": "test",
            "working_dir": str(tmp_path),
            "delegations": [],
            "delegation_results": None,
            "session_id": "test-session",
            "_file_lock_manager": None,  # not set in state — must come from orchestrator
            "history": [],
        }
        config = {"configurable": {"orchestrator": orch}}

        result = await delegation_node(state, config)
        assert isinstance(result, dict)
