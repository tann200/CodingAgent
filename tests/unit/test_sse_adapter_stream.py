import asyncio

import pytest

from src.core.orchestration.event_bus import EventBus
from src.server.app import ServerEventBusAdapter


@pytest.mark.asyncio
async def test_sse_adapter_streams_event():
    bus = EventBus()
    adapter = ServerEventBusAdapter(bus)

    gen = adapter.event_generator("all")

    # Start awaiting the first generated SSE payload
    next_fut = asyncio.create_task(gen.__anext__())

    # Give the generator a moment to subscribe
    await asyncio.sleep(0.01)

    # Publish an event that the adapter subscribes to
    bus.publish("agent.start", {"session_id": "s1", "msg": "ok"})

    res = await asyncio.wait_for(next_fut, timeout=1.0)
    assert "agent.start" in res

    # Close the generator to trigger cleanup
    try:
        await gen.aclose()
    except Exception:
        pass
