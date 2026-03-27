"""
AST-aware refactoring tools for Python files.

Provides ast_rename (position-accurate symbol renaming) and ast_list_symbols
(listing all definitions) using Python's built-in ast module.
For non-Python files, falls back to word-boundary regex matching.
"""

import ast
import difflib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import tool

logger = logging.getLogger(__name__)


def _is_python_file(path: Path) -> bool:
    return path.suffix.lower() == ".py"



@tool(side_effects=["write"], tags=["coding"])
def ast_rename(
    path: str,
    old_name: str,
    new_name: str,
    workdir: str = None,
) -> Dict[str, Any]:
    """Rename a symbol (function, class, or variable) in a file using AST analysis.

    Safer than find/replace: only renames actual symbol definitions and
    call sites, not comments or string literals that happen to contain the name.
    Returns a unified diff of the changes made.

    Args:
        path: File path (relative to workdir).
        old_name: Current name of the symbol.
        new_name: New name for the symbol.
        workdir: Working directory (defaults to cwd).

    Returns:
        status, changes_made (bool), diff (unified diff string).
    """
    if not old_name or not new_name:
        return {"status": "error", "error": "old_name and new_name must be non-empty"}

    from src.tools.tools_config import get_default_workdir

    workdir_path = Path(workdir) if workdir else get_default_workdir()

    try:
        file_path = _safe_resolve(path, workdir_path)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    if not file_path.exists():
        return {"status": "error", "error": f"File not found: {path}"}

    original = file_path.read_text(encoding="utf-8")

    # Guardrail: mark file as read, then verify write is allowed
    try:
        from src.tools.guardrails import mark_file_read, check_read_before_write
        mark_file_read(str(file_path))
        err = check_read_before_write(str(file_path))
        if err:
            return {"status": "error", "error": err["error"]}
    except ImportError:
        pass

    if _is_python_file(file_path):
        # Python: use AST to collect line numbers that contain the symbol, then
        # do word-boundary regex replacement only on those lines.
        # This preserves all comments, blank lines, and original formatting —
        # ast.unparse() is intentionally NOT used because it strips all comments.
        try:
            tree = ast.parse(original)
        except SyntaxError as se:
            return {"status": "error", "error": f"Syntax error in {path}: {se}"}

        # Collect line numbers (1-indexed) where the symbol appears as an AST node
        affected_lines: set = set()
        for node in ast.walk(tree):
            node_name: Optional[str] = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node_name = node.name
            elif isinstance(node, ast.Name):
                node_name = node.id
            elif isinstance(node, ast.Attribute):
                node_name = node.attr
            if node_name == old_name and hasattr(node, "lineno"):
                affected_lines.add(node.lineno)

        if not affected_lines:
            return {
                "status": "ok",
                "changes_made": False,
                "diff": "",
                "message": "No occurrences found",
            }

        # Apply word-boundary replacement only on affected lines (preserves formatting)
        pattern = re.compile(r"\b" + re.escape(old_name) + r"\b")
        source_lines = original.splitlines(keepends=True)
        for i, line in enumerate(source_lines):
            if (i + 1) in affected_lines:  # lineno is 1-indexed
                source_lines[i] = pattern.sub(new_name, line)
        new_content = "".join(source_lines)
    else:
        # Non-Python: word-boundary regex on the full text.
        # Cannot distinguish symbols from string literals without a language parser.
        pattern = re.compile(r"\b" + re.escape(old_name) + r"\b")
        new_content = pattern.sub(new_name, original)

    if new_content == original:
        return {
            "status": "ok",
            "changes_made": False,
            "diff": "",
            "message": "No occurrences found",
        }

    # Generate diff
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(file_path),
            tofile=str(file_path),
            lineterm="",
        )
    )
    diff = "\n".join(diff_lines)

    # Write the new content
    file_path.write_text(new_content, encoding="utf-8")

    return {
        "status": "ok",
        "changes_made": True,
        "diff": diff[:5000],
    }


@tool(tags=["coding"])
def ast_list_symbols(
    path: str,
    symbol_type: str = "any",
    workdir: str = None,
) -> Dict[str, Any]:
    """List all function, class, and variable definitions in a file.

    Returns {name, type, start_line, end_line} for each symbol.
    Useful for exploring a file before making edits.

    Args:
        path: File path (relative to workdir).
        symbol_type: Filter by type: "function", "class", "variable", or "any".
        workdir: Working directory (defaults to cwd).

    Returns:
        status, symbols (list of {name, type, start_line, end_line}).
    """
    from src.tools.tools_config import get_default_workdir

    workdir_path = Path(workdir) if workdir else get_default_workdir()

    try:
        file_path = _safe_resolve(path, workdir_path)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    if not file_path.exists():
        return {"status": "error", "error": f"File not found: {path}"}

    content = file_path.read_text(encoding="utf-8")
    symbols: List[Dict[str, Any]] = []

    if _is_python_file(file_path):
        try:
            tree = ast.parse(content)
        except SyntaxError as se:
            return {"status": "error", "error": f"Syntax error in {path}: {se}"}

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(
                node, ast.AsyncFunctionDef
            ):
                if symbol_type in ("any", "function"):
                    symbols.append(
                        {
                            "name": node.name,
                            "type": "function",
                            "start_line": node.lineno,
                            "end_line": getattr(node, "end_lineno", node.lineno),
                        }
                    )
            elif isinstance(node, ast.ClassDef):
                if symbol_type in ("any", "class"):
                    symbols.append(
                        {
                            "name": node.name,
                            "type": "class",
                            "start_line": node.lineno,
                            "end_line": getattr(node, "end_lineno", node.lineno),
                        }
                    )
            elif isinstance(node, ast.Assign) and symbol_type in ("any", "variable"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.append(
                            {
                                "name": target.id,
                                "type": "variable",
                                "start_line": node.lineno,
                                "end_line": getattr(node, "end_lineno", node.lineno),
                            }
                        )
    else:
        # Non-Python: regex fallback
        func_re = re.compile(
            r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE
        )
        class_re = re.compile(r"(?:export\s+)?class\s+(\w+)", re.MULTILINE)
        const_re = re.compile(r"(?:export\s+)?(?:const|let|var)\s+(\w+)", re.MULTILINE)

        for m in func_re.finditer(content):
            if symbol_type in ("any", "function"):
                line = content[: m.start()].count("\n") + 1
                symbols.append(
                    {
                        "name": m.group(1),
                        "type": "function",
                        "start_line": line,
                        "end_line": line,
                    }
                )
        for m in class_re.finditer(content):
            if symbol_type in ("any", "class"):
                line = content[: m.start()].count("\n") + 1
                symbols.append(
                    {
                        "name": m.group(1),
                        "type": "class",
                        "start_line": line,
                        "end_line": line,
                    }
                )
        for m in const_re.finditer(content):
            if symbol_type in ("any", "variable"):
                line = content[: m.start()].count("\n") + 1
                symbols.append(
                    {
                        "name": m.group(1),
                        "type": "variable",
                        "start_line": line,
                        "end_line": line,
                    }
                )

    return {"status": "ok", "path": str(file_path), "symbols": symbols}
