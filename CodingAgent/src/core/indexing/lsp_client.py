"""lsp_client.py — Async LSP client (S2-A).

Wraps a language-server subprocess over JSON-RPC 2.0 stdio.  One
``LSPClient`` instance is created per language per workspace by
``LSPManager``.

Lifecycle::

    client = LSPClient(server_cmd=["pylsp"], workspace_root=Path("/code"))
    await client.start()
    diags = await client.get_diagnostics("file:///code/main.py")
    await client.shutdown()

Fallback
--------
If the server binary is absent or crashes during ``start()``, the client
silently degrades to ``_DummyLSPClient`` behaviour — all methods return
empty results.  Tools built on top never raise; they return an info
message instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_INIT_TIMEOUT = 10.0
_REQUEST_TIMEOUT = 15.0
_MAX_AUTO_RESTARTS = 3  # P4-2: stop retrying after this many crashes
_RESTART_BACKOFF_BASE = 2.0  # seconds; doubles each attempt (capped at 30 s)


# ---------------------------------------------------------------------------
# Data classes (mirror LSP types)
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    """A lint / type error from the language server."""

    severity: int  # 1=Error, 2=Warning, 3=Information, 4=Hint
    line: int  # 0-based
    col: int  # 0-based
    message: str
    source: str = ""
    code: str = ""

    @property
    def severity_label(self) -> str:
        return {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
            self.severity, "unknown"
        )


@dataclass
class Location:
    """A source location (file + range)."""

    uri: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass
class DocumentSymbol:
    """A named symbol in a document (function, class, variable …)."""

    name: str
    kind: int  # LSP SymbolKind
    start_line: int
    end_line: int
    detail: str = ""

    @property
    def kind_label(self) -> str:
        _KIND = {
            1: "File",
            2: "Module",
            3: "Namespace",
            4: "Package",
            5: "Class",
            6: "Method",
            7: "Property",
            8: "Field",
            9: "Constructor",
            10: "Enum",
            11: "Interface",
            12: "Function",
            13: "Variable",
            14: "Constant",
            15: "String",
            16: "Number",
            17: "Boolean",
            18: "Array",
            19: "Object",
            20: "Key",
            21: "Null",
            22: "EnumMember",
            23: "Struct",
            24: "Event",
            25: "Operator",
            26: "TypeParameter",
        }
        return _KIND.get(self.kind, f"kind{self.kind}")


@dataclass
class WorkspaceEdit:
    """A set of file edits produced by a rename operation."""

    changes: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=dict
    )  # uri → [TextEdit]


# ---------------------------------------------------------------------------
# _DummyLSPClient — used when no server is available
# ---------------------------------------------------------------------------


class _DummyLSPClient:
    """No-op LSP client.  All methods return empty results immediately."""

    async def start(self) -> None:
        pass

    async def get_diagnostics(self, file_uri: str) -> List[Diagnostic]:
        return []

    def get_cached_diagnostics(self, file_uri: str) -> List[Diagnostic]:
        """CP-10: Sync accessor — always empty for the dummy client."""
        return []

    async def get_hover(self, file_uri: str, line: int, col: int) -> str:
        return ""

    async def get_references(
        self, file_uri: str, line: int, col: int
    ) -> List[Location]:
        return []

    async def get_definition(
        self, file_uri: str, line: int, col: int
    ) -> List[Location]:
        return []

    async def get_symbols(self, file_uri: str) -> List[DocumentSymbol]:
        return []

    async def rename(
        self, file_uri: str, line: int, col: int, new_name: str
    ) -> WorkspaceEdit:
        return WorkspaceEdit()

    async def shutdown(self) -> None:
        pass

    @property
    def available(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# LSPClient — real JSON-RPC 2.0 stdio client
# ---------------------------------------------------------------------------


class LSPClient:
    """Async LSP client wrapping a language server subprocess.

    Uses JSON-RPC 2.0 over stdin/stdout.  One instance per language per
    workspace is managed by ``LSPManager``.

    Parameters
    ----------
    server_cmd:
        Command to start the language server (e.g. ``["pylsp"]``).
    workspace_root:
        Absolute path to the workspace root sent as ``rootUri`` in the
        LSP ``initialize`` request.
    """

    def __init__(self, server_cmd: List[str], workspace_root: Path) -> None:
        self._cmd = server_cmd
        self._root = workspace_root.resolve()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._req_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._started = False
        self._lock = asyncio.Lock()
        # P4-2: auto-restart state
        self._shutting_down: bool = False
        self._restart_count: int = 0
        # CP-10: last-known diagnostics per file URI — populated by both pull
        # (get_diagnostics) and push (textDocument/publishDiagnostics) paths.
        # Accessed synchronously by lsp_context.get_lsp_diagnostics_block().
        self._diagnostics_cache: Dict[str, List[Diagnostic]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return (
            self._started and self._proc is not None and self._proc.returncode is None
        )

    async def start(self) -> None:
        """Start the server subprocess and send the ``initialize`` handshake."""
        if self._started:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._root),
            )
        except (FileNotFoundError, PermissionError) as exc:
            logger.debug("LSPClient: server not found (%s): %s", self._cmd, exc)
            return

        self._reader_task = asyncio.ensure_future(self._reader_loop())

        # initialize
        init_result = await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self._root.as_uri(),
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": False},
                        "hover": {"contentFormat": ["plaintext"]},
                        "references": {},
                        "definition": {},
                        "documentSymbol": {},
                        "rename": {},
                    }
                },
                "workspaceFolders": [
                    {"uri": self._root.as_uri(), "name": self._root.name}
                ],
            },
            timeout=_INIT_TIMEOUT,
        )
        if init_result is not None:
            await self._notify("initialized", {})
            self._started = True
            logger.info("LSPClient: started %s for %s", self._cmd[0], self._root)
        else:
            logger.warning("LSPClient: initialize timed out for %s", self._cmd[0])

    async def get_diagnostics(self, file_uri: str) -> List[Diagnostic]:
        """Return diagnostics for *file_uri* (pull model via textDocument/diagnostic).

        Falls back to an empty list if the server does not support pull
        diagnostics (older servers push via ``textDocument/publishDiagnostics``
        instead — those are handled by the notification handler).

        CP-10: Pull results are stored in ``_diagnostics_cache`` so that
        ``get_cached_diagnostics()`` can return them synchronously later.
        """
        if not self.available:
            return self._diagnostics_cache.get(file_uri, [])
        result = await self._request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": file_uri}},
        )
        if result is None:
            return self._diagnostics_cache.get(file_uri, [])
        items = result.get("items") or []
        diags = [self._parse_diagnostic(d) for d in items]
        self._diagnostics_cache[file_uri] = diags
        return diags

    def get_cached_diagnostics(self, file_uri: str) -> List[Diagnostic]:
        """Return the last-known diagnostics for *file_uri* synchronously.

        CP-10: This is the sync accessor used by ``lsp_context.py`` during
        prompt assembly.  Returns an empty list if no diagnostics have been
        cached yet (i.e. no pull or push has occurred for this URI).
        """
        return list(self._diagnostics_cache.get(file_uri, []))

    async def get_hover(self, file_uri: str, line: int, col: int) -> str:
        if not self.available:
            return ""
        result = await self._request(
            "textDocument/hover",
            {
                "textDocument": {"uri": file_uri},
                "position": {"line": line, "character": col},
            },
        )
        if result is None:
            return ""
        contents = result.get("contents") or {}
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get("value", "")
        if isinstance(contents, list):
            return "\n".join(
                c.get("value", c) if isinstance(c, dict) else str(c) for c in contents
            )
        return ""

    async def get_references(
        self, file_uri: str, line: int, col: int
    ) -> List[Location]:
        if not self.available:
            return []
        result = await self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": file_uri},
                "position": {"line": line, "character": col},
                "context": {"includeDeclaration": True},
            },
        )
        if not result:
            return []
        return [self._parse_location(loc) for loc in result]

    async def get_definition(
        self, file_uri: str, line: int, col: int
    ) -> List[Location]:
        if not self.available:
            return []
        result = await self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": file_uri},
                "position": {"line": line, "character": col},
            },
        )
        if result is None:
            return []
        if isinstance(result, list):
            return [self._parse_location(loc) for loc in result]
        return [self._parse_location(result)]

    async def get_symbols(self, file_uri: str) -> List[DocumentSymbol]:
        if not self.available:
            return []
        result = await self._request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": file_uri}},
        )
        if not result:
            return []
        return [self._parse_symbol(s) for s in result]

    async def rename(
        self, file_uri: str, line: int, col: int, new_name: str
    ) -> WorkspaceEdit:
        if not self.available:
            return WorkspaceEdit()
        result = await self._request(
            "textDocument/rename",
            {
                "textDocument": {"uri": file_uri},
                "position": {"line": line, "character": col},
                "newName": new_name,
            },
        )
        if result is None:
            return WorkspaceEdit()
        changes = result.get("changes") or {}
        return WorkspaceEdit(changes=changes)

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._proc is None:
            return
        try:
            await self._request("shutdown", {})
            await self._notify("exit", {})
        except Exception as exc:
            logger.debug("LSPClient: shutdown request failed: %s", exc)
        if self._reader_task:
            self._reader_task.cancel()
        try:
            self._proc.kill()
        except Exception as exc:
            logger.debug("LSPClient: process kill failed: %s", exc)
        self._started = False

    # ------------------------------------------------------------------
    # Internal JSON-RPC plumbing
    # ------------------------------------------------------------------

    async def _restart(self) -> None:
        """P4-2: Restart the server subprocess after a crash.

        Uses exponential backoff up to ``_MAX_AUTO_RESTARTS`` attempts.
        The restart is a no-op if ``_shutting_down`` is set or if the
        restart ceiling has been reached.
        """
        if self._shutting_down or self._restart_count >= _MAX_AUTO_RESTARTS:
            return

        self._restart_count += 1
        delay = min(_RESTART_BACKOFF_BASE * (2 ** (self._restart_count - 1)), 30.0)
        logger.warning(
            "LSPClient: server crashed (exit %s) — restarting in %.1fs (attempt %d/%d)",
            self._proc.returncode if self._proc else "?",
            delay,
            self._restart_count,
            _MAX_AUTO_RESTARTS,
        )
        await asyncio.sleep(delay)
        if self._shutting_down:
            return
        # Reset state so start() will proceed
        self._started = False
        self._proc = None
        self._reader_task = None
        await self.start()
        if self.available:
            logger.info(
                "LSPClient: restart %d/%d succeeded for %s",
                self._restart_count,
                _MAX_AUTO_RESTARTS,
                self._cmd[0],
            )
        else:
            logger.warning(
                "LSPClient: restart %d/%d failed for %s",
                self._restart_count,
                _MAX_AUTO_RESTARTS,
                self._cmd[0],
            )

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _request(
        self,
        method: str,
        params: Any,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> Optional[Any]:
        req_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, Exception) as exc:
            self._pending.pop(req_id, None)
            logger.debug("LSPClient: request %s timed out or failed: %s", method, exc)
            return None

    async def _notify(self, method: str, params: Any) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, msg: Any) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        body = json.dumps(msg).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        try:
            self._proc.stdin.write(header + body)
            await self._proc.stdin.drain()
        except Exception as exc:
            logger.debug("LSPClient: send error: %s", exc)

    async def _reader_loop(self) -> None:
        """Read JSON-RPC messages from the server stdout and resolve futures."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        reader = self._proc.stdout
        try:
            while True:
                header = b""
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    header += line
                    if header.endswith(b"\r\n\r\n"):
                        break

                content_length = 0
                for part in header.split(b"\r\n"):
                    if part.lower().startswith(b"content-length:"):
                        content_length = int(part.split(b":", 1)[1].strip())

                if content_length == 0:
                    continue

                body = await reader.readexactly(content_length)
                try:
                    msg = json.loads(body)
                except json.JSONDecodeError:
                    continue

                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        error = msg.get("error")
                        if error:
                            fut.set_exception(RuntimeError(str(error)))
                        else:
                            fut.set_result(msg.get("result"))
                elif msg_id is None:
                    # CP-10: Handle server-push notifications.  The most
                    # important one is textDocument/publishDiagnostics which
                    # many servers emit proactively.  Store results in the
                    # cache so get_cached_diagnostics() can surface them
                    # synchronously during prompt assembly.
                    method = msg.get("method", "")
                    if method == "textDocument/publishDiagnostics":
                        params = msg.get("params") or {}
                        uri = params.get("uri", "")
                        raw_diags = params.get("diagnostics") or []
                        if uri:
                            self._diagnostics_cache[uri] = [
                                self._parse_diagnostic(d) for d in raw_diags
                            ]
        except asyncio.CancelledError:
            logger.debug("LSPClient: reader loop cancelled")
        except Exception as exc:
            logger.debug("LSPClient: reader loop exited: %s", exc)
        finally:
            # Resolve all pending futures so callers don't hang indefinitely
            # when the server process exits or the reader task is cancelled.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("LSP server disconnected"))
            self._pending.clear()
            # P4-2: Schedule auto-restart if the server crashed unexpectedly.
            crashed = (
                self._proc is not None
                and self._proc.returncode is not None
                and not self._shutting_down
                and self._restart_count < _MAX_AUTO_RESTARTS
            )
            if crashed:
                try:
                    asyncio.ensure_future(self._restart())
                except RuntimeError:
                    pass  # no running loop — nothing to schedule

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_diagnostic(d: Dict[str, Any]) -> Diagnostic:
        r = d.get("range", {})
        start = r.get("start", {})
        return Diagnostic(
            severity=d.get("severity", 1),
            line=start.get("line", 0),
            col=start.get("character", 0),
            message=d.get("message", ""),
            source=d.get("source", ""),
            code=str(d.get("code", "")),
        )

    @staticmethod
    def _parse_location(loc: Dict[str, Any]) -> Location:
        r = loc.get("range", {})
        start = r.get("start", {})
        end = r.get("end", {})
        return Location(
            uri=loc.get("uri", ""),
            start_line=start.get("line", 0),
            start_col=start.get("character", 0),
            end_line=end.get("line", 0),
            end_col=end.get("character", 0),
        )

    @staticmethod
    def _parse_symbol(s: Dict[str, Any]) -> DocumentSymbol:
        loc = s.get("location", {}) or {}
        r = loc.get("range", s.get("range", {})) or {}
        start = r.get("start", {}) or {}
        end = r.get("end", {}) or {}
        return DocumentSymbol(
            name=s.get("name", ""),
            kind=s.get("kind", 0),
            start_line=start.get("line", 0),
            end_line=end.get("line", 0),
            detail=s.get("detail", ""),
        )
