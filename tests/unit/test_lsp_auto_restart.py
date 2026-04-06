"""Tests for P4-2: LSP server auto-restart on crash.

These tests exercise the restart logic entirely without a real LSP server binary.
They use asyncio.run / pytest-asyncio together with direct state manipulation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.indexing.lsp_client import (
    LSPClient,
    _DummyLSPClient,
    _MAX_AUTO_RESTARTS,
    _RESTART_BACKOFF_BASE,
)
from src.core.indexing.lsp_manager import LSPManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(tmp_path: Path, cmd: list | None = None) -> LSPClient:
    return LSPClient(
        server_cmd=cmd or ["nonexistent-lsp-server"],
        workspace_root=tmp_path,
    )


def _crashed_proc(returncode: int = 1) -> MagicMock:
    """Return a mock process that has already exited with the given returncode."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_shutting_down_false_by_default(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        assert client._shutting_down is False

    def test_restart_count_zero_by_default(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        assert client._restart_count == 0

    def test_max_restarts_constant(self) -> None:
        assert _MAX_AUTO_RESTARTS == 3

    def test_backoff_base_constant(self) -> None:
        assert _RESTART_BACKOFF_BASE == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# shutdown() sets _shutting_down
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_sets_shutting_down(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        # proc is None — shutdown should still flip the flag
        await client.shutdown()
        assert client._shutting_down is True

    @pytest.mark.asyncio
    async def test_shutdown_clears_started_flag(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._started = True  # pretend it was started
        client._proc = _crashed_proc(returncode=None)  # simulate live proc
        await client.shutdown()
        assert client._started is False


# ---------------------------------------------------------------------------
# _restart() logic — no backoff sleep (patched to 0)
# ---------------------------------------------------------------------------


class TestRestartMethod:
    @pytest.mark.asyncio
    async def test_restart_no_op_when_shutting_down(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._shutting_down = True
        client._proc = _crashed_proc()
        start_calls: list = []

        with patch.object(client, "start", side_effect=lambda: start_calls.append(1)):
            with patch("asyncio.sleep", new=AsyncMock()):
                await client._restart()

        assert start_calls == []

    @pytest.mark.asyncio
    async def test_restart_no_op_when_max_restarts_reached(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(tmp_path)
        client._restart_count = _MAX_AUTO_RESTARTS  # already at ceiling
        client._proc = _crashed_proc()
        start_calls: list = []

        with patch.object(client, "start", side_effect=lambda: start_calls.append(1)):
            with patch("asyncio.sleep", new=AsyncMock()):
                await client._restart()

        assert start_calls == []

    @pytest.mark.asyncio
    async def test_restart_increments_count(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._proc = _crashed_proc()

        with patch.object(client, "start", new=AsyncMock()):
            with patch("asyncio.sleep", new=AsyncMock()):
                await client._restart()

        assert client._restart_count == 1

    @pytest.mark.asyncio
    async def test_restart_resets_state_before_start(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._started = True
        client._proc = _crashed_proc()
        client._reader_task = MagicMock()
        captured_started: list = []

        async def _fake_start():
            captured_started.append(client._started)
            captured_started.append(client._proc)
            captured_started.append(client._reader_task)

        with patch.object(client, "start", side_effect=_fake_start):
            with patch("asyncio.sleep", new=AsyncMock()):
                await client._restart()

        assert captured_started[0] is False  # _started reset
        assert captured_started[1] is None  # _proc reset
        assert captured_started[2] is None  # _reader_task reset

    @pytest.mark.asyncio
    async def test_restart_calls_start(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._proc = _crashed_proc()
        start_calls: list = []

        async def _fake_start():
            start_calls.append(True)

        with patch.object(client, "start", side_effect=_fake_start):
            with patch("asyncio.sleep", new=AsyncMock()):
                await client._restart()

        assert len(start_calls) == 1

    @pytest.mark.asyncio
    async def test_restart_sleeps_with_exponential_backoff(
        self, tmp_path: Path
    ) -> None:
        client = _make_client(tmp_path)
        client._proc = _crashed_proc()
        sleep_args: list = []

        async def _fake_sleep(secs: float) -> None:
            sleep_args.append(secs)

        with patch.object(client, "start", new=AsyncMock()):
            with patch("asyncio.sleep", side_effect=_fake_sleep):
                # First restart: delay = 2.0 * 2^0 = 2.0
                await client._restart()

        assert len(sleep_args) == 1
        assert sleep_args[0] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_second_restart_longer_delay(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._proc = _crashed_proc()
        client._restart_count = 1  # simulate one prior restart
        sleep_args: list = []

        async def _fake_sleep(secs: float) -> None:
            sleep_args.append(secs)

        with patch.object(client, "start", new=AsyncMock()):
            with patch("asyncio.sleep", side_effect=_fake_sleep):
                await client._restart()

        # delay = 2.0 * 2^1 = 4.0
        assert sleep_args[0] == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_restart_delay_capped_at_30_seconds(self, tmp_path: Path) -> None:
        import src.core.indexing.lsp_client as _lsp_mod

        client = _make_client(tmp_path)
        client._proc = _crashed_proc()
        # Set count=10; patch ceiling to 20 so the guard passes.
        # Without the min(…, 30.0) cap, delay would be 2.0 * 2^10 = 2048 s.
        client._restart_count = 10
        sleep_args: list = []

        async def _fake_sleep(secs: float) -> None:
            sleep_args.append(secs)

        with patch.object(_lsp_mod, "_MAX_AUTO_RESTARTS", 20):
            with patch.object(client, "start", new=AsyncMock()):
                with patch("asyncio.sleep", side_effect=_fake_sleep):
                    await client._restart()

        assert sleep_args[0] == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_restart_no_op_if_shutting_down_after_sleep(
        self, tmp_path: Path
    ) -> None:
        """If shutdown() is called while sleeping, start() must not be called."""
        client = _make_client(tmp_path)
        client._proc = _crashed_proc()
        start_calls: list = []

        async def _fake_sleep(_: float) -> None:
            client._shutting_down = True  # simulate shutdown during backoff

        with patch.object(client, "start", side_effect=lambda: start_calls.append(1)):
            with patch("asyncio.sleep", side_effect=_fake_sleep):
                await client._restart()

        assert start_calls == []


# ---------------------------------------------------------------------------
# _reader_loop finally block schedules restart on crash
# ---------------------------------------------------------------------------


class TestReaderLoopCrashScheduling:
    @pytest.mark.asyncio
    async def test_crashed_reader_loop_schedules_restart(self, tmp_path: Path) -> None:
        """Simulate a crashed process: _reader_loop returns immediately (empty read).
        Verify _restart() is scheduled via asyncio.ensure_future."""
        client = _make_client(tmp_path)
        # Set up a proc that has already exited
        proc = _crashed_proc(returncode=1)
        # StreamReader that returns b"" immediately (EOF)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        proc.stdout = reader
        client._proc = proc
        client._started = True

        restart_scheduled: list = []
        orig_ensure_future = asyncio.ensure_future

        def _capture_ensure_future(coro, *args, **kwargs):
            restart_scheduled.append(
                coro.__name__ if hasattr(coro, "__name__") else str(coro)
            )
            # Don't actually schedule to avoid asyncio complexity
            coro.close()
            return MagicMock()

        with patch("asyncio.ensure_future", side_effect=_capture_ensure_future):
            await client._reader_loop()

        assert len(restart_scheduled) == 1

    @pytest.mark.asyncio
    async def test_no_restart_scheduled_when_shutting_down(
        self, tmp_path: Path
    ) -> None:
        """_shutting_down=True prevents restart scheduling."""
        client = _make_client(tmp_path)
        proc = _crashed_proc(returncode=1)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        proc.stdout = reader
        client._proc = proc
        client._started = True
        client._shutting_down = True

        restart_scheduled: list = []

        def _capture_ensure_future(coro, *args, **kwargs):
            restart_scheduled.append(True)
            coro.close()
            return MagicMock()

        with patch("asyncio.ensure_future", side_effect=_capture_ensure_future):
            await client._reader_loop()

        assert restart_scheduled == []

    @pytest.mark.asyncio
    async def test_no_restart_when_max_restarts_reached(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        proc = _crashed_proc(returncode=1)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        proc.stdout = reader
        client._proc = proc
        client._restart_count = _MAX_AUTO_RESTARTS  # ceiling reached

        restart_scheduled: list = []

        def _capture_ensure_future(coro, *args, **kwargs):
            restart_scheduled.append(True)
            coro.close()
            return MagicMock()

        with patch("asyncio.ensure_future", side_effect=_capture_ensure_future):
            await client._reader_loop()

        assert restart_scheduled == []

    @pytest.mark.asyncio
    async def test_pending_futures_resolved_on_crash(self, tmp_path: Path) -> None:
        """Pending futures must be set to exception when server crashes."""
        client = _make_client(tmp_path)
        proc = _crashed_proc(returncode=1)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        proc.stdout = reader
        client._proc = proc

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        client._pending[999] = fut

        with patch(
            "asyncio.ensure_future",
            side_effect=lambda c, **kw: (c.close(), MagicMock())[1],
        ):
            await client._reader_loop()

        assert fut.done()
        assert isinstance(fut.exception(), RuntimeError)
        assert "disconnected" in str(fut.exception())


# ---------------------------------------------------------------------------
# LSPManager.get_client re-starts a crashed client
# ---------------------------------------------------------------------------


class TestLSPManagerRestartOnGet:
    @pytest.mark.asyncio
    async def test_manager_restarts_crashed_client_on_get(self, tmp_path: Path) -> None:
        manager = LSPManager(workspace=tmp_path)
        # Inject a dead LSPClient directly into the cache
        dead_client = _make_client(tmp_path)
        dead_client._started = False  # not available
        dead_client._proc = None
        manager._clients["python"] = dead_client

        start_calls: list = []

        async def _fake_start():
            start_calls.append(True)

        with patch.object(dead_client, "start", side_effect=_fake_start):
            returned = await manager.get_client("python")

        assert returned is dead_client
        assert len(start_calls) == 1

    @pytest.mark.asyncio
    async def test_manager_does_not_restart_shutting_down_client(
        self, tmp_path: Path
    ) -> None:
        manager = LSPManager(workspace=tmp_path)
        dead_client = _make_client(tmp_path)
        dead_client._started = False
        dead_client._shutting_down = True
        manager._clients["python"] = dead_client

        start_calls: list = []

        async def _fake_start():
            start_calls.append(True)

        with patch.object(dead_client, "start", side_effect=_fake_start):
            await manager.get_client("python")

        assert start_calls == []

    @pytest.mark.asyncio
    async def test_manager_returns_dummy_unchanged(self, tmp_path: Path) -> None:
        manager = LSPManager(workspace=tmp_path)
        dummy = _DummyLSPClient()
        manager._clients["python"] = dummy

        returned = await manager.get_client("python")
        assert returned is dummy


# ---------------------------------------------------------------------------
# available property
# ---------------------------------------------------------------------------


class TestAvailableProperty:
    def test_not_available_when_not_started(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        assert client.available is False

    def test_not_available_when_proc_is_none(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._started = True
        client._proc = None
        assert client.available is False

    def test_not_available_when_proc_has_returncode(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._started = True
        client._proc = _crashed_proc(returncode=1)
        assert client.available is False

    def test_available_when_started_and_proc_running(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path)
        client._started = True
        proc = MagicMock()
        proc.returncode = None  # still running
        client._proc = proc
        assert client.available is True
