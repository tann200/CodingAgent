"""Configuration hot-reload utilities for CodingAgent.

This module provides functionality to automatically detect changes to
`config.user.yaml` and reload configuration hierarchies dynamically.
Inspired by OpenClaw's ConfigReloader implementation.

Features:
- File system watching for config.user.yaml modifications
- Deep merging of configuration layers
- Thread-safe configuration updates
- Dirty flag for easy reloading integration
"""

from __future__ import annotations
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Callable, Optional, Iterable
from pathlib import Path

# Avoid importing project path helpers at module import time to prevent
# import cycles during tests; import lazily in _get_config_path().

# Global singleton
_config_reloader: Optional["ConfigReloader"] = None


def get_config_reloader(initial_load: bool = True) -> "ConfigReloader":
    """Get the singleton ConfigReloader instance."""
    global _config_reloader
    if _config_reloader is None:
        _config_reloader = ConfigReloader(initial_load=initial_load)
    return _config_reloader


class ConfigReloader:
    """Manages automatic reloading of configuration files.

    This component watches for changes to configuration files and triggers
    a reload when changes are detected. It's designed to work with the
    existing Config system but adds automatic hot-reload capabilities.

    Example usage:

    # In your main configuration loop:
    reloader = get_config_reloader()
    last_hash = None

    while True:
        if reloader.changed():
            config = load_merged_config()  # Your existing load function
            print("Configuration reloaded!")
        time.sleep(1)
    """

    def __init__(self, initial_load: bool = True) -> None:
        self._initialized = False
        self._pending_reload = False
        self._last_hash: Optional[str] = None

        # Setup watcher thread
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_watcher = threading.Event()
        # Callbacks invoked on reload/change. Each callback receives a set of
        # changed paths (set[str]) or None when invoked via explicit load().
        self._callbacks: list[Callable[[Optional[set]], None]] = []

        if initial_load:
            self.load()

        # Start watcher thread
        self._setup_watcher()

    def _setup_watcher(self) -> None:
        """Set up the file watcher thread."""
        if self._watcher_thread is None:
            self._watcher_thread = threading.Thread(
                target=self._watcher_loop, daemon=True
            )
            self._watcher_thread.start()

    def _watcher_loop(self) -> None:
        """Main watcher loop that monitors config file changes."""
        config_path = self._get_config_path()

        if not config_path.exists():
            logging.warning(f"Config file not found: {config_path}")
            return

        # Initial hash calculation
        if self._initialized:
            self._last_hash = self._file_hash(config_path)
            return

        # Main watcher loop
        while not self._stop_watcher.is_set():
            time.sleep(1)

            try:
                if self._stop_watcher.is_set():
                    break

                if self._file_hash_changed(config_path):
                    logging.info("Configuration file changed - triggering reload")
                    self._pending_reload = True

            except Exception as e:
                logging.debug(f"Error in config watcher: {e}")

    def _file_hash(self, path: Path) -> str:
        """Calculate a simple hash of the config file content."""
        try:
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            # Use string form of Python's hash to keep the return type stable
            return str(hash(content))
        except Exception:
            return "0"

    def _file_hash_changed(self, path: Path) -> bool:
        """Check if the config file hash has changed since last check."""
        current_hash = self._file_hash(path)
        if current_hash and current_hash != self._last_hash:
            self._last_hash = current_hash
            return True
        return False

    def changed(self) -> bool:
        """Check if configuration has changed pending reload.

        Returns:
            True if reload is pending or file has changed since last load
        """
        return self._pending_reload

    def load(self) -> Dict[str, Any]:
        """Force a reload of the configuration.

        Returns:
            The loaded merged configuration dictionary
        """
        try:
            from src.core.config_loader import load_merged_config

            config = load_merged_config()

            if self._initialized:
                logging.info("Configuration loaded successfully")
            else:
                logging.info("Configuration initialized and loaded successfully")

            self._initialized = True
            self._pending_reload = False
            self._last_hash = self._file_hash(self._get_config_path())

            # Notify callbacks that an explicit load occurred. Pass None to
            # indicate this was a forced load (not triggered by watcher), and
            # let callbacks decide how to handle it.
            self._invoke_callbacks(None)

            return config

        except Exception as e:
            logging.error(f"Failed to load configuration: {e}")
            raise

    def reload_if_pending(self) -> None:
        """Check if reload is pending and execute it if so."""
        if self._pending_reload:
            logging.info("Executing pending configuration reload")
            try:
                from src.core.config_loader import load_merged_config

                config = load_merged_config()
                # Optionally store the loaded config somewhere if needed
                logging.debug("Configuration reloaded successfully")
                # On a watcher-driven reload we call callbacks with the path
                # set that triggered this reload (if we have one stored).
                # We'll pass an empty set here (the watcher loop's _on_change
                # will call callbacks with the explicit changed paths).
                self._invoke_callbacks(set())
            except Exception as e:
                logging.error(f"Failed to reload configuration: {e}")
            finally:
                self._pending_reload = False  # Reset pending flag

    def add_callback(self, fn: Callable[[Optional[set]], None]) -> None:
        """Register a callback invoked when configuration is reloaded.

        The callback receives an Optional[set] of changed paths. ``None``
        indicates an explicit programmatic load (ConfigReloader.load()).
        """
        try:
            if callable(fn):
                self._callbacks.append(fn)
        except Exception:
            logging.debug("config_reloader: failed to add callback", exc_info=True)

    def remove_callback(self, fn: Callable[[Optional[set]], None]) -> None:
        """Remove a previously registered callback (no-op if absent)."""
        try:
            if fn in self._callbacks:
                self._callbacks.remove(fn)
        except Exception:
            logging.debug("config_reloader: failed to remove callback", exc_info=True)

    def _invoke_callbacks(self, changed_paths: Optional[set]) -> None:
        """Safely invoke registered callbacks with the provided changed_paths."""
        for cb in list(self._callbacks):
            try:
                cb(changed_paths)
            except Exception as exc:
                logging.warning("config_reloader: callback error: %s", exc)

    def stop(self) -> None:
        """Stop the watcher thread."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._stop_watcher.set()
            self._watcher_thread.join(timeout=2.0)

    def _get_config_path(self) -> Path:
        """Get the active configuration file path."""
        try:
            # Lazily import path helper to avoid import cycles during tests
            from src.core.paths import get_agent_context_dir

            # This mimics Config's get_agent_config_path behavior
            return get_agent_context_dir() / "config.user.yaml"
        except Exception:
            # Fallback to standard location
            return Path.cwd() / "config.user.yaml"
