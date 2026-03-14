from typing import Dict, Any
from pathlib import Path
import difflib


def generate_patch(path: str, new_content: str, workdir: Path) -> Dict[str, Any]:
    """Generate a unified diff patch between existing file content and new_content.
    Returns {'status':'ok','patch': '...'} or error.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            p = workdir / p
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


def apply_patch(path: str, patch: str, workdir: Path) -> Dict[str, Any]:
    """Apply a unified diff patch to a file by delegating to file_tools.edit_file logic.
    This function is a thin wrapper meant to be registered as a tool.
    """
    try:
        from src.tools import file_tools

        return file_tools.edit_file(path=path, patch=patch, workdir=workdir)
    except Exception as e:
        return {"status": "error", "error": str(e)}


def edit_code_block(
    path: str, block_to_find: str, new_block: str, workdir: str
) -> Dict[str, Any]:
    """
    Edits a code block in a file by replacing an existing block with a new one.
    """
    try:
        workdir_path = Path(workdir)
        file_path = workdir_path / path
        if not file_path.exists():
            return {"status": "error", "error": f"File not found: {path}"}

        old_content = file_path.read_text()

        if block_to_find not in old_content:
            return {
                "status": "error",
                "error": f"Block to find not found in file: {path}",
            }

        new_content = old_content.replace(block_to_find, new_block)

        from src.tools import file_tools

        return file_tools.write_file(
            path=path, content=new_content, workdir=workdir_path
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}
