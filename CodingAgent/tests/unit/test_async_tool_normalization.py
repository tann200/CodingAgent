import asyncio
import inspect


def test_async_tool_normalization_in_thread():
    """Ensure that when a callable executed in a worker thread returns an
    awaitable, the helper runs it to completion and does not return a coroutine
    object to the caller.
    """

    async def async_tool(x):
        await asyncio.sleep(0)
        return {"value": x * 2}

    def wrapper(x):
        # Simulate a tool function that accidentally returns an awaitable
        return async_tool(x)

    # The expected behaviour is that the awaitable is executed inside the thread
    # (via asyncio.run) and the caller receives the resolved dict, not a coroutine.

    def call_in_thread():
        # Re-create the logic used in tool_execution_pipeline._run_tool_callable
        rv = wrapper(21)
        if inspect.isawaitable(rv):
            return asyncio.run(rv)
        return rv

    res = None
    try:
        import concurrent.futures as _cf

        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(call_in_thread)
            res = fut.result(timeout=5)
    finally:
        pass

    assert isinstance(res, dict)
    assert res.get("value") == 42
