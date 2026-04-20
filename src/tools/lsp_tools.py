"""lsp_tools.py — LSP-backed tools for the LLM agent (S2-B).

Six read-only / workspace-write tools that surface language-server
intelligence to the model:

- ``lsp_diagnostics`` — lint / type errors for a file
- ``lsp_references``  — all references to a symbol
- ``lsp_definition``  — go-to-definition
- ``lsp_symbols``     — list all symbols in a file
- ``lsp_hover``       — type info / docs at a position
- ``lsp_rename``      — rename symbol across workspace

All tools degrade gracefully: when no language server is installed the
function returns a human-readable ``"LSP unavailable"`` message rather
than raising.

Registration
------------
These tools are registered in ``toolsets/coding.yaml`` and
``toolsets/debug.yaml``.  Add them to the orchestrator's
``example_registry()`` via::

    from src.tools.lsp_tools import register_lsp_tools
    register_lsp_tools(registry)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_UNAVAILABLE_MSG = (
    "LSP unavailable — install a language server for this file type "
    "(see src/config/lsp_servers.yaml for setup instructions)."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _get_manager(working_dir: Optional[str] = None):
    """Return the LSPManager for the given working directory."""
    from src.core.indexing.lsp_manager import get_lsp_manager

    root = Path(working_dir) if working_dir else Path.cwd()
    return get_lsp_manager(workspace=root)


def _ok(output: str) -> Dict[str, Any]:
    return {"ok": True, "output": output}


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "output": msg}


# ---------------------------------------------------------------------------
# lsp_diagnostics
# ---------------------------------------------------------------------------


async def lsp_diagnostics(
    path: str,
    working_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Get lint / type errors for *path* from the language server.

    Parameters
    ----------
    path:
        Absolute or workspace-relative file path.
    working_dir:
        Workspace root (used to select the LSP server).

    Returns
    -------
    dict
        ``{"ok": True, "output": "<formatted diagnostics>"}`` or
        ``{"ok": True, "output": "No issues found."}`` when clean.
    """
    try:
        mgr = _get_manager(working_dir)
        client = await mgr.get_client_for_file(path)
        if not client.available:
            return _ok(_UNAVAILABLE_MSG)
        uri = _path_to_uri(path)
        diags = await client.get_diagnostics(uri)
        if not diags:
            return _ok("No diagnostics — file looks clean.")
        lines = []
        for d in diags:
            lines.append(
                f"  [{d.severity_label.upper()}] line {d.line + 1}:{d.col + 1} "
                f"{d.message}" + (f" ({d.source})" if d.source else "")
            )
        return _ok(f"Diagnostics for {path}:\n" + "\n".join(lines))
    except Exception as exc:
        logger.debug("lsp_diagnostics: error: %s", exc)
        return _ok(_UNAVAILABLE_MSG)


# ---------------------------------------------------------------------------
# lsp_references
# ---------------------------------------------------------------------------


async def lsp_references(
    path: str,
    line: int,
    col: int,
    working_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Find all references to the symbol at *line*/*col* in *path*.

    Parameters
    ----------
    path:
        File containing the symbol.
    line:
        0-based line number.
    col:
        0-based column number.
    """
    try:
        mgr = _get_manager(working_dir)
        client = await mgr.get_client_for_file(path)
        if not client.available:
            return _ok(_UNAVAILABLE_MSG)
        uri = _path_to_uri(path)
        refs = await client.get_references(uri, line, col)
        if not refs:
            return _ok("No references found.")
        lines = [f"  {r.uri}:{r.start_line + 1}:{r.start_col + 1}" for r in refs]
        return _ok(f"References ({len(refs)}):\n" + "\n".join(lines))
    except Exception as exc:
        logger.debug("lsp_references: error: %s", exc)
        return _ok(_UNAVAILABLE_MSG)


# ---------------------------------------------------------------------------
# lsp_definition
# ---------------------------------------------------------------------------


async def lsp_definition(
    path: str,
    line: int,
    col: int,
    working_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Go to the definition of the symbol at *line*/*col* in *path*.

    Parameters
    ----------
    path:
        File containing the symbol.
    line:
        0-based line number.
    col:
        0-based column number.
    """
    try:
        mgr = _get_manager(working_dir)
        client = await mgr.get_client_for_file(path)
        if not client.available:
            return _ok(_UNAVAILABLE_MSG)
        uri = _path_to_uri(path)
        locs = await client.get_definition(uri, line, col)
        if not locs:
            return _ok("No definition found.")
        lines = [
            f"  {loc.uri}:{loc.start_line + 1}:{loc.start_col + 1}" for loc in locs
        ]
        return _ok("Definition:\n" + "\n".join(lines))
    except Exception as exc:
        logger.debug("lsp_definition: error: %s", exc)
        return _ok(_UNAVAILABLE_MSG)


# ---------------------------------------------------------------------------
# lsp_symbols
# ---------------------------------------------------------------------------


async def lsp_symbols(
    path: str,
    working_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """List all symbols (functions, classes, variables) in *path*.

    Parameters
    ----------
    path:
        File to inspect.
    """
    try:
        mgr = _get_manager(working_dir)
        client = await mgr.get_client_for_file(path)
        if not client.available:
            return _ok(_UNAVAILABLE_MSG)
        uri = _path_to_uri(path)
        syms = await client.get_symbols(uri)
        if not syms:
            return _ok("No symbols found.")
        lines = [
            f"  {s.kind_label:12} {s.name}  (line {s.start_line + 1}–{s.end_line + 1})"
            + (f" — {s.detail}" if s.detail else "")
            for s in syms
        ]
        return _ok(f"Symbols in {path} ({len(syms)}):\n" + "\n".join(lines))
    except Exception as exc:
        logger.debug("lsp_symbols: error: %s", exc)
        return _ok(_UNAVAILABLE_MSG)


# ---------------------------------------------------------------------------
# lsp_hover
# ---------------------------------------------------------------------------


async def lsp_hover(
    path: str,
    line: int,
    col: int,
    working_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Get type information and documentation for the symbol at *line*/*col*.

    Parameters
    ----------
    path:
        File containing the symbol.
    line:
        0-based line number.
    col:
        0-based column number.
    """
    try:
        mgr = _get_manager(working_dir)
        client = await mgr.get_client_for_file(path)
        if not client.available:
            return _ok(_UNAVAILABLE_MSG)
        uri = _path_to_uri(path)
        text = await client.get_hover(uri, line, col)
        if not text:
            return _ok("No hover information available.")
        return _ok(f"Hover at {path}:{line + 1}:{col + 1}:\n{text}")
    except Exception as exc:
        logger.debug("lsp_hover: error: %s", exc)
        return _ok(_UNAVAILABLE_MSG)


# ---------------------------------------------------------------------------
# lsp_rename
# ---------------------------------------------------------------------------


async def lsp_rename(
    path: str,
    line: int,
    col: int,
    new_name: str,
    working_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename the symbol at *line*/*col* and all its references across the workspace.

    Parameters
    ----------
    path:
        File containing the symbol to rename.
    line:
        0-based line number.
    col:
        0-based column number.
    new_name:
        New name for the symbol.
    """
    try:
        mgr = _get_manager(working_dir)
        client = await mgr.get_client_for_file(path)
        if not client.available:
            return _ok(_UNAVAILABLE_MSG)
        uri = _path_to_uri(path)
        edit = await client.rename(uri, line, col, new_name)
        if not edit.changes:
            return _ok("No rename edits produced — symbol may not support renaming.")

        # Apply the edits to disk
        total_edits = 0
        edited_files = []
        for file_uri, text_edits in edit.changes.items():
            try:
                file_path = Path(file_uri.replace("file://", ""))
                if not file_path.exists():
                    continue
                content = file_path.read_text(encoding="utf-8")
                lines = content.splitlines(keepends=True)
                # Apply edits in reverse order to preserve line numbers
                for te in sorted(
                    text_edits,
                    key=lambda e: (
                        e["range"]["start"]["line"],
                        e["range"]["start"]["character"],
                    ),
                    reverse=True,
                ):
                    r = te["range"]
                    sl, sc = r["start"]["line"], r["start"]["character"]
                    el, ec = r["end"]["line"], r["end"]["character"]
                    new_text = te.get("newText", "")
                    if sl == el and sl < len(lines):
                        line_str = lines[sl]
                        lines[sl] = line_str[:sc] + new_text + line_str[ec:]
                file_path.write_text("".join(lines), encoding="utf-8")
                total_edits += len(text_edits)
                edited_files.append(str(file_path))
            except Exception as file_exc:
                logger.debug(
                    "lsp_rename: failed to apply edit to %s: %s", file_uri, file_exc
                )

        return _ok(
            f"Renamed to '{new_name}' in {len(edited_files)} file(s) ({total_edits} edit(s)):\n"
            + "\n".join(f"  {f}" for f in edited_files)
        )
    except Exception as exc:
        logger.debug("lsp_rename: error: %s", exc)
        return _ok(_UNAVAILABLE_MSG)


# ---------------------------------------------------------------------------
# Tool schema definitions (used by ToolRegistry.register)
# ---------------------------------------------------------------------------

LSP_TOOL_SCHEMAS = [
    {
        "name": "lsp_diagnostics",
        "description": "Get lint/type errors for a file from the language server.",
        "permission": "read_only",
        "permission_kind": "LSP",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to check."},
            },
            "required": ["path"],
        },
        "fn": lsp_diagnostics,
    },
    {
        "name": "lsp_references",
        "description": "Find all references to the symbol at the given line/column.",
        "permission": "read_only",
        "permission_kind": "LSP",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File containing the symbol.",
                },
                "line": {"type": "integer", "description": "0-based line number."},
                "col": {"type": "integer", "description": "0-based column number."},
            },
            "required": ["path", "line", "col"],
        },
        "fn": lsp_references,
    },
    {
        "name": "lsp_definition",
        "description": "Go to the definition of the symbol at the given line/column.",
        "permission": "read_only",
        "permission_kind": "LSP",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File containing the symbol.",
                },
                "line": {"type": "integer", "description": "0-based line number."},
                "col": {"type": "integer", "description": "0-based column number."},
            },
            "required": ["path", "line", "col"],
        },
        "fn": lsp_definition,
    },
    {
        "name": "lsp_symbols",
        "description": "List all symbols (functions, classes, variables) in a file.",
        "permission": "read_only",
        "permission_kind": "LSP",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to inspect.",
                },
            },
            "required": ["path"],
        },
        "fn": lsp_symbols,
    },
    {
        "name": "lsp_hover",
        "description": "Get type information and documentation for symbol at position.",
        "permission": "read_only",
        "permission_kind": "LSP",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File containing the symbol.",
                },
                "line": {"type": "integer", "description": "0-based line number."},
                "col": {"type": "integer", "description": "0-based column number."},
            },
            "required": ["path", "line", "col"],
        },
        "fn": lsp_hover,
    },
    {
        "name": "lsp_rename",
        "description": "Rename a symbol and all its references across the workspace.",
        "permission": "workspace_write",
        "permission_kind": "LSPWrite",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File containing the symbol.",
                },
                "line": {"type": "integer", "description": "0-based line number."},
                "col": {"type": "integer", "description": "0-based column number."},
                "new_name": {
                    "type": "string",
                    "description": "New name for the symbol.",
                },
            },
            "required": ["path", "line", "col", "new_name"],
        },
        "fn": lsp_rename,
    },
]


def register_lsp_tools(registry: Any) -> None:
    """Register all LSP tools into *registry*.

    ``registry`` must have a ``register(name, fn, schema)`` method that
    matches the ``ToolRegistry`` interface in orchestrator.py.
    """
    for schema in LSP_TOOL_SCHEMAS:
        try:
            fn = schema["fn"]
            name = schema["name"]
            registry.register(name, fn, schema)
            logger.debug("lsp_tools: registered %s", name)
        except Exception as exc:
            logger.warning(
                "lsp_tools: failed to register %s: %s", schema.get("name"), exc
            )
