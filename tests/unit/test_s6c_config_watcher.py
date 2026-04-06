"""tests/unit/test_s6c_config_watcher.py — Tests for S6-C ConfigWatcher hot-reload.

Since `watchfiles` is an optional dependency that may not be installed in CI,
all tests that exercise the watch loop use mocking. Tests also verify the
graceful no-op path when watchfiles is absent.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestConfigWatcherInit:
    def test_construction_without_working_dir(self):
        from src.core.config_loader import ConfigWatcher
        w = ConfigWatcher()
        assert w._working_dir is not None
        assert w._callbacks == []

    def test_construction_with_working_dir(self, tmp_path):
        from src.core.config_loader import ConfigWatcher
        w = ConfigWatcher(working_dir=tmp_path)
        assert w._working_dir == tmp_path

    def test_construction_with_event_bus(self):
        from src.core.config_loader import ConfigWatcher
        mock_bus = MagicMock()
        w = ConfigWatcher(event_bus=mock_bus)
        assert w._event_bus is mock_bus

    def test_add_callback(self):
        from src.core.config_loader import ConfigWatcher
        w = ConfigWatcher()
        cb = MagicMock()
        w.add_callback(cb)
        assert cb in w._callbacks

    def test_construction_with_reload_callbacks(self):
        from src.core.config_loader import ConfigWatcher
        cb1 = MagicMock()
        cb2 = MagicMock()
        w = ConfigWatcher(reload_callbacks=[cb1, cb2])
        assert cb1 in w._callbacks
        assert cb2 in w._callbacks


class TestConfigWatcherAvailability:
    def test_start_returns_false_when_watchfiles_absent(self, tmp_path):
        """Without watchfiles, start() returns False and no thread is started."""
        from src.core.config_loader import ConfigWatcher
        w = ConfigWatcher(working_dir=tmp_path)
        w._available = False
        result = w.start()
        assert result is False
        assert w._thread is None

    def test_start_returns_true_when_watchfiles_present(self, tmp_path):
        """With watchfiles mocked, start() returns True and launches thread."""
        from src.core.config_loader import ConfigWatcher

        w = ConfigWatcher(working_dir=tmp_path)
        w._available = True

        # Mock the watch loop so it exits immediately
        def _immediate_exit():
            pass

        w._watch_loop = _immediate_exit

        import threading as _threading
        with patch.object(_threading, "Thread", wraps=_threading.Thread) as mock_thread_cls:
            result = w.start()

        # stop flag for cleanup
        w._stop_flag = True
        assert result is True

    def test_check_watchfiles_returns_bool(self):
        from src.core.config_loader import ConfigWatcher
        result = ConfigWatcher._check_watchfiles()
        assert isinstance(result, bool)


class TestConfigWatcherOnChange:
    def test_callbacks_called_with_changed_paths(self):
        from src.core.config_loader import ConfigWatcher
        cb = MagicMock()
        w = ConfigWatcher(reload_callbacks=[cb])
        changed = {"/tmp/providers.json"}
        w._on_change(changed)
        cb.assert_called_once_with(changed)

    def test_event_published_when_bus_set(self):
        from src.core.config_loader import ConfigWatcher
        mock_bus = MagicMock()
        w = ConfigWatcher(event_bus=mock_bus)
        changed = {"/tmp/.agent/config.json"}
        w._on_change(changed)
        mock_bus.publish.assert_called_once()
        event_name = mock_bus.publish.call_args[0][0]
        assert event_name == "config.reloaded"

    def test_event_payload_contains_changed_paths(self):
        from src.core.config_loader import ConfigWatcher
        mock_bus = MagicMock()
        w = ConfigWatcher(event_bus=mock_bus)
        changed = {"/tmp/providers.json"}
        w._on_change(changed)
        payload = mock_bus.publish.call_args[0][1]
        assert "changed_paths" in payload
        assert "/tmp/providers.json" in payload["changed_paths"]

    def test_no_bus_no_event_no_crash(self):
        """No event bus → on_change still works, just skips publish."""
        from src.core.config_loader import ConfigWatcher
        cb = MagicMock()
        w = ConfigWatcher(reload_callbacks=[cb])
        w._on_change({"/some/path"})
        cb.assert_called_once()

    def test_callback_exception_does_not_propagate(self):
        """Bad callback raises but _on_change does not crash."""
        from src.core.config_loader import ConfigWatcher

        def _bad_cb(paths):
            raise RuntimeError("callback error")

        good_cb = MagicMock()
        w = ConfigWatcher(reload_callbacks=[_bad_cb, good_cb])
        w._on_change({"/some/path"})
        # The good callback should still be called after the bad one
        good_cb.assert_called_once()

    def test_multiple_callbacks_all_called(self):
        from src.core.config_loader import ConfigWatcher
        cb1 = MagicMock()
        cb2 = MagicMock()
        cb3 = MagicMock()
        w = ConfigWatcher(reload_callbacks=[cb1, cb2, cb3])
        w._on_change({"/tmp/config.json"})
        cb1.assert_called_once()
        cb2.assert_called_once()
        cb3.assert_called_once()


class TestConfigWatcherStop:
    def test_stop_sets_flag(self):
        from src.core.config_loader import ConfigWatcher
        w = ConfigWatcher()
        assert w._stop_flag is False
        w.stop()
        assert w._stop_flag is True
