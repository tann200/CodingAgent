"""File I/O tools — read, write, list, delete, rename, glob, tail, mkdir.

Extracted from file_tools.py.  All public names are re-exported from
src.tools.file_tools for backward compatibility.

Circular-import note: _safe_resolve is imported directly from _path_utils
(the underlying implementation) rather than from file_tools, to avoid a
circular dependency.  The _safe_resolve wrapper in file_tools.py remains
the authoritative public symbol for test compatibility.
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any

from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import tool, PermissionKind
from src.tools._diff_gate import (
    pop_preview_rejection as _pop_preview_rejection,
    register_preview_gate as _register_preview_gate,
)
from src.tools._workspace_guard import WorkspaceGuard  # type: ignore[assignment]
from src.tools._lint_verify import verify_candidate_content as _verify_write_candidate
from src.tools.guardrails import (
    check_read_before_write as _check_read_before_write,
    mark_file_read as _mark_file_read,
)
from src.core.orchestration.event_bus import get_event_bus as _get_event_bus
from src.tools.formatter import run_formatter as _run_formatter
from src.tools._diff_gate import _publish_diff_preview

_logger = logging.getLogger(__name__)
_get_bus = _get_event_bus

_WRITE_HARD_LINE_LIMIT = 500
_WRITE_WARN_LINE_LIMIT = 200
_READ_FILE_MAX_LINE = 2_000
_READ_FILE_MAX_CHARS = 100_000


def get_project_deny_write_patterns(workdir: str) -> list[str]:
    try:
        from src.tools.tools_config import agent_context_path

        config_path = agent_context_path(Path(workdir)) / "config.json"
        if not config_path.exists():
            return []
        import json

        data = json.loads(config_path.read_text(encoding="utf-8"))
        patterns = data.get("deny_write") or []
        return [str(pattern) for pattern in patterns if pattern]
    except Exception:
        return []


def _is_in_workspace(path: Path, workdir: Path) -> bool:
    """Return True iff *path* is strictly inside *workdir* (no escaping via ..).

    Mirrors opencode's Instance.containsPath() — pure path-prefix check,
    no OS sandbox involved.
    """
    try:
        path.resolve().relative_to(workdir.resolve())
        return True
    except ValueError:
        return False


def _check_project_deny_write(abs_path: Path, workdir: Path) -> None:
    """Raise PermissionError if *abs_path* matches a project config deny_write pattern.

    OP-5: Reads ``deny_write`` patterns from ``.agent-context/config.json``
    (relative to *workdir*) and applies them via ``fnmatch.fnmatch`` against
    the relative path of *abs_path*.  No-ops silently when the config file is
    absent or when no patterns are configured.
    """

    try:
        patterns = get_project_deny_write_patterns(str(workdir))
        if not patterns:
            return
        try:
            rel = abs_path.relative_to(workdir)
        except ValueError:
            rel = abs_path  # can't relativise — compare against full path
        rel_str = str(rel).replace("\\", "/")
        for pattern in patterns:
            if fnmatch.fnmatch(rel_str, pattern):
                raise PermissionError(
                    f"write blocked by project config: '{rel_str}' matches "
                    f"deny_write pattern '{pattern}'"
                )
    except PermissionError:
        raise
    except Exception:
        pass  # non-fatal — don't let config errors prevent writes


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


def _generate_write_diff(
    p: Path, original_content: str, content: str
) -> tuple[str, int, int]:
    """Generate a unified diff for a write_file operation.

    Returns (diff, lines_added, lines_removed).
    Handles both file updates and new file creation.
    """
    original_lines = original_content.splitlines() if original_content else []
    new_lines = content.splitlines()

    if original_content:
        diff_lines = list(
            difflib.unified_diff(
                original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm="\n"
            )
        )
        diff = "".join(diff_lines)
        # MED-1 fix: exclude "+++" and "---" unified diff headers from line counts
        lines_added = sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )
        lines_removed = sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        )
    else:
        # New file — produce a single unified-diff hunk with all lines added
        hunk_header = f"@@ -0,0 +1,{len(new_lines)} @@\n"
        diff_lines = ["--- /dev/null\n", f"+++ {p}\n", hunk_header]
        for line in new_lines:
            diff_lines.append(f"+{line}\n" if not line.endswith("\n") else f"+{line}")
        diff = "".join(diff_lines)
        lines_added = len(new_lines)
        lines_removed = 0

    return diff, lines_added, lines_removed


@tool(
    side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE
)
def write_file(
    path: str,
    content: str,
    workdir: Path | None = None,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """Write content to a file. Returns diff in result for TUI display."""
    if workdir is None:
        workdir = Path.cwd()
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("write_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

    p = _safe_resolve(path, workdir)

    # GAP-S1: Read-before-write guardrail (check after resolve so path is absolute)
    try:
        rbw = _check_read_before_write(str(p))
        if rbw:
            return {"path": str(p), "status": "error", **rbw}
    except Exception:
        pass

    # OP-5: Apply project-level deny_write patterns from .agent-context/config.json.
    try:
        _check_project_deny_write(p, workdir)
    except PermissionError as _pe:
        return {"path": path, "status": "error", "error": str(_pe)}

    p.parent.mkdir(parents=True, exist_ok=True)

    # Read original content BEFORE modification for diff generation
    original_content = ""
    if p.exists():
        original_content = p.read_text(encoding="utf-8")

    # D-04: Idempotency guard — skip write when content is identical
    if original_content == content:
        return {
            "path": str(p),
            "status": "no_change",
            "diff": "",
            "lines_added": 0,
            "lines_removed": 0,
            "is_new_file": False,
        }

    # Generate unified diff BEFORE writing so preview shows what *will* change (F14 fix)
    diff, lines_added, lines_removed = _generate_write_diff(
        p, original_content, content
    )

    # GAP-S3: Hard file-size guard — block BEFORE writing (guard must run pre-write)
    if lines_added > _WRITE_HARD_LINE_LIMIT:
        return {
            "path": str(p),
            "status": "error",
            "error": (
                f"write_file refused: {lines_added} lines exceeds {_WRITE_HARD_LINE_LIMIT}-line hard limit. "
                "Split into multiple smaller writes."
            ),
        }

    ver_err = _verify_write_candidate(p, content, workdir)
    if ver_err:
        return ver_err

    # PREV-1: If preview confirmation is required, block until the user
    # accepts or rejects the diff in the TUI (same gate as edit_file_atomic).
    # If confirmation is not required (default), publish for display only and
    # proceed immediately.
    try:
        from src.tools.tools_config import requires_preview_confirmation

        _path_key = str(p)
        _is_new = not bool(original_content)

        if requires_preview_confirmation():
            # Blocking gate — wait for TUI accept/reject

            _has_sub = _get_bus().has_subscribers("file.diff.preview")
            if _has_sub:
                _gate_ev = _register_preview_gate(_path_key)
                _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
                _gate_ev.wait(timeout=300.0)
                # Use helpers to check/clear rejection state atomically
                _was_rejected = _pop_preview_rejection(_path_key)
                if _was_rejected:
                    return {
                        "path": str(p),
                        "status": "rejected",
                        "message": "Write rejected by user.",
                    }
            else:
                # No TUI subscriber — publish for telemetry and proceed
                _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
        else:
            # F14: Publish diff preview BEFORE writing so the TUI can show
            # the proposed change as informational output — no gate.
            _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
    except Exception as _prev_exc:
        _logger.debug("write_file preview gate error (non-fatal): %s", _prev_exc)
        # Fallback: publish without gate
        _publish_diff_preview(str(p), diff, is_new_file=not bool(original_content))

    # Write new content atomically using mkstemp -> os.replace with fsync.
    # This avoids exposing partially-written files to readers.
    _fd = None
    _tmp_path = None
    try:
        _fd, _tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            # Wrap fd in a text-mode file and write
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                _fd = None
                _f.write(content)
                try:
                    _f.flush()
                    os.fsync(_f.fileno())
                except Exception:
                    # fsync best-effort; don't fail the write for platform limits
                    pass
            try:
                os.replace(_tmp_path, str(p))
            except Exception:
                try:
                    shutil.move(_tmp_path, str(p))
                except Exception as _mv_exc:
                    # Neither os.replace nor shutil.move succeeded — clean up and fail.
                    try:
                        os.unlink(_tmp_path)
                    except Exception:
                        pass
                    raise _mv_exc
        except Exception:
            if _fd is not None:
                try:
                    os.close(_fd)
                except Exception:
                    pass
            raise
    except Exception as _mk_exc:
        # Ensure temp file cleaned up if present
        try:
            if _tmp_path and os.path.exists(_tmp_path):
                os.unlink(_tmp_path)
        except Exception:
            pass
        _logger.exception("write_file atomic write failed for %s", p)
        return {"path": str(p), "status": "error", "error": str(_mk_exc)}

    # MEM-1: Evict the just-written file from ContextBuilder's read cache so the
    # next context build reflects the new content rather than the pre-write version.
    try:
        from src.core.context.context_builder import ContextBuilder as _CB
        _CB.invalidate_path(str(p))
    except Exception:
        pass  # never block a write on cache-invalidation failure

    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "is_new_file": not bool(original_content),
        "diff": diff,
    }
    # S2-C: Auto-formatter — run after write; failures are warnings only.
    try:
        _formatted = _run_formatter(str(p))
        if _formatted:
            result["formatted"] = True
    except Exception:
        pass  # Never block a write on formatter failure

    # F13: Signal when a file write is unreasonably large — agent should split the task.
    if lines_added > _WRITE_WARN_LINE_LIMIT:
        result["requires_split"] = True
        result["error"] = (
            f"write_file wrote {lines_added} lines in a single call. "
            "Split into multiple targeted function/section writes."
        )
    return result


@tool(
    tags=["coding", "planning", "debug", "review"],
    permission_kind=PermissionKind.READ_FILE,
)
def read_file(
    path: str,
    summarize: bool = False,
    workdir: Path | None = None,
) -> Dict[str, Any]:
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    # Binary detection: read first 512 bytes for null-byte check
    # without loading the entire file into memory (OOM-1 fix).
    try:
        with p.open("rb") as _fh:
            _header = _fh.read(512)
    except Exception as _e:
        return {"path": str(p), "status": "error", "error": f"Failed to read: {_e}"}
    if b"\x00" in _header:
        return {
            "path": str(p),
            "status": "error",
            "error": "Binary file detected. Use a binary-aware tool.",
        }

    try:
        with p.open("rb") as _fh:
            raw_bytes = _fh.read(_READ_FILE_MAX_CHARS * 4)
    except Exception as _e:
        return {"path": str(p), "status": "error", "error": f"Failed to read: {_e}"}

    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": str(p),
            "status": "error",
            "error": "File is not valid UTF-8. Cannot read as text.",
        }

    # GAP-S1: Mark file as read for guardrail enforcement
    try:
        _mark_file_read(str(p.resolve()))
    except Exception:
        pass

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

    # Per-line cap: single pass — avoids scanning lines twice.
    lines = content.splitlines(keepends=True)
    capped = []
    any_capped = False
    for ln in lines:
        if len(ln) > _READ_FILE_MAX_LINE:
            capped.append(ln[:_READ_FILE_MAX_LINE] + "… [line truncated]\n")
            any_capped = True
        else:
            capped.append(ln)
    if any_capped:
        content = "".join(capped)

    # Total output cap
    truncated = False
    if len(content) > _READ_FILE_MAX_CHARS:
        omitted = len(content) - _READ_FILE_MAX_CHARS
        content = (
            content[:_READ_FILE_MAX_CHARS]
            + f"\n... [file truncated: {omitted} chars omitted]"
        )
        truncated = True

    result: Dict[str, Any] = {"path": str(p), "status": "ok", "content": content}
    if truncated:
        result["truncated"] = True
    return result


@tool(
    name="list_files",
    tags=["coding", "planning", "debug", "review"],
    permission_kind=PermissionKind.READ_FILE,
)
def list_dir(path: str = ".", workdir: Path | None = None) -> Dict[str, Any]:
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name in _OS_JUNK:
            continue
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"path": str(p), "status": "ok", "items": items}


@tool(
    side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE
)
def delete_file(
    path: str,
    workdir: Path | None = None,
    user_approved: bool = False,  # type: ignore[assignment]
) -> Dict[str, Any]:
    if workdir is None:
        workdir = Path.cwd()
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("delete_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

    # Read-before-write guardrail: deletion is destructive
    try:
        rbw = _check_read_before_write(path)
        if rbw:
            return {"path": path, "status": "error", **rbw}
    except Exception:
        pass

    try:
        p = _safe_resolve(path, workdir)
        if not p.exists():
            return {"path": str(p), "status": "not_found"}
        deleted_path = str(p)

        # TS-4: Warn if the file is tracked by git (deletion would remove history)
        git_warning = None
        try:
            _gr = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(p)],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(workdir),
            )
            if _gr.returncode == 0:
                git_warning = (
                    f"'{path}' is tracked by git. "
                    "Deleting it will remove the file from the working tree; "
                    "use 'git rm' to also stage the deletion."
                )
                _logger.warning("delete_file: %s", git_warning)
        except Exception:
            pass

        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        result = {"path": deleted_path, "status": "ok", "deleted": True}
        if git_warning:
            result["warning"] = git_warning
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(
    side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE
)
def rename_file(
    path: str = "",
    new_path: str = "",
    workdir: Path | None = None,
    # common aliases accepted so the LLM doesn't need to guess
    src_path: str = "",
    dst_path: str = "",
    src: str = "",
    dst: str = "",
    old_path: str = "",
    new_name: str = "",
) -> Dict[str, Any]:
    """Rename (move) a file within the workspace.

    Args:
        path: Current path of the file (also accepted as src_path, src, old_path).
        new_path: New path for the file (also accepted as dst_path, dst, new_name).

    Both paths are resolved against workdir and validated to prevent path traversal.
    """
    if workdir is None:
        workdir = Path.cwd()
    # Resolve aliases so callers don't need to know the exact parameter names
    resolved_src = path or src_path or src or old_path
    resolved_dst = new_path or dst_path or dst or new_name

    if not resolved_src:
        return {
            "status": "error",
            "error": "Missing source path (use 'path' parameter)",
        }
    if not resolved_dst:
        return {
            "status": "error",
            "error": "Missing destination path (use 'new_path' parameter)",
        }

    try:
        src_resolved = _safe_resolve(resolved_src, workdir)
        dst_resolved = _safe_resolve(resolved_dst, workdir)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    if not src_resolved.exists():
        return {"path": str(src_resolved), "status": "not_found"}

    # Read-before-write guardrail: rename is destructive on the source file
    try:
        rbw = _check_read_before_write(resolved_src)
        if rbw:
            return {"status": "error", **rbw}
    except Exception:
        pass

    try:
        dst_resolved.parent.mkdir(parents=True, exist_ok=True)
        src_resolved.rename(dst_resolved)
        return {"ok": True, "status": "ok", "renamed": str(dst_resolved)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding"], permission_kind=PermissionKind.READ_FILE)
def read_file_chunk(
    path: str,
    offset: int = 0,
    limit: int = -1,
    workdir: Path | None = None,
) -> Dict[str, Any]:
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    # Open in binary, apply byte offset, then decode to handle multi-byte UTF-8
    # correctly. Text-mode seek() is only safe with offsets returned by tell().
    with p.open("rb") as _bf:
        if offset > 0:
            _bf.seek(offset)
        raw = _bf.read(limit if limit != -1 else None)
    content = raw.decode("utf-8", errors="replace")
    # GAP-S1: Mark file as read for guardrail enforcement
    try:
        _mark_file_read(str(p.resolve()))
    except Exception:
        pass
    return {
        "path": str(p),
        "status": "ok",
        "content": content,
        "offset": offset,
        "limit": limit,
    }


_GLOB_RESULT_LIMIT = 500


@tool(tags=["coding"], permission_kind=PermissionKind.READ_FILE)
def glob(pattern: str, workdir: Path | None = None) -> Dict[str, Any]:
    """Find files matching a glob pattern. Supports ** for recursive matching."""
    if workdir is None:
        workdir = Path.cwd()
    try:
        base = Path(workdir).resolve()
        # F13 fix: reject patterns that escape the working directory via ".."
        if ".." in pattern:
            return {
                "status": "error",
                "error": "Glob pattern must not contain '..'. Path traversal outside the working directory is not allowed.",
            }
        if "**" in pattern:
            # Pattern already expresses recursion; use Path.glob() verbatim so ** is honoured
            raw = base.glob(pattern)
        else:
            # Simple pattern — search the whole tree recursively
            raw = base.rglob(pattern)
        matches = []
        for p in raw:
            if not p.is_file():
                continue
            try:
                rel = str(p.resolve().relative_to(base))
                matches.append(rel)
            except ValueError:
                # Path resolved to outside base — skip silently (prevents path traversal exfiltration)
                continue
        total_found = len(matches)
        truncated = total_found > _GLOB_RESULT_LIMIT
        # Sort by modification time descending (most recently modified first),
        # matching claw-code-main Rust glob_search behaviour.
        matches.sort(
            key=lambda rel_path: (
                (base / rel_path).stat().st_mtime if (base / rel_path).exists() else 0.0
            ),
            reverse=True,
        )
        matches = matches[:_GLOB_RESULT_LIMIT]
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
    try:
        n = int(lines)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"lines must be an integer, got {lines!r}"}
    try:
        import os as _os

        with p.open("rb") as _fh:
            _fh.seek(0, _os.SEEK_END)
            _size = _fh.tell()
            if _size == 0:
                return {"path": str(p), "status": "ok", "content": "", "total_lines": 0, "lines_shown": 0}
            _chunk = 4096
            _buf = bytearray()
            _pos = _size
            _found_lines = 0
            while _pos > 0 and _found_lines <= n:
                _read_size = min(_chunk, _pos)
                _pos -= _read_size
                _fh.seek(_pos)
                _buf = bytearray(_fh.read(_read_size)) + _buf
                _found_lines = _buf.count(b"\n")
            # Ensure we start at a newline boundary
            if _found_lines > n:
                _idx = _buf.find(b"\n")
                # Skip the first newline to get exactly n lines (or start from the buffer's
                # offset if we didn't overshoot).  Use a simple loop to count newlines.
                _nl_count = 0
                for _i, _b in enumerate(_buf):
                    if _b == 10:  # \n
                        _nl_count += 1
                        if _nl_count > n:
                            _buf = _buf[_i + 1:]
                            break
            content = _buf.decode("utf-8", errors="replace")
        all_lines = content.splitlines(keepends=True)
        tail = all_lines[-n:] if n < len(all_lines) else all_lines
        return {
            "path": str(p),
            "status": "ok",
            "content": "".join(tail),
            "total_lines": _size,
            "lines_shown": len(tail),
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
    """Read a file as base64-encoded bytes. Useful for binary files, images, or compiled artifacts.

    Args:
        path: File path to read
        max_bytes: Maximum bytes to read (default 1MB)
        workdir: Working directory for path resolution
    """
    if workdir is None:
        workdir = Path.cwd()

    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}
    try:
        mb = int(max_bytes)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "error": f"max_bytes must be an integer, got {max_bytes!r}",
        }
    try:
        with p.open("rb") as _fh:
            data = _fh.read(mb)
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
