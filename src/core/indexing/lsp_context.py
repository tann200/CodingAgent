"""LSP-style context injection for the system prompt.

Provides:
- ``get_lsp_context_block(workdir, budget_chars)`` — static symbol index block
- ``get_lsp_diagnostics_block(workdir, files, budget_chars)`` — live diagnostics

The symbol index queries the ``SymbolGraph``; the diagnostics block reads the
synchronous cache populated by ``LSPClient`` from pull/push results.

Both functions are lightweight and never block; they return empty strings when
the feature is disabled, no data is available, or any error occurs.

Feature gate
~~~~~~~~~~~~
Injection is skipped unless the merged config contains::

    "lsp_context": {"enabled": true}

Or the environment variable ``CODINGAGENT_LSP_CONTEXT=1`` is set.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_MAX_SYMBOLS = 50  # symbols to include per language
_DEFAULT_BUDGET = 2_000  # chars budget for the entire block


def _is_enabled() -> bool:
    if os.environ.get("CODINGAGENT_LSP_CONTEXT") == "1":
        return True
    try:
        from src.core.config_loader import get as _cfg_get

        lsp_cfg = _cfg_get("lsp_context") or {}
        return bool(lsp_cfg.get("enabled", False))
    except Exception:
        return False


def _get_symbols(workdir: Path) -> List[Dict[str, Any]]:
    """Return up to ``_MAX_SYMBOLS`` top-level symbols from the SymbolGraph."""
    try:
        from src.core.indexing.symbol_graph import SymbolGraph

        sg = SymbolGraph(workdir=str(workdir))
        nodes = sg.nodes  # dict: symbol_id -> node dict
        symbols = []
        for _, node in nodes.items():
            kind = node.get("type", "")
            name = node.get("name", "")
            file_path = node.get("file", "")
            line = node.get("line", 0)
            if name and kind in ("function", "class", "method"):
                symbols.append(
                    {
                        "name": name,
                        "kind": kind,
                        "file": file_path,
                        "line": line,
                    }
                )
        # Sort: classes first, then functions, by file then name
        symbols.sort(
            key=lambda s: (0 if s["kind"] == "class" else 1, s["file"], s["name"])
        )
        return symbols[:_MAX_SYMBOLS]
    except Exception as exc:
        logger.debug("lsp_context: symbol fetch failed: %s", exc)
        return []


def get_lsp_context_block(
    workdir: Optional[Path] = None,
    budget_chars: int = _DEFAULT_BUDGET,
) -> str:
    """Return a fenced LSP context block for the system prompt.

    Returns an empty string when:
    - The feature is disabled (no config flag / env var).
    - The symbol index is absent or empty.
    - An unexpected error occurs.

    Parameters
    ----------
    workdir:
        Project root.  Defaults to ``Path.cwd()``.
    budget_chars:
        Maximum characters for the returned block.  The symbol list is
        truncated to fit.

    Returns
    -------
    str
        ``<lsp_context>\\n...\\n</lsp_context>`` or ``""``.
    """
    if not _is_enabled():
        return ""

    if workdir is None:
        workdir = Path.cwd()

    try:
        symbols = _get_symbols(workdir)
        if not symbols:
            return ""

        lines: List[str] = []
        for sym in symbols:
            entry = f"  {sym['kind']} {sym['name']} ({sym['file']}:{sym['line']})"
            lines.append(entry)

        body = "\n".join(lines)
        block = f"<lsp_context>\n{body}\n</lsp_context>"

        if len(block) > budget_chars:
            block = block[:budget_chars] + "\n... [lsp_context truncated]</lsp_context>"

        return block
    except Exception as exc:
        logger.debug("lsp_context: get_lsp_context_block failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# CP-10: Live diagnostics block
# ---------------------------------------------------------------------------

_DEFAULT_DIAG_BUDGET = 2_000  # chars
_MAX_DIAG_FILES = 10  # cap on number of files shown
_MAX_DIAG_PER_FILE = 20  # cap on diagnostics shown per file
# Only surface errors and warnings by default (severity 1 and 2).
_DIAG_SEVERITY_THRESHOLD = 2


def get_lsp_diagnostics_block(
    workdir: Optional[Path] = None,
    files: Optional[Sequence[str]] = None,
    budget_chars: int = _DEFAULT_DIAG_BUDGET,
) -> str:
    """Return a fenced diagnostics block for the system prompt.

    Reads the synchronous ``_diagnostics_cache`` on every live
    ``LSPClient`` instance managed by ``LSPManager``.  This cache is
    populated by:

    - ``get_diagnostics()`` pull calls (on explicit tool use)
    - ``textDocument/publishDiagnostics`` server-push notifications

    This function is **synchronous** and **non-blocking** — it never starts
    a language server or awaits a coroutine.  If no diagnostics have been
    cached yet it returns an empty string.

    Parameters
    ----------
    workdir:
        Workspace root.  Defaults to ``Path.cwd()``.
    files:
        Optional explicit list of file paths to include.  When omitted all
        URIs present in the cache are used.
    budget_chars:
        Maximum characters for the returned block.

    Returns
    -------
    str
        ``<lsp_diagnostics>\\n...\\n</lsp_diagnostics>`` or ``""``.
    """
    if not _is_enabled():
        return ""

    if workdir is None:
        workdir = Path.cwd()

    try:
        from src.core.indexing.lsp_manager import get_lsp_manager  # type: ignore[import]

        mgr = get_lsp_manager(workspace=workdir)

        # Collect all cached clients (synchronous attribute access — no await)
        all_clients = list(getattr(mgr, "_clients", {}).values())
        if not all_clients:
            return ""

        # Build URI → diagnostics mapping from all clients
        combined: Dict[str, list] = {}
        for client in all_clients:
            cache: Dict[str, list] = getattr(client, "_diagnostics_cache", {})
            for uri, diags in cache.items():
                combined.setdefault(uri, []).extend(diags)

        if not combined:
            return ""

        # Filter to requested files if specified
        if files:
            file_uris = {Path(f).resolve().as_uri() for f in files}
            combined = {k: v for k, v in combined.items() if k in file_uris}

        if not combined:
            return ""

        lines: List[str] = []
        file_count = 0
        for uri, diags in sorted(combined.items()):
            if file_count >= _MAX_DIAG_FILES:
                break
            # Filter to errors/warnings only
            relevant = [
                d
                for d in diags
                if getattr(d, "severity", 1) <= _DIAG_SEVERITY_THRESHOLD
            ]
            if not relevant:
                continue
            # Try to make path relative to workdir for readability
            try:
                display_path = str(
                    Path(uri.replace("file://", "")).relative_to(workdir)
                )
            except ValueError:
                display_path = uri.replace("file://", "")

            file_lines: List[str] = []
            for d in relevant[:_MAX_DIAG_PER_FILE]:
                label = getattr(d, "severity_label", "error").upper()
                line_no = getattr(d, "line", 0) + 1
                col_no = getattr(d, "col", 0) + 1
                message = getattr(d, "message", "")
                source = getattr(d, "source", "")
                suffix = f" ({source})" if source else ""
                file_lines.append(
                    f"  [{label}] {display_path}:{line_no}:{col_no} {message}{suffix}"
                )

            if file_lines:
                lines.extend(file_lines)
                file_count += 1

        if not lines:
            return ""

        body = "\n".join(lines)
        block = f"<lsp_diagnostics>\n{body}\n</lsp_diagnostics>"

        if len(block) > budget_chars:
            block = (
                block[:budget_chars]
                + "\n... [lsp_diagnostics truncated]</lsp_diagnostics>"
            )

        return block

    except Exception as exc:
        logger.debug("lsp_context: get_lsp_diagnostics_block failed: %s", exc)
        return ""
