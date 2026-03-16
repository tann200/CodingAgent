from __future__ import annotations

from pathlib import Path
from typing import Dict, Any


# Default working directory used by tools (project root /output)
DEFAULT_WORKDIR = Path.cwd() / "output"
DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p
    p = p.resolve()
    # ensure we are inside workdir
    if not str(p).startswith(str(workdir.resolve())):
        raise PermissionError("Path outside working directory is not allowed")
    return p


def write_file(
    path: str, content: str, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p), "status": "ok"}


def read_file(
    path: str, summarize: bool = False, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}
    content = p.read_text(encoding="utf-8")
    if summarize and len(content) > 500:
        lines = content.splitlines()
        if len(lines) > 20:
            summary = (
                f"[{len(lines)} lines, {len(content)} chars] "
                + "\n".join(lines[:10])
                + f"\n... [{len(lines) - 20} more lines]"
            )
        else:
            summary = f"[{len(content)} chars] {content[:500]}..."
        return {"path": str(p), "status": "ok", "content": summary, "truncated": True}
    return {"path": str(p), "status": "ok", "content": content}


def list_dir(path: str = ".", workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    items = []
    for child in p.iterdir():
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"path": str(p), "status": "ok", "items": items}


def delete_file(path: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    try:
        p = _safe_resolve(path, workdir)
        if not p.exists():
            return {"path": str(p), "status": "not_found"}
        deleted_path = str(p)
        if p.is_dir():
            import shutil

            shutil.rmtree(p)
        else:
            p.unlink()
        return {"path": deleted_path, "status": "ok", "deleted": True}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def sandbox_info(workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    return {"workdir": str(workdir.resolve())}


def read_file_chunk(
    path: str, offset: int = 0, limit: int = -1, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    with p.open("r", encoding="utf-8") as f:
        f.seek(offset)
        content = f.read(limit)
        return {
            "path": str(p),
            "status": "ok",
            "content": content,
            "offset": offset,
            "limit": limit,
        }


def edit_file(path: str, patch: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
        patch_file = f.name

    try:
        if not patch.strip().startswith("---") and not patch.strip().startswith("@@"):
            return {
                "path": str(p),
                "status": "error",
                "error": "Invalid patch format. Must be unified diff.",
            }

        # Apply unified diff.
        # Using -f to force (ignore previous patches) and -u (unified)
        result = subprocess.run(
            ["patch", "-u", "-f", str(p), "-i", patch_file],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            return {
                "path": str(p),
                "status": "error",
                "error": f"Patch failed code {result.returncode}:\n{result.stdout}\n{result.stderr}",
            }

        return {"path": str(p), "status": "ok"}
    finally:
        try:
            os.remove(patch_file)
        except OSError:
            pass


def bash(command: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Execute a shell command and return its output."""
    import subprocess
    import shlex

    # Security: only allow certain safe commands
    allowed_commands = {
        "ls",
        "cat",
        "grep",
        "find",
        "git",
        "head",
        "tail",
        "wc",
        "pwd",
        "echo",
        "date",
        "which",
        "env",
    }
    cmd_parts = shlex.split(command)
    if cmd_parts and cmd_parts[0] not in allowed_commands:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed. Allowed: {allowed_commands}",
        }

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "status": "ok",
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Command timed out after 30 seconds"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def glob(pattern: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Find files matching a glob pattern."""
    import fnmatch

    try:
        matches = []
        base = Path(workdir)
        for path in base.rglob(pattern.replace("**/", "*").replace("**", "*")):
            if path.is_file():
                matches.append(str(path.relative_to(base)))
        return {"status": "ok", "pattern": pattern, "matches": matches}
    except Exception as e:
        return {"status": "error", "error": str(e)}
