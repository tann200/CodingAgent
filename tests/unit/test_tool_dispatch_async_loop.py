import asyncio
import pytest
from unittest.mock import MagicMock
from src.core.orchestration.tool_execution_pipeline import _dispatch_tool_call


@pytest.mark.asyncio
async def test_dispatch_async_tool_from_running_loop():
    async def sample_async_tool(msg: str):
        await asyncio.sleep(0.01)
        return {"status": "ok", "echo": msg}

    orch = MagicMock()
    orch.working_dir = None
    orch._tool_executor = None

    tool = {"fn": sample_async_tool}
    # timeout_seconds=0 forces execution in the current thread which has a running asyncio loop
    res = _dispatch_tool_call(
        orch=orch,
        name="sample_async_tool",
        args={"msg": "hello from loop"},
        tool=tool,
        tool_call_id="call_1",
        timeout_seconds=0,
    )

    assert res == {"status": "ok", "echo": "hello from loop"}
