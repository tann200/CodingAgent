"""
tool_result_formatter.py — Pure formatting functions for tool execution results.

Extracted from orchestrator.py (lines 242–603) so the Orchestrator class is not
responsible for display-layer concerns.

All functions are pure (no side effects, no dependencies on Orchestrator state).
They accept a raw tool-result dict and return a human-readable string suitable
for the TUI and the message history.

Public API
----------
    format_tool_result(result, tool_name=None) -> str
        Top-level dispatcher — use this everywhere.

    TOOL_RESULT_FORMATTERS
        Dict mapping tool names to single-argument formatter callables.  Exposed
        for tests and for any code that needs to enumerate supported formatters.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Side-by-side diff helpers
# ---------------------------------------------------------------------------


def _format_side_by_side_diff(unified_diff: str, max_width: int = 80) -> str:
    """Convert a unified diff to a side-by-side format for better readability."""
    if not unified_diff:
        return ""

    lines = unified_diff.strip().split("\n")
    left_lines: List[str] = []
    current_hunk: Dict[str, Any] = {"left": [], "right": [], "header": ""}

    def _fmt(line: str, _is_left: bool) -> str:
        if line.startswith("---") or line.startswith("+++"):
            return line
        if line.startswith("@@"):
            return line
        if line.startswith("-"):
            return f"[-] {line[1:]}"
        if line.startswith("+"):
            return f"[+] {line[1:]}"
        if line.startswith(" "):
            return f"    {line[1:]}"
        return f"    {line}"

    def _render_hunk() -> List[str]:
        if not current_hunk["left"] and not current_hunk["right"]:
            return []
        result: List[str] = []
        if current_hunk["header"]:
            result.append("")
            result.append(current_hunk["header"])
            result.append("")
        left_texts = [_fmt(line, True) for line in current_hunk["left"]]
        right_texts = [_fmt(line, False) for line in current_hunk["right"]]
        max_len = max(len(left_texts), len(right_texts))
        while len(left_texts) < max_len:
            left_texts.append("")
        while len(right_texts) < max_len:
            right_texts.append("")
        sep = "  │  "
        for i in range(max_len):
            left = left_texts[i][:max_width].ljust(max_width)
            right = right_texts[i][:max_width].ljust(max_width)
            result.append(f"{left}{sep}{right}")
        return result

    for line in lines:
        if line.startswith("---") or line.startswith("+++"):
            continue
        elif line.startswith("@@"):
            left_lines.extend(_render_hunk())
            current_hunk = {"left": [], "right": [], "header": line}
        elif line.startswith("-"):
            current_hunk["left"].append(line)
        elif line.startswith("+"):
            current_hunk["right"].append(line)
        elif line.startswith(" "):
            current_hunk["left"].append(line)
            current_hunk["right"].append(line)

    left_lines.extend(_render_hunk())
    return "\n".join(left_lines) if left_lines else unified_diff


# ---------------------------------------------------------------------------
# Per-tool formatters
# ---------------------------------------------------------------------------


def _format_list_files_result(result: Dict[str, Any]) -> str:
    """Format list_files / list_dir results as a markdown bullet list."""
    if not isinstance(result, dict):
        return str(result)
    if "items" in result:
        items = result["items"]
        if not items:
            return "📁 Empty directory"
        lines = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("name", "?")
                marker = "📁" if item.get("is_dir", False) else "📄"
                lines.append(f"- {marker} {name}")
            else:
                lines.append(f"- 📄 {item}")
        return "\n".join(lines)
    return str(result)


def _format_read_file_result(result: Dict[str, Any]) -> str:
    """Format read_file results."""
    if not isinstance(result, dict):
        return str(result)
    if "content" in result:
        path = result.get("path", "unknown")
        truncated = result.get("truncated", False)
        out = f"File: {path}\n"
        if truncated:
            out += "[Content truncated]\n"
        out += result["content"]
        return out
    return str(result)


def _format_glob_result(result: Dict[str, Any]) -> str:
    """Format glob results as a list of matched file paths."""
    if not isinstance(result, dict):
        return str(result)
    matches = result.get("matches")
    if matches is None:
        return str(result)
    if not matches:
        pattern = result.get("pattern", "")
        return f"No files found matching `{pattern}`" if pattern else "No files found"
    pattern = result.get("pattern", "")
    header = (
        f"Found {len(matches)} file(s) matching `{pattern}`:"
        if pattern
        else f"Found {len(matches)} file(s):"
    )
    lines = [header]
    for m in matches:
        lines.append(f"  {m}")
    truncated = result.get("truncated")
    if truncated:
        total = result.get("total_found", "?")
        lines.append(f"  ... (showing {len(matches)} of {total} total)")
    return "\n".join(lines)


def _format_grep_result(result: Dict[str, Any]) -> str:
    """Format grep results."""
    if not isinstance(result, dict):
        return str(result)
    if "matches" in result:
        matches = result["matches"]
        if not matches:
            return "No matches found"
        out = f"Found {len(matches)} match(es):\n"
        for match in matches[:20]:
            if isinstance(match, dict):
                fp = match.get("file_path", "?")
                ln = match.get("line_number", "?")
                ct = match.get("content", "").strip()
                out += f"  {fp}:{ln}: {ct}\n"
            else:
                out += f"  {match}\n"
        if len(matches) > 20:
            out += f"  ... and {len(matches) - 20} more\n"
        return out.strip()
    return str(result)


def _format_search_result(result: Dict[str, Any]) -> str:
    """Format search_code results."""
    if not isinstance(result, dict):
        return str(result)
    if "results" in result:
        results = result["results"]
        if not results:
            return "No results found"
        out = f"Found {len(results)} result(s):\n"
        for r in results[:10]:
            if isinstance(r, dict):
                fp = r.get("file_path", "?")
                ct = r.get("content", "").strip()
                out += f"  📄 {fp}\n"
                if ct:
                    out += f"     {ct[:100]}\n"
            else:
                out += f"  {r}\n"
        return out.strip()
    return str(result)


def _format_symbol_result(result: Dict[str, Any]) -> str:
    """Format find_symbol results."""
    if not isinstance(result, dict):
        return str(result)
    name = result.get("symbol_name", "?")
    fp = result.get("file_path", "?")
    symbol_type = result.get("symbol_type", "symbol")
    line = result.get("start_line", "?")
    return f"Found {symbol_type} `{name}` at {fp}:{line}"


def _format_change_summary(
    tool_result: Dict[str, Any],
    file_path: str,
    is_write: bool = True,
) -> str:
    """Generate a formatted change summary with side-by-side diff."""
    if not isinstance(tool_result, dict):
        return str(tool_result)
    status = tool_result.get("status", "unknown")
    if status != "ok":
        verb = "Write" if is_write else "Edit"
        return f"✗ {verb} failed: {tool_result.get('error', 'Unknown error')}"
    diff = tool_result.get("diff", "")
    lines_added = tool_result.get("lines_added", 0)
    lines_removed = tool_result.get("lines_removed", 0)
    is_new_file = tool_result.get("is_new_file", False)
    lines = []
    if is_new_file:
        lines.append(f"📄 **New file created:** `{file_path}`")
    else:
        prefix = "📝" if is_write else "✏️"
        lines.append(f"{prefix} **File modified:** `{file_path}`")
    if lines_added or lines_removed:
        lines.append(f"   [+{lines_added} / -{lines_removed} lines]")
    if diff:
        lines.append("")
        lines.append("```diff")
        lines.append(diff)
        lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatter registry and top-level dispatcher
# ---------------------------------------------------------------------------

TOOL_RESULT_FORMATTERS: Dict[str, Any] = {
    "list_files": _format_list_files_result,
    "list_dir": _format_list_files_result,
    "glob": _format_glob_result,
    "find": _format_glob_result,
    "find_files": _format_glob_result,
    "read_file": _format_read_file_result,
    "grep": _format_grep_result,
    "search_code": _format_search_result,
    "find_symbol": _format_symbol_result,
    "edit_file": lambda r: _format_change_summary(
        r, r.get("path", "unknown"), is_write=False
    ),
    "edit_file_atomic": lambda r: _format_change_summary(
        r, r.get("path", "unknown"), is_write=False
    ),
    "write_file": lambda r: _format_change_summary(
        r, r.get("path", "unknown"), is_write=True
    ),
}


def format_tool_result(result: Any, tool_name: Optional[str] = None) -> str:
    """Format a tool result for display based on the tool type.

    Parameters
    ----------
    result:
        Raw result from a tool execution (typically a dict).
    tool_name:
        Optional tool name for selecting the specific formatter.

    Returns
    -------
    str
        Human-readable representation of the result.
    """
    if tool_name and tool_name in TOOL_RESULT_FORMATTERS:
        return TOOL_RESULT_FORMATTERS[tool_name](result)

    if isinstance(result, dict):
        for key in ["items", "content", "matches", "results"]:
            if key in result and key in TOOL_RESULT_FORMATTERS:
                return TOOL_RESULT_FORMATTERS[key](result)
        if "diff" in result:
            return f"```diff\n{result['diff']}\n```"
        if "patch" in result:
            return f"```diff\n{result['patch']}\n```"
        status = result.get("status", "ok")
        if status == "ok":
            path = result.get("path", "")
            return f"✓ {path}" if path else "✓ Done"
        else:
            error = result.get("error", "Unknown error")
            return f"✗ {error}"

    return str(result) if result else ""
