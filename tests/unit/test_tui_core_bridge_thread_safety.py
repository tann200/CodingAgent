import asyncio
import threading
from unittest.mock import MagicMock
from tui.tui_src.ui.core_bridge import AgentBridge


def test_schedule_callback_on_main_app_loop():
    app = MagicMock()
    app_loop = asyncio.new_event_loop()
    app._loop = app_loop

    bridge = AgentBridge(app)
    mock_fn = MagicMock()

    # When running on the exact app loop:
    async def _test():
        bridge._schedule_callback(mock_fn, "arg1", 42)
        # Yield to let call_soon execute
        await asyncio.sleep(0.01)

    app_loop.run_until_complete(_test())
    app_loop.close()

    mock_fn.assert_called_once_with("arg1", 42)
    app.call_from_thread.assert_not_called()


def test_schedule_callback_on_background_worker_loop():
    app = MagicMock()
    main_app_loop = asyncio.new_event_loop()
    app._loop = main_app_loop

    bridge = AgentBridge(app)
    mock_fn = MagicMock()

    # Run in a background thread with its own independent event loop
    def _worker():
        worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(worker_loop)

        async def _run():
            bridge._schedule_callback(mock_fn, "bg_arg")

        worker_loop.run_until_complete(_run())
        worker_loop.close()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    main_app_loop.close()
    app.call_from_thread.assert_called_once_with(mock_fn, "bg_arg")


def test_schedule_callback_without_running_loop():
    app = MagicMock()
    app._loop = None
    bridge = AgentBridge(app)
    mock_fn = MagicMock()

    # Direct call with no event loop
    bridge._schedule_callback(mock_fn, "no_loop_arg")
    app.call_from_thread.assert_called_once_with(mock_fn, "no_loop_arg")
