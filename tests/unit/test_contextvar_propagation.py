import asyncio
import contextvars
import concurrent.futures
import threading


def test_contextvar_propagates_into_executor():
    VAR = contextvars.ContextVar("test_var", default=None)
    VAR.set("parent")

    def worker():
        # When using contextvars.copy_context().run(fn) the value should be visible
        return VAR.get()

    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(ctx.run, worker)
        res = fut.result(timeout=5)
    assert res == "parent"


def test_run_with_correlation_helper_available_and_uses_context(loop=None):
    # Import locally to avoid circular imports at module import time
    from src.core.orchestration.event_bus import run_with_correlation

    VAR = contextvars.ContextVar("test_var2", default=None)
    VAR.set("cid-1234")

    def worker():
        return VAR.get()

    # Use asyncio loop to exercise run_with_correlation path
    async def _run():
        loop = asyncio.get_running_loop()
        res = await run_with_correlation(loop, None, worker)
        return res

    out = asyncio.run(_run())
    assert out == "cid-1234"
