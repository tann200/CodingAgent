"""tests/unit/test_config_watcher.py — ROB-2: ConfigWatcher unit tests.

Tests cover:
- start() returns False (no-op) when watchfiles is absent
- start() returns True and launches a daemon thread when watchfiles is present
- stop() sets _stop_flag so the watch loop exits cleanly
- add_callback() registers callbacks called by _on_change()
- reload callbacks receive the changed-paths set
- callback exceptions are swallowed (do not propagate)
- event_bus.publish() is called with correct args on change
- event_bus.publish() exceptions are swallowed
- constructor reload_callbacks kwarg registers callbacks immediately
- _on_change() calls all registered callbacks
- start() is idempotent (second call is a no-op when thread is alive)
- start() after stop() re-starts the thread
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.config_loader import ConfigWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_watcher(
    tmp_path: Path,
    event_bus: Any = None,
    reload_callbacks=None,
) -> ConfigWatcher:
    return ConfigWatcher(
        working_dir=tmp_path,
        event_bus=event_bus,
        reload_callbacks=reload_callbacks,
    )


# ---------------------------------------------------------------------------
# Availability / no-op behaviour when watchfiles is absent
# ---------------------------------------------------------------------------


class TestStartNoWatchfiles:
    def test_start_returns_false_without_watchfiles(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        with patch.object(watcher, "_available", False):
            result = watcher.start()
        assert result is False

    def test_no_thread_started_without_watchfiles(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        with patch.object(watcher, "_available", False):
            watcher.start()
        assert watcher._thread is None

    def test_check_watchfiles_returns_bool(self, tmp_path: Path) -> None:
        result = ConfigWatcher._check_watchfiles()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# start() / stop() with watchfiles present (mocked)
# ---------------------------------------------------------------------------


class TestStartStop:
    def _start_with_fake_watchfiles(self, watcher: ConfigWatcher) -> None:
        """Patch _watch_loop to a no-op so the thread exits immediately."""
        with patch.object(watcher, "_available", True):
            with patch.object(watcher, "_watch_loop", return_value=None):
                watcher.start()

    def test_start_returns_true_when_available(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        with patch.object(watcher, "_available", True):
            with patch.object(watcher, "_watch_loop", return_value=None):
                result = watcher.start()
        assert result is True

    def test_start_creates_daemon_thread(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        with patch.object(watcher, "_available", True):
            with patch.object(watcher, "_watch_loop", return_value=None):
                watcher.start()
        assert watcher._thread is not None
        assert isinstance(watcher._thread, threading.Thread)
        assert watcher._thread.daemon is True

    def test_start_idempotent_when_thread_alive(self, tmp_path: Path) -> None:
        """Calling start() twice should not spawn a second thread."""
        watcher = _make_watcher(tmp_path)
        barrier = threading.Event()

        def _blocking_watch_loop() -> None:
            barrier.wait(timeout=5)

        with patch.object(watcher, "_available", True):
            with patch.object(watcher, "_watch_loop", side_effect=_blocking_watch_loop):
                watcher.start()
                first_thread = watcher._thread
                watcher.start()  # second call
                second_thread = watcher._thread
        barrier.set()  # unblock the thread
        assert first_thread is second_thread

    def test_stop_sets_stop_flag(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        assert watcher._stop_flag is False
        watcher.stop()
        assert watcher._stop_flag is True

    def test_stop_flag_cleared_on_restart(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        watcher.stop()
        assert watcher._stop_flag is True
        # Re-start clears the flag
        with patch.object(watcher, "_available", True):
            with patch.object(watcher, "_watch_loop", return_value=None):
                watcher.start()
        assert watcher._stop_flag is False

    def test_thread_name_is_config_watcher(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        with patch.object(watcher, "_available", True):
            with patch.object(watcher, "_watch_loop", return_value=None):
                watcher.start()
        assert watcher._thread is not None
        assert watcher._thread.name == "config-watcher"


# ---------------------------------------------------------------------------
# add_callback / constructor reload_callbacks
# ---------------------------------------------------------------------------


class TestCallbacks:
    def test_add_callback_registers(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        cb = MagicMock()
        watcher.add_callback(cb)
        assert cb in watcher._callbacks

    def test_constructor_reload_callbacks(self, tmp_path: Path) -> None:
        cb = MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[cb])
        assert cb in watcher._callbacks

    def test_multiple_callbacks_all_registered(self, tmp_path: Path) -> None:
        cb1, cb2, cb3 = MagicMock(), MagicMock(), MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[cb1, cb2])
        watcher.add_callback(cb3)
        assert cb1 in watcher._callbacks
        assert cb2 in watcher._callbacks
        assert cb3 in watcher._callbacks


# ---------------------------------------------------------------------------
# _on_change() behaviour
# ---------------------------------------------------------------------------


class TestOnChange:
    def test_on_change_calls_single_callback(self, tmp_path: Path) -> None:
        cb = MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[cb])
        changed = {"/path/to/config.json"}
        watcher._on_change(changed)
        cb.assert_called_once_with(changed)

    def test_on_change_calls_all_callbacks(self, tmp_path: Path) -> None:
        cb1, cb2 = MagicMock(), MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[cb1, cb2])
        changed = {"/a.json", "/b.json"}
        watcher._on_change(changed)
        cb1.assert_called_once_with(changed)
        cb2.assert_called_once_with(changed)

    def test_on_change_callback_receives_changed_paths(self, tmp_path: Path) -> None:
        received: list = []
        watcher = _make_watcher(tmp_path, reload_callbacks=[received.append])
        paths = {"/cfg/providers.json"}
        watcher._on_change(paths)
        assert received == [paths]

    def test_on_change_swallows_callback_exception(self, tmp_path: Path) -> None:
        def _bad_cb(_):
            raise ValueError("boom")

        cb2 = MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[_bad_cb, cb2])
        # Should not raise
        watcher._on_change({"/x.json"})
        # cb2 must still be called despite _bad_cb raising
        cb2.assert_called_once()

    def test_on_change_no_callbacks_is_safe(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path)
        # Must not raise
        watcher._on_change({"/config.json"})


# ---------------------------------------------------------------------------
# EventBus integration via _on_change()
# ---------------------------------------------------------------------------


class TestEventBus:
    def test_on_change_publishes_event(self, tmp_path: Path) -> None:
        bus = MagicMock()
        watcher = _make_watcher(tmp_path, event_bus=bus)
        paths = {"/cfg/config.json"}
        watcher._on_change(paths)
        bus.publish.assert_called_once()
        call_args = bus.publish.call_args
        event_name = call_args[0][0]
        payload = call_args[0][1]
        assert event_name == "config.reloaded"
        assert set(payload["changed_paths"]) == paths

    def test_on_change_no_event_bus_is_safe(self, tmp_path: Path) -> None:
        watcher = _make_watcher(tmp_path, event_bus=None)
        # Must not raise
        watcher._on_change({"/config.json"})

    def test_on_change_event_bus_exception_swallowed(self, tmp_path: Path) -> None:
        bus = MagicMock()
        bus.publish.side_effect = RuntimeError("bus error")
        watcher = _make_watcher(tmp_path, event_bus=bus)
        # Must not raise
        watcher._on_change({"/config.json"})

    def test_event_payload_contains_list(self, tmp_path: Path) -> None:
        bus = MagicMock()
        watcher = _make_watcher(tmp_path, event_bus=bus)
        watcher._on_change({"/a.json"})
        payload = bus.publish.call_args[0][1]
        assert isinstance(payload["changed_paths"], list)


# ---------------------------------------------------------------------------
# Watch loop integration (file-system level, optional)
# ---------------------------------------------------------------------------


class TestWatchLoop:
    """Test _watch_loop by injecting a fake watchfiles module into sys.modules."""

    def _make_fake_watchfiles(self, changes_sequence):
        """Return a fake watchfiles module whose watch() yields from changes_sequence."""
        import types

        fake_mod = types.ModuleType("watchfiles")

        def _watch(*paths, stop_event=None):
            yield from changes_sequence

        fake_mod.watch = _watch  # type: ignore[assignment]
        return fake_mod

    def test_watch_loop_respects_stop_flag(self, tmp_path: Path) -> None:
        """_watch_loop invokes callback when _stop_flag is False."""
        cfg_dir = tmp_path / ".agent"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text("{}")

        cb = MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[cb])
        watcher._stop_flag = False

        done = threading.Event()
        fake_mod = self._make_fake_watchfiles([{(1, str(cfg_file))}])

        import sys

        with patch.dict(sys.modules, {"watchfiles": fake_mod}):
            t = threading.Thread(target=watcher._watch_loop, daemon=True)
            t.start()
            t.join(timeout=3)

        # Callback must have been invoked exactly once
        assert cb.call_count == 1

    def test_watch_loop_exits_when_stop_flag_set_before_change(
        self, tmp_path: Path
    ) -> None:
        """If stop_flag is already True, _watch_loop breaks on first change."""
        cfg_dir = tmp_path / ".agent"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "config.json"
        cfg_file.write_text("{}")

        cb = MagicMock()
        watcher = _make_watcher(tmp_path, reload_callbacks=[cb])
        watcher._stop_flag = True  # already stopped before first change

        fake_mod = self._make_fake_watchfiles([{(1, str(cfg_file))}])

        import sys

        with patch.dict(sys.modules, {"watchfiles": fake_mod}):
            t = threading.Thread(target=watcher._watch_loop, daemon=True)
            t.start()
            t.join(timeout=3)

        # stop_flag was True → break before calling callback
        assert cb.call_count == 0
