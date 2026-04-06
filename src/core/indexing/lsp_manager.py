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
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Dict, Optional

from src.core.indexing.lsp_client import LSPClient, _DummyLSPClient

logger = logging.getLogger(__name__)

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
    except Exception:
        pass
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

    Parameters
    ----------
    workspace:
        Absolute path to the workspace root.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        self._clients: Dict[str, LSPClient | _DummyLSPClient] = {}
        self._lock = asyncio.Lock()
        self._config = _load_server_config()

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
                    except Exception:
                        pass
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


def _get_manager_lock() -> asyncio.Lock:
    global _MANAGER_LOCK
    if _MANAGER_LOCK is None:
        _MANAGER_LOCK = asyncio.Lock()
    return _MANAGER_LOCK


def get_lsp_manager(workspace: Optional[Path] = None) -> LSPManager:
    """Return the singleton ``LSPManager`` for *workspace*.

    Uses ``Path.cwd()`` when *workspace* is None.

    SEC-1: This synchronous helper is intentionally kept for callers that cannot
    use ``await``.  It is safe for **single-threaded** or **startup** use only.
    For concurrent async callers use ``get_lsp_manager_async`` instead.
    """
    root = (workspace or Path.cwd()).resolve()
    key = str(root)
    if key not in _MANAGERS:
        _MANAGERS[key] = LSPManager(workspace=root)
    return _MANAGERS[key]


async def get_lsp_manager_async(workspace: Optional[Path] = None) -> LSPManager:
    """Async-safe version of ``get_lsp_manager`` for concurrent callers.

    SEC-1: Uses the module-level asyncio.Lock to prevent a race condition where
    two coroutines both observe ``key not in _MANAGERS`` and each create a
    separate ``LSPManager`` instance — causing duplicate LSP server processes.
    """
    root = (workspace or Path.cwd()).resolve()
    key = str(root)
    if key in _MANAGERS:
        return _MANAGERS[key]
    async with _get_manager_lock():
        # Double-checked locking: re-check inside the lock in case another
        # coroutine created the manager while we were waiting.
        if key not in _MANAGERS:
            _MANAGERS[key] = LSPManager(workspace=root)
        return _MANAGERS[key]
