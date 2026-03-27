"""
Wave coordinator for Parallel Reads, Sequential Writes execution.
Coordinates execution waves for PRSW.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Dict

logger = logging.getLogger(__name__)


@dataclass
class ExecutionWave:
    wave_id: int
    read_agents: List[Dict] = field(default_factory=list)
    write_agents: List[Dict] = field(default_factory=list)


class WaveCoordinator:
    """
    Coordinates execution waves for Parallel Reads, Sequential Writes.

    Wave structure:
    - Wave N: Read-only agents (Scout, Researcher) run in parallel
    - After Wave N: Coder executes writes sequentially
    - After all writes: Next wave begins

    CRITICAL: Uses async-only operations, no time.sleep().
    """

    def __init__(self, lock_manager, event_bus=None):
        self.lock_manager = lock_manager
        self.event_bus = event_bus
        self.current_wave = 0

    async def execute_wave(self, wave: ExecutionWave) -> Dict:
        """Execute a single wave: parallel reads, then sequential writes."""

        read_results = await self._execute_parallel_reads(wave.read_agents)

        write_results = await self._execute_sequential_writes(wave.write_agents)

        return {
            "read_results": read_results,
            "write_results": write_results,
            "wave_complete": True,
        }

    async def _execute_parallel_reads(self, agents: List[Dict]) -> Dict:
        """Execute read-only agents in parallel."""
        if not agents:
            return {}

        tasks = []
        for agent in agents:
            task = asyncio.create_task(self._run_read_agent(agent))
            tasks.append((agent.get("result_key", "read"), task))

        results = {}
        for key, task in tasks:
            try:
                result = await task
                results[key] = result
            except Exception as e:
                logger.error(f"_execute_parallel_reads: {key} failed: {e}")
                results[key] = {"status": "error", "error": str(e)}

        return results

    async def _execute_sequential_writes(self, agents: List[Dict]) -> Dict:
        """Execute write agents sequentially with proper lock management."""
        if not agents:
            return {}

        results = {}
        for agent in agents:
            result = await self._run_write_agent(agent)
            key = agent.get("result_key", "write")
            results[key] = result

        return results

    async def _run_read_agent(self, agent: Dict) -> Dict:
        """Run a read-only agent."""
        files = agent.get("files", [])
        agent_id = agent.get("agent_id", "read_agent")

        acquired = []
        try:
            for f in files:
                if await self.lock_manager.acquire_read_async(f, agent_id):
                    acquired.append(f)

            result = {
                "status": "completed",
                "files": files,
            }
            return result

        finally:
            for f in acquired:
                await self.lock_manager.release_read(f, agent_id)

    async def _run_write_agent(self, agent: Dict) -> Dict:
        """Run a write agent with lock management."""
        files = agent.get("files", [])
        agent_id = agent.get("agent_id", "write_agent")
        acquired = []

        try:
            for f in files:
                success = await self.lock_manager.acquire_write_async(f, agent_id)
                if not success:
                    raise TimeoutError(f"Failed to acquire lock for {f}")
                acquired.append(f)

            result = {
                "status": "completed",
                "files": files,
            }
            return result

        except asyncio.CancelledError:
            logger.warning(
                f"_run_write_agent: cancelled, releasing {len(acquired)} locks"
            )
            raise

        finally:
            for f in acquired:
                await self.lock_manager.release_write(f, agent_id)
            self.lock_manager.reset_cancel()


def create_wave_coordinator(lock_manager, event_bus=None) -> WaveCoordinator:
    """Factory function to create a WaveCoordinator."""
    return WaveCoordinator(lock_manager=lock_manager, event_bus=event_bus)
