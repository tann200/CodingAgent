"""Configuration hot-reload utilities for CodingAgent.

This module provides functionality to automatically detect changes to
configuration files (user config, workspace configs, providers, tools) and
reload configuration hierarchies dynamically.
Inspired by OpenClaw's ConfigReloader implementation.

Features:
- File system watching for active configuration file modifications
- Re-entrancy protection against recursive loading during initialization
- Thread-safe configuration updates
- Dirty flag and callback invocation for hot reloading
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Global singleton
_config_reloader: Optional["ConfigReloader"] = None


def get_config_reloader(
    initial_load: bool = False,
    workdir: Optional[Path] = None,
) -> "ConfigReloader":
    """Get the singleton ConfigReloader instance."""
    global _config_reloader
    if _config_reloader is None:
        _config_reloader = ConfigReloader(initial_load=initial_load, workdir=workdir)
    return _config_reloader


class ConfigReloader:
    """Manages automatic reloading of configuration files.

    This component watches for changes to configuration files in the active
    hierarchy and triggers a reload when changes are detected.
    """

    def __init__(
        self,
        initial_load: bool = False,
        workdir: Optional[Path] = None,
    ) -> None:
        self._workdir = Path(workdir) if workdir else None
        self._initialized = False
        self._pending_reload = False
        self._is_loading = False
        self._last_hashes: Dict[str, str] = {}

        # Setup watcher thread
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_watcher = threading.Event()
        # Callbacks invoked on reload/change. Each callback receives a set of
        # changed paths (set[str]) or None when invoked via explicit load().
        self._callbacks: list[Callable[[Optional[set]], None]] = []

        if initial_load:
            self.load()
        else:
            self._last_hashes = self._calculate_hashes()
            self._initialized = True

        # Start watcher thread
        self._setup_watcher()

    def _setup_watcher(self) -> None:
        """Set up the file watcher thread."""
        if self._watcher_thread is None:
            self._watcher_thread = threading.Thread(
                target=self._watcher_loop, daemon=True, name="config-reloader"
            )
            self._watcher_thread.start()

    def _get_watched_paths(self) -> List[Path]:
        """Collect all active configuration files across user and workspace layers."""
        paths: List[Path] = []
        try:
            from src.core.paths import get_user_config_path

            user_cfg = get_user_config_path()
            if user_cfg.exists():
                paths.append(user_cfg)
        except Exception:
            pass

        try:
            ac_dir: Optional[Path] = None
            if self._workdir:
                try:
                    from src.tools.tools_config import agent_context_path

                    ac_dir = agent_context_path(self._workdir)
                except Exception:
                    ac_dir = self._workdir / ".codingAgent"
            else:
                try:
                    from src.core.paths import get_agent_context_dir

                    ac_dir = get_agent_context_dir()
                except Exception:
                    pass

            if ac_dir and ac_dir.exists():
                for name in (
                    "config.json",
                    "config.local.json",
                    "providers.json",
                    "tools.json",
                    "config.user.yaml",
                ):
                    p = ac_dir / name
                    if p.exists() and p not in paths:
                        paths.append(p)
        except Exception as exc:
            logger.debug("ConfigReloader: failed to resolve workspace paths: %s", exc)

        # Fallback to config.user.yaml in cwd if present
        cwd_yaml = Path.cwd() / "config.user.yaml"
        if cwd_yaml.exists() and cwd_yaml not in paths:
            paths.append(cwd_yaml)

        return paths

    def _calculate_hashes(self) -> Dict[str, str]:
        """Calculate content hashes for all currently existing watched files."""
        hashes: Dict[str, str] = {}
        for p in self._get_watched_paths():
            try:
                if p.is_file():
                    content = p.read_bytes()
                    hashes[str(p.resolve())] = hashlib.sha256(content).hexdigest()[:16]
            except Exception:
                pass
        return hashes

    def _check_for_changes(self) -> Set[str]:
        """Compare current watched file hashes to _last_hashes and return changed paths."""
        new_hashes = self._calculate_hashes()
        changed: Set[str] = set()

        for p_str, h in new_hashes.items():
            if self._last_hashes.get(p_str) != h:
                changed.add(p_str)

        for p_str in self._last_hashes:
            if p_str not in new_hashes:
                changed.add(p_str)

        if changed:
            self._last_hashes = new_hashes
        return changed

    def _watcher_loop(self) -> None:
        """Main watcher loop that monitors config file changes."""
        if not self._last_hashes:
            self._last_hashes = self._calculate_hashes()

        while not self._stop_watcher.is_set():
            try:
                time.sleep(1)
                if self._stop_watcher.is_set():
                    break

                changed = self._check_for_changes()
                if changed:
                    logger.info(
                        "Configuration file(s) changed: %s - triggering reload", changed
                    )
                    self._pending_reload = True
                    self._invoke_callbacks(changed)

            except Exception as e:
                logger.debug("Error in config watcher: %s", e)

    def _file_hash(self, path: Path) -> str:
        """Calculate a stable hash of the config file content."""
        try:
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        except Exception:
            return "0"

    def _file_hash_changed(self, path: Path) -> bool:
        """Check if the config file hash has changed since last check."""
        current_hash = self._file_hash(path)
        last_hash = self._last_hashes.get(str(path.resolve()))
        if current_hash and current_hash != last_hash:
            self._last_hashes[str(path.resolve())] = current_hash
            return True
        return False

    def changed(self) -> bool:
        """Check if configuration has changed pending reload."""
        if self._pending_reload:
            return True
        # Synchronous fallback check
        changed = self._check_for_changes()
        if changed:
            self._pending_reload = True
            return True
        return False

    def clear_changed(self) -> None:
        """Reset the pending reload flag."""
        self._pending_reload = False

    def load(self) -> Dict[str, Any]:
        """Force a reload of the configuration with re-entrancy protection."""
        if self._is_loading:
            return {}
        self._is_loading = True
        try:
            from src.core.config_loader import load_merged_config

            config = load_merged_config(working_dir=self._workdir)

            if self._initialized:
                logger.info("Configuration loaded successfully")
            else:
                logger.info("Configuration initialized and loaded successfully")

            self._initialized = True
            self._pending_reload = False
            self._last_hashes = self._calculate_hashes()

            try:
                import src.core.config_loader as _cl
                with _cl._config_lock:
                    _cl._cached_config = config
                    _cl._last_load_time = time.time()
            except Exception:
                pass

            self._invoke_callbacks(None)
            return config
        except Exception as e:
            logger.error("Failed to load configuration: %s", e)
            raise
        finally:
            self._is_loading = False

    def reload_if_pending(self) -> Optional[Dict[str, Any]]:
        """Check if reload is pending and execute it if so."""
        if self.changed():
            logger.info("Executing pending configuration reload")
            try:
                cfg = self.load()
                return cfg
            except Exception as e:
                logger.error("Failed to reload configuration: %s", e)
            finally:
                self._pending_reload = False
        return None

    def add_callback(self, fn: Callable[[Optional[set]], None]) -> None:
        """Register a callback invoked when configuration is reloaded."""
        try:
            if callable(fn) and fn not in self._callbacks:
                self._callbacks.append(fn)
        except Exception as e:
            logger.debug("config_reloader: failed to add callback: %s", e)

    def remove_callback(self, fn: Callable[[Optional[set]], None]) -> None:
        """Remove a previously registered callback (no-op if absent)."""
        try:
            if fn in self._callbacks:
                self._callbacks.remove(fn)
        except Exception as e:
            logger.debug("config_reloader: failed to remove callback: %s", e)

    def _invoke_callbacks(self, changed_paths: Optional[set]) -> None:
        """Safely invoke registered callbacks with the provided changed_paths."""
        for cb in list(self._callbacks):
            try:
                cb(changed_paths)
            except Exception as exc:
                logger.warning("config_reloader: callback error: %s", exc)

    def stop(self) -> None:
        """Stop the watcher thread."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._stop_watcher.set()
            self._watcher_thread.join(timeout=2.0)

    def _get_config_path(self) -> Path:
        """Get primary configuration file path (legacy compatibility helper)."""
        watched = self._get_watched_paths()
        if watched:
            return watched[0]
        try:
            from src.core.paths import get_agent_context_dir

            return get_agent_context_dir() / "config.json"
        except Exception:
            return Path.cwd() / ".codingAgent" / "config.json"
