"""inline_tool.py — TASK-TUI-1

Per-tool inline widgets with distinct icons and status text.

Each entry in ``TOOL_RENDER_MAP`` provides:
  - ``icon``    — ASCII glyph shown before the tool name (terminal-safe, no emoji)
  - ``pending`` — Text template shown while the tool call is in progress.
                  Supports ``{path}``, ``{cmd}``, ``{desc}``, ``{pat}``, ``{n}``, ``{agent}``
  - ``done``    — Text template shown after the tool call completes.

Usage::

    render = TOOL_RENDER_MAP.get(tool_name, FALLBACK_RENDER)
    pending_text = render.format_pending(args)
    done_text    = render.format_done(args, result)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ToolRender:
    """Rendering configuration for a single tool type."""

    icon: str
    pending: str
    done: str

    def format_pending(self, args: Dict[str, Any]) -> str:
        """Interpolate *args* into the pending template (best-effort)."""
        return _render(self.pending, args, result=None)

    def format_done(self, args: Dict[str, Any], result: Any = None) -> str:
        """Interpolate *args* (and optional *result*) into the done template."""
        return _render(self.done, args, result=result)

    def label_pending(self, args: Dict[str, Any]) -> str:
        """Return the full pending label: ``{icon} {pending}``."""
        return f"{self.icon} {self.format_pending(args)}"

    def label_done(self, args: Dict[str, Any], result: Any = None) -> str:
        """Return the full done label: ``{icon} {done}``."""
        return f"{self.icon} {self.format_done(args, result)}"


# ---------------------------------------------------------------------------
# Tool render registry
# ---------------------------------------------------------------------------

TOOL_RENDER_MAP: Dict[str, ToolRender] = {
    # File I/O
    "read_file": ToolRender(
        icon="->",
        pending="Reading ...",
        done="Read {path}",
    ),
    "write_file": ToolRender(
        icon="<-",
        pending="Writing ...",
        done="Write {path}",
    ),
    "edit_file": ToolRender(
        icon="<-",
        pending="Editing ...",
        done="Edit {path}",
    ),
    "edit_by_line_range": ToolRender(
        icon="<-",
        pending="Editing lines ...",
        done="Edit {path}:{start_line}-{end_line}",
    ),
    "apply_patch": ToolRender(
        icon="<-",
        pending="Applying patch ...",
        done="Patch {path}",
    ),
    # Shell
    "bash": ToolRender(
        icon="#",
        pending="{desc}",
        done="$ {cmd}",
    ),
    # Search / navigate
    "glob_tool": ToolRender(
        icon="*",
        pending='Glob "{pat}" ...',
        done='Glob "{pat}" ({n} matches)',
    ),
    "grep_tool": ToolRender(
        icon="*",
        pending='Grep "{pat}" ...',
        done='Grep "{pat}" ({n} matches)',
    ),
    "list_dir": ToolRender(
        icon="->",
        pending="Listing {path} ...",
        done="List {path}",
    ),
    # Memory
    "memory_search": ToolRender(
        icon="?",
        pending='Searching memory for "{query}" ...',
        done='Memory search "{query}"',
    ),
    "memory_save": ToolRender(
        icon="*",
        pending="Saving to memory ...",
        done="Saved memory entry",
    ),
    # Task management
    "manage_todo": ToolRender(
        icon="@",
        pending="Updating todos ...",
        done="Todos updated",
    ),
    # Delegation / subagents
    "delegate_task": ToolRender(
        icon="|",
        pending="Delegating ...",
        done="{agent} Task -- {desc}",
    ),
    # Web
    "webfetch": ToolRender(
        icon="~",
        pending="Fetching {url} ...",
        done="Fetched {url}",
    ),
    "websearch": ToolRender(
        icon="~",
        pending='Searching "{query}" ...',
        done='Search "{query}"',
    ),
    # Git
    "git_status": ToolRender(icon="#", pending="git status ...", done="git status"),
    "git_diff": ToolRender(icon="#", pending="git diff ...", done="git diff {path}"),
    "git_log": ToolRender(icon="#", pending="git log ...", done="git log"),
    "git_commit": ToolRender(icon="#", pending="git commit ...", done="git commit"),
    "git_stash": ToolRender(icon="#", pending="git stash ...", done="git stash"),
    "git_restore": ToolRender(icon="#", pending="git restore ...", done="git restore {path}"),
    # LSP
    "lsp_diagnostics": ToolRender(
        icon="!",
        pending="Checking diagnostics {path} ...",
        done="Diagnostics {path}",
    ),
    "lsp_references": ToolRender(
        icon="->",
        pending="Finding references ...",
        done="References for {symbol}",
    ),
    "lsp_definition": ToolRender(
        icon="->",
        pending="Go to definition ...",
        done="Definition of {symbol}",
    ),
    # Formatter
    "run_formatter": ToolRender(
        icon="@",
        pending="Formatting {path} ...",
        done="Formatted {path}",
    ),
}

#: Used when a tool name is not found in ``TOOL_RENDER_MAP``.
FALLBACK_RENDER = ToolRender(
    icon="@",
    pending="{tool_name} ...",
    done="{tool_name}",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ARG_ALIASES: Dict[str, list[str]] = {
    "path": ["path", "file_path", "src_path", "dst_path", "target", "filename"],
    "cmd":  ["command", "cmd", "bash_command", "script"],
    "desc": ["description", "desc", "task", "title"],
    "pat":  ["pattern", "pat", "glob", "query"],
    "url":  ["url", "uri", "endpoint"],
    "query": ["query", "search_query", "q"],
    "agent": ["agent", "role", "agent_role"],
    "n":    ["count", "n", "num_results"],
    "symbol": ["symbol", "name", "identifier"],
    "start_line": ["start_line", "start"],
    "end_line": ["end_line", "end"],
    "tool_name": ["tool_name", "name"],
}


def _resolve_arg(key: str, args: Dict[str, Any]) -> str:
    """Try each alias for *key* in *args*; return empty string if none found."""
    for alias in _ARG_ALIASES.get(key, [key]):
        if alias in args:
            val = args[alias]
            if val is not None:
                s = str(val)
                # Truncate long values to keep labels readable
                return s if len(s) <= 60 else s[:57] + "..."
    return ""


def _render(template: str, args: Dict[str, Any], result: Any) -> str:
    """Fill all ``{key}`` placeholders in *template* using *args*."""
    # Collect all placeholder keys from the template
    import re
    keys = re.findall(r"\{(\w+)\}", template)
    replacements: Dict[str, str] = {}
    for key in keys:
        val = _resolve_arg(key, args)
        if not val and result is not None:
            # Try extracting from result dict if available
            if isinstance(result, dict):
                val = str(result.get(key, ""))
        replacements[key] = val or f"<{key}>"
    try:
        return template.format(**replacements)
    except (KeyError, ValueError):
        return template
