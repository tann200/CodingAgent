from typing import Dict, Any
from pathlib import Path
import difflib

from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import tool


def generate_unified_diff(
    old_content: str,
    new_content: str,
    from_file: str = "a",
    to_file: str = "b",
) -> str:
    """Return a unified diff string comparing old_content to new_content.

    TASK-14: Standalone helper (not a registered tool) so that
    ``edit_file_atomic`` and ``edit_by_line_range`` can include a diff payload
    in their results without duplicating ``difflib`` boilerplate.

    Args:
        old_content: Original file content.
        new_content: Modified file content.
        from_file:   Label for the "a" side of the diff (typically the file path).
        to_file:     Label for the "b" side of the diff (typically the same path).

    Returns:
        A unified diff string, or an empty string if the contents are identical.
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="\n",
        )
    )
    return "".join(diff_lines)


@tool(tags=["coding"])
def generate_patch(path: str, new_content: str, workdir: Path) -> Dict[str, Any]:
    """Generate a unified diff patch between existing file content and new_content.
    Returns {'status':'ok','patch': '...'} or error.
    """
    try:
        try:
            p = _safe_resolve(path, workdir)
        except PermissionError as pe:
            return {"status": "error", "error": str(pe)}
        if not p.exists():
            return {"status": "error", "error": "file not found"}
        old = p.read_text(encoding="utf-8").splitlines(keepends=True)
        new = new_content.splitlines(keepends=True)
        patch_lines = list(
            difflib.unified_diff(old, new, fromfile=str(p), tofile=str(p), lineterm="")
        )
        patch = "\n".join(patch_lines)
        return {"status": "ok", "patch": patch}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(side_effects=["write"], tags=["coding"])
def apply_patch(path: str, patch: str, workdir: Path) -> Dict[str, Any]:
    """Apply a unified diff patch to a file by delegating to file_tools.edit_file logic.
    This function is a thin wrapper meant to be registered as a tool.
    """
    try:
        from src.tools import file_tools

        return file_tools.edit_file(path=path, patch=patch, workdir=workdir)
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(side_effects=["write"], tags=["coding"])
def edit_code_block(
    path: str, block_to_find: str, new_block: str, workdir: str
) -> Dict[str, Any]:
    """
    Edits a code block in a file by replacing an existing block with a new one.
    block_to_find must appear exactly once — ambiguous matches are rejected.
    """
    try:
        from src.tools import file_tools

        workdir_path = Path(workdir)

        # C1: Path safety — use _safe_resolve to prevent path traversal
        try:
            file_path = file_tools._safe_resolve(path, workdir_path)
        except PermissionError as e:
            return {"status": "error", "error": str(e)}

        if not file_path.exists():
            return {"status": "not_found", "error": f"File not found: {path}"}

        old_content = file_path.read_text(encoding="utf-8")

        # Mark as read so write_file guardrail is satisfied
        try:
            from src.tools.guardrails import mark_file_read

            mark_file_read(str(file_path.resolve()))
        except Exception:
            pass

        # C2: Uniqueness check — reject if 0 or >1 occurrences
        count = old_content.count(block_to_find)
        if count == 0:
            return {
                "status": "error",
                "error": f"block_to_find not found in file: {path}",
            }
        if count > 1:
            return {
                "status": "error",
                "error": f"block_to_find appears {count} times in {path}. "
                "Provide more context to make it unique.",
            }

        new_content = old_content.replace(block_to_find, new_block, 1)

        return file_tools.write_file(
            path=path, content=new_content, workdir=workdir_path
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}
