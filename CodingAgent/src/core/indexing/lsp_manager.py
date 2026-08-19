"""lsp_manager.py — Singleton LSP server manager (S2-A).

``LSPManager`` maintains one ``LSPClient`` instance per language per workspace,
with lazy start (server started on first tool call) and auto-detection of
language from file extension.

Usage::

    from src.core.indexing.lsp_manager import get_lsp_manager

    mgr = get_lsp_manager(workspace=Path("/code"))
    client = await mgr.get_client("python")
    diags = await client.get_diagnostics("file:///code/main.py")

If no server is installed for the language the manager silently returns a
``_DummyLSPClient`` (all methods return empty results).

v2 Phase 3: CPU-aware concurrency limits (max 2 concurrent LSPs for 6-core CPUs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Dict, Optional

from src.core.indexing.lsp_client import LSPClient, _DummyLSPClient

logger = logging.getLogger(__name__)


def _get_default_max_lsps() -> int:
    """Get default max concurrent LSPs based on CPU cores."""
    cores = os.cpu_count() or 4
    if cores <= 6:
        return 2
    if cores <= 12:
        return 3
    return 4


# Map file extension → language key (used in lsp_servers.yaml)
_EXT_LANGUAGE: Dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}

_CONFIG_PATH = Path(__file__).parents[2] / "config" / "lsp_servers.yaml"


def _load_server_config() -> Dict:
    """Load lsp_servers.yaml; return empty dict on error."""
    try:
        import yaml  # type: ignore[import]

        if _CONFIG_PATH.exists():
            return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("lsp_manager: failed to load lsp_servers.yaml: %s", exc)
    # Minimal built-in fallback so yaml dependency is optional
    return {
        "python": {"cmd": ["pylsp"], "fallback_cmds": [["pyright", "--stdio"]]},
        "typescript": {"cmd": ["typescript-language-server", "--stdio"]},
        "go": {"cmd": ["gopls"]},
        "rust": {"cmd": ["rust-analyzer"]},
    }


def _find_cmd(entry: Dict) -> Optional[list]:
    """Return the first available server command from *entry*, or None."""
    candidates = [entry.get("cmd", [])] + entry.get("fallback_cmds", [])
    for cmd in candidates:
        if cmd and shutil.which(cmd[0]):
            return cmd
    return None


class LSPManager:
    """Manages one LSPClient per language for a given workspace root.

    Clients are started lazily on the first ``get_client()`` call.

    v2 Phase 3: CPU-aware concurrency limits (max 2 for 6-core, 3 for 12-core, 4 for more).

    Parameters
    ----------
    workspace:
        Absolute path to the workspace root.
    max_concurrent:
        Maximum number of concurrent LSP servers (default: CPU-aware auto).
    """

    def __init__(
        self,
        workspace: Path,
        max_concurrent: Optional[int] = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._clients: Dict[str, LSPClient | _DummyLSPClient] = {}
        self._lock = asyncio.Lock()
        self._config = _load_server_config()
        self._max_concurrent = max_concurrent or _get_default_max_lsps()
        self._active_count = 0
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def get_client(self, language: str) -> LSPClient | _DummyLSPClient:
        """Return a started LSP client for *language*.

        Returns ``_DummyLSPClient`` when no server binary is found.
        """
        async with self._lock:
            if language in self._clients:
                existing = self._clients[language]
                # P4-2: if the cached client is a real LSPClient that is no longer
                # available (crashed) and hasn't already scheduled a restart, try
                # starting it again so the caller gets a live client.
                if (
                    isinstance(existing, LSPClient)
                    and not existing.available
                    and not existing._shutting_down
                ):
                    existing._started = False
                    await existing.start()
                return self._clients[language]

            entry = self._config.get(language, {})
            cmd = _find_cmd(entry) if entry else None

            if cmd:
                client: LSPClient | _DummyLSPClient = LSPClient(
                    server_cmd=cmd,
                    workspace_root=self._workspace,
                )
                await client.start()
                if not client.available:
                    logger.debug(
                        "LSPManager: server started but unavailable for %s — falling back",
                        language,
                    )
                    client = _DummyLSPClient()
            else:
                logger.debug(
                    "LSPManager: no server found for language %r — using dummy",
                    language,
                )
                client = _DummyLSPClient()

            self._clients[language] = client
            return client

    def get_semaphore(self) -> asyncio.Semaphore:
        """Get or create a semaphore for concurrency control."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        return self._semaphore

    async def get_client_for_file(self, path: str) -> LSPClient | _DummyLSPClient:
        """Return a client inferred from the file extension of *path*."""
        ext = Path(path).suffix.lower()
        language = _EXT_LANGUAGE.get(ext, "")
        if not language:
            return _DummyLSPClient()
        return await self.get_client(language)

    async def shutdown_all(self) -> None:
        """Shut down all running LSP servers."""
        async with self._lock:
            for client in self._clients.values():
                if isinstance(client, LSPClient):
                    try:
                        await client.shutdown()
                    except Exception as exc:
                        logger.debug("lsp_manager: error shutting down LSP client: %s", exc)
            self._clients.clear()

    @staticmethod
    def language_for_file(path: str) -> str:
        """Return the language key for *path* based on its extension."""
        return _EXT_LANGUAGE.get(Path(path).suffix.lower(), "")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_MANAGERS: Dict[str, LSPManager] = {}
_MANAGER_LOCK: asyncio.Lock | None = None
# ARCH-audit fix: a threading.Lock guards _MANAGERS for the synchronous
# get_lsp_manager() path, which may be called from non-async contexts or
# from multiple threads concurrently.
_MANAGERS_THREAD_LOCK: threading.Lock = threading.Lock()


def _get_manager_lock() -> asyncio.Lock:
    global _MANAGER_LOCK
    if _MANAGER_LOCK is None:
        _MANAGER_LOCK = asyncio.Lock()
    return _MANAGER_LOCK


def get_lsp_manager(workspace: Optional[Path] = None) -> LSPManager:
    """Return the singleton ``LSPManager`` for *workspace*.

    Uses ``Path.cwd()`` when *workspace* is None.

    This helper is safe for both single-threaded and multi-threaded use.
    For concurrent async callers prefer ``get_lsp_manager_async`` to avoid
    holding the threading lock across await points.
    """
    root = (workspace or Path.cwd()).resolve()
    key = str(root)
    # Fast path: read under the lock to avoid TOCTOU race between
    # the "key not in _MANAGERS" check and the insert.
    with _MANAGERS_THREAD_LOCK:
        if key not in _MANAGERS:
            _MANAGERS[key] = LSPManager(workspace=root)
        return _MANAGERS[key]


async def get_lsp_manager_async(workspace: Optional[Path] = None) -> LSPManager:
    """Async-safe version of ``get_lsp_manager`` for concurrent callers.

    SEC-1: Uses the module-level asyncio.Lock to prevent a race condition where
    two coroutines both observe ``key not in _MANAGERS`` and each create a
    separate ``LSPManager`` instance — causing duplicate LSP server processes.

    C2: Fast-path dict reads also hold _MANAGERS_THREAD_LOCK so they don't
    race with concurrent sync ``get_manager`` calls from other threads.
    """
    root = (workspace or Path.cwd()).resolve()
    key = str(root)
    # Fast path: atomic dict read under thread lock (no await inside this block)
    with _MANAGERS_THREAD_LOCK:
        if key in _MANAGERS:
            return _MANAGERS[key]
    # Slow path: coordinate creation with async lock
    async with _get_manager_lock():
        # Double-check: re-read under thread lock inside async lock so the
        # check and the insert are atomic with respect to sync get_manager.
        with _MANAGERS_THREAD_LOCK:
            if key not in _MANAGERS:
                _MANAGERS[key] = LSPManager(workspace=root)
            return _MANAGERS[key]
