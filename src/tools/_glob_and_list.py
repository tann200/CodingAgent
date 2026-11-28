"""Directory listing, glob search, chunk/byte/tail readers, and mkdir tools (OPT-06).

Extracted from ``src/tools/_file_io.py`` to keep file I/O modules focused and
under 1,000 LOC. All public symbols are re-exported from ``src/tools/_file_io.py``
and ``src/tools/file_tools.py`` for full backward compatibility.
"""

from __future__ import annotations

import base64
import fnmatch
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict

from src.core.security.redaction import (
    is_secret_path as _is_secret_path,
    redact_secrets as _redact_secrets,
)
from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import PermissionKind, tool
from src.tools._workspace_guard import WorkspaceGuard
from src.tools.guardrails import mark_file_read as _mark_file_read

_OS_JUNK = frozenset(
    {
        ".DS_Store",
        "._.DS_Store",
        "Thumbs.db",
        "desktop.ini",
        ".Spotlight-V100",
        ".Trashes",
        ".fseventsd",
    }
)

_GLOB_RESULT_LIMIT = 500
_GLOB_PRUNED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".codingAgent",
        ".codingagent",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
    }
)


@tool(
    name="list_files",
    tags=["coding", "planning", "debug", "review"],
    permission_kind=PermissionKind.READ_FILE,
)
def list_dir(path: str = ".", workdir: Path | None = None) -> Dict[str, Any]:
    """List all files and subdirectories located within the specified directory path."""
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "error", "error": f"Path not found: {path}"}
    if not p.is_dir():
        return {"path": str(p), "status": "error", "error": f"Not a directory: {path}"}
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name in _OS_JUNK:
            continue
        if _is_secret_path(child):
            continue
        items.append({"name": child.name, "is_dir": child.is_dir()})
    try:
        _mark_file_read(str(p.resolve()))
    except Exception:
        pass
    return {"path": str(p), "status": "ok", "items": items}


@tool(tags=["coding"], permission_kind=PermissionKind.READ_FILE)
def read_file_chunk(
    path: str,
    offset: int = 0,
    limit: int = -1,
    workdir: Path | None = None,
) -> Dict[str, Any]:
    """Read a specific line range or chunk from a file."""
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    if _is_secret_path(p):
        return {
            "path": str(p),
            "status": "error",
            "error": f"Access denied: '{p.name}' is a protected secret file. Reading credentials and private keys is restricted.",
        }

    _CHUNK_MAX_BYTES = 65_536
    safe_offset = max(0, offset)
    if limit <= 0 or limit > _CHUNK_MAX_BYTES:
        actual_limit = _CHUNK_MAX_BYTES
    else:
        actual_limit = limit

    with p.open("rb") as _bf:
        if safe_offset > 0:
            _bf.seek(safe_offset)
        raw = _bf.read(actual_limit)
    content = raw.decode("utf-8", errors="replace")
    content = _redact_secrets(content)
    try:
        _mark_file_read(str(p.resolve()))
    except Exception:
        pass
    return {
        "path": str(p),
        "status": "ok",
        "content": content,
        "offset": safe_offset,
        "limit": limit if limit > 0 else _CHUNK_MAX_BYTES,
    }


def _glob_pattern_matches(rel_posix: str, filename: str, pattern: str) -> bool:
    """Match *rel_posix* against a glob pattern supporting ``**`` and simple patterns."""
    pat = pattern.replace("\\", "/").lstrip("./")
    if not pat:
        return False
    if "/" not in pat and "**" not in pat:
        return fnmatch.fnmatch(filename, pat)
    if PurePosixPath(rel_posix).match(pat):
        return True
    if pat.startswith("**/"):
        tail = pat[3:]
        if PurePosixPath(rel_posix).match(tail) or (
            "/" not in tail and fnmatch.fnmatch(filename, tail)
        ):
            return True
    return fnmatch.fnmatch(rel_posix, pat)


@tool(tags=["coding"], permission_kind=PermissionKind.READ_FILE)
def glob(pattern: str, workdir: Path | None = None) -> Dict[str, Any]:
    """Find files matching a glob pattern. Supports ** for recursive matching."""
    if workdir is None:
        workdir = Path.cwd()
    try:
        from src.core.security.redaction import is_git_internals_path, is_secret_path

        base = Path(workdir).resolve()
        if ".." in pattern:
            return {
                "status": "error",
                "error": "Glob pattern must not contain '..'. Path traversal outside the working directory is not allowed.",
            }

        pat_parts = set(pattern.replace("\\", "/").split("/"))
        pruned_dirs = _GLOB_PRUNED_DIRS - pat_parts

        matched_with_mtime: list[tuple[float, str]] = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in pruned_dirs and d != ".git"
            ]
            dp = Path(dirpath)
            try:
                rel_dir = dp.relative_to(base)
            except ValueError:
                continue
            rel_dir_posix = "" if rel_dir == Path(".") else rel_dir.as_posix()
            for fname in filenames:
                rel_posix = f"{rel_dir_posix}/{fname}" if rel_dir_posix else fname
                if not _glob_pattern_matches(rel_posix, fname, pattern):
                    continue
                candidate = dp / fname
                try:
                    resolved = candidate.resolve()
                    rel = str(resolved.relative_to(base))
                except (ValueError, OSError):
                    continue
                if not resolved.is_file():
                    continue
                if is_secret_path(resolved) or is_git_internals_path(resolved, for_write=False):
                    continue
                try:
                    mtime = resolved.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matched_with_mtime.append((mtime, rel))

        total_found = len(matched_with_mtime)
        truncated = total_found > _GLOB_RESULT_LIMIT
        matched_with_mtime.sort(key=lambda item: item[0], reverse=True)
        matches = [rel for _, rel in matched_with_mtime[:_GLOB_RESULT_LIMIT]]
        result: Dict[str, Any] = {
            "status": "ok",
            "pattern": pattern,
            "matches": matches,
        }
        if truncated:
            result["truncated"] = True
            result["total_found"] = total_found
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding", "debug"], permission_kind=PermissionKind.READ_FILE)
def tail_log_file(
    path: str,
    lines: int = 50,
    workdir: Path | None = None,
) -> Dict[str, Any]:
    """Read the last N lines of a file. Useful for inspecting log files."""
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    if _is_secret_path(p):
        return {
            "path": str(p),
            "status": "error",
            "error": f"Access denied: '{p.name}' is a protected secret file. Reading credentials and private keys is restricted.",
        }

    try:
        n = int(lines)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"lines must be an integer, got {lines!r}"}
    if n <= 0:
        return {"status": "error", "error": f"lines must be greater than 0, got {n}"}

    _MAX_TAIL_LINES = 1_000
    _MAX_TAIL_BYTES = 512 * 1024
    n = min(n, _MAX_TAIL_LINES)

    try:
        with p.open("rb") as _fh:
            _fh.seek(0, os.SEEK_END)
            _size = _fh.tell()
            if _size == 0:
                return {
                    "path": str(p),
                    "status": "ok",
                    "content": "",
                    "total_lines": 0,
                    "lines_shown": 0,
                    "file_size_bytes": 0,
                }
            _chunk = 4096
            _buf = bytearray()
            _pos = _size
            _found_lines = 0
            while _pos > 0 and _found_lines <= n and len(_buf) < _MAX_TAIL_BYTES:
                _read_size = min(_chunk, _pos)
                _pos -= _read_size
                _fh.seek(_pos)
                _buf = bytearray(_fh.read(_read_size)) + _buf
                _found_lines = _buf.count(b"\n")
            if _found_lines > n:
                _nl_count = 0
                for _i, _b in enumerate(_buf):
                    if _b == 10:
                        _nl_count += 1
                        if _nl_count > n:
                            _buf = _buf[_i + 1 :]
                            break
            content = _buf.decode("utf-8", errors="replace")

        all_lines = content.splitlines(keepends=True)
        tail = all_lines[-n:] if n < len(all_lines) else all_lines

        total_lines_count = 0
        with p.open("rb") as _cnt_f:
            for chunk in iter(lambda: _cnt_f.read(65536), b""):
                total_lines_count += chunk.count(b"\n")

        content_str = _redact_secrets("".join(tail))

        return {
            "path": str(p),
            "status": "ok",
            "content": content_str,
            "total_lines": total_lines_count,
            "lines_shown": len(tail),
            "file_size_bytes": _size,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(
    side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE
)
def create_directory(path: str, workdir: Path | None = None) -> Dict[str, Any]:
    """Create a directory and all necessary parents."""
    if workdir is None:
        workdir = Path.cwd()
    try:
        p = _safe_resolve(path, workdir)
    except (PermissionError, ValueError) as exc:
        return {"path": path, "status": "error", "error": str(exc)}
    guard = WorkspaceGuard(workspace_root=workdir.resolve())
    guard_result = guard.guard_operation("create_directory", str(p))
    if guard_result.get("status") == "error":
        return {"path": str(p), "status": "error", "error": guard_result.get("error")}
    try:
        from src.tools import _file_io

        _file_io._check_project_deny_write(p, workdir)
    except PermissionError as _pe:
        return {"path": str(p), "status": "error", "error": str(_pe)}
    try:
        from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

        if is_current_tool_cancelled():
            return {
                "path": str(p),
                "status": "error",
                "error": "Tool execution was cancelled or timed out before create committed.",
                "cancelled": True,
            }
    except Exception:
        pass
    try:
        p.mkdir(parents=True, exist_ok=True)
        return {"path": str(p), "status": "ok", "created": True}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding", "debug"], permission_kind=PermissionKind.READ_FILE)
def read_file_bytes(
    path: str,
    max_bytes: int = 1048576,
    workdir: Path | None = None,
) -> Dict[str, Any]:
    """Read a file as base64-encoded bytes. Useful for binary files, images, or compiled artifacts."""
    if workdir is None:
        workdir = Path.cwd()

    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    if _is_secret_path(p):
        return {
            "path": str(p),
            "status": "error",
            "error": f"Access denied: '{p.name}' is a protected secret file. Reading credentials and private keys is restricted.",
        }

    try:
        mb = int(max_bytes)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "error": f"max_bytes must be an integer, got {max_bytes!r}",
        }

    _MAX_BYTES_HARD_CAP = 10_485_760  # 10 MB
    if mb <= 0 or mb > _MAX_BYTES_HARD_CAP:
        mb = 1_048_576 if mb <= 0 else _MAX_BYTES_HARD_CAP

    try:
        with p.open("rb") as _fh:
            data = _fh.read(mb)

        from src.core.security.redaction import contains_secrets

        try:
            decoded_text = data.decode("utf-8", errors="ignore")
            if contains_secrets(decoded_text):
                return {
                    "path": str(p),
                    "status": "error",
                    "error": "Cannot read raw bytes of file containing credentials/secrets; use read_file for redacted text",
                }
        except Exception:
            pass

        return {
            "path": str(p),
            "status": "ok",
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "bytes_read": len(data),
            "total_bytes": p.stat().st_size,
            "truncated": len(data) < p.stat().st_size,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
