"""File I/O tools — read, write, list, delete, rename, glob, tail, mkdir.

Extracted from file_tools.py.  All public names are re-exported from
src.tools.file_tools for backward compatibility.

Circular-import note: _safe_resolve is imported directly from _path_utils
(the underlying implementation) rather than from file_tools, to avoid a
circular dependency.  The _safe_resolve wrapper in file_tools.py remains
the authoritative public symbol for test compatibility.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Any

_logger = logging.getLogger(__name__)

from src.tools._path_utils import safe_resolve as _safe_resolve_impl
from src.tools._tool import tool, PermissionKind
from src.tools._diff_gate import (
    _publish_diff_preview,
    register_preview_gate,
    _preview_gate_lock,
    _preview_rejected,
)

# Write-size constants (authoritative values — re-exported from file_tools)
_READ_FILE_MAX_CHARS = 50_000
_READ_FILE_MAX_LINE = 2_000
_WRITE_HARD_LINE_LIMIT = 500
_WRITE_WARN_LINE_LIMIT = 200


# WorkspaceGuard: no-op fallback; real import shadows it at runtime
class WorkspaceGuard:
    """No-op guard when src.core is not available."""

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        return {"status": "ok"}


try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard  # type: ignore[assignment]
except ImportError:
    pass


def _safe_resolve(path: str, workdir: Path) -> Path:
    """Thin wrapper around _path_utils.safe_resolve for use within this module."""
    return _safe_resolve_impl(path, workdir)


def _check_project_deny_write(abs_path: Path, workdir: Path) -> None:
    """Raise PermissionError if *abs_path* matches a project config deny_write pattern.

    OP-5: Reads ``deny_write`` patterns from ``.agent-context/config.json``
    (relative to *workdir*) and applies them via ``fnmatch.fnmatch`` against
    the relative path of *abs_path*.  No-ops silently when the config file is
    absent or when no patterns are configured.
    """
    import fnmatch

    try:
        from src.core.config_loader import get_project_deny_write_patterns

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


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
def write_file(
    path: str,
    content: str,
    workdir: Path = None,  # type: ignore[assignment]
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

    # GAP-S1: Read-before-write guardrail
    try:
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(path)
        if rbw:
            return {"path": path, "status": "error", **rbw}
    except Exception:
        pass

    import difflib

    p = _safe_resolve(path, workdir)

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
            "lines_added": 0,
            "lines_removed": 0,
            "is_new_file": False,
        }

    # Generate unified diff BEFORE writing so preview shows what *will* change (F14 fix)
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
            from src.core.orchestration.event_bus import get_event_bus as _get_bus

            _has_sub = _get_bus().has_subscribers("file.diff.preview")
            if _has_sub:
                _gate_ev = register_preview_gate(_path_key)
                _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
                _gate_ev.wait(timeout=300.0)
                with _preview_gate_lock:
                    _was_rejected = _path_key in _preview_rejected
                    if _was_rejected:
                        _preview_rejected.discard(_path_key)
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

    # Write new content
    p.write_text(content, encoding="utf-8")

    # MEM-1: Invalidate context cache entry so the next ContextBuilder instantiation
    # re-reads from disk rather than serving the pre-write cached content.
    try:
        from src.core.context.context_builder import ContextBuilder as _CB

        _CB.invalidate_path(str(p))
    except Exception:
        pass  # Never block a write on cache invalidation failure

    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "is_new_file": not bool(original_content),
    }
    # S2-C: Auto-formatter — run after write; failures are warnings only.
    try:
        from src.tools.formatter import run_formatter as _run_formatter

        _formatted = _run_formatter(str(p))
        if _formatted:
            result["formatted"] = True
    except Exception:
        pass  # Never block a write on formatter failure

    # IMPL-5: Post-write auto-lint — informational, does not block the write
    try:
        from src.tools.lint_dispatch import quick_lint as _quick_lint

        lint_result = _quick_lint(str(p), workdir)
        if lint_result and lint_result.get("lint_errors"):
            result["lint_warnings"] = lint_result["lint_errors"]
            result["lint_status"] = "warnings"
    except Exception:
        pass  # Never block a write on lint failure
    # F13: Signal when a file write is unreasonably large — agent should split the task.
    if lines_added > _WRITE_WARN_LINE_LIMIT:
        result["requires_split"] = True
        result["error"] = (
            f"write_file wrote {lines_added} lines in a single call. "
            "Split into multiple targeted function/section writes."
        )
    return result


@tool(tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.READ_FILE)
def read_file(
    path: str,
    summarize: bool = False,
    workdir: Path = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    # Binary detection: check first 512 bytes for null bytes
    raw_bytes = p.read_bytes()
    if b"\x00" in raw_bytes[:512]:
        return {
            "path": str(p),
            "status": "error",
            "error": f"Binary file detected ({len(raw_bytes)} bytes). Use a binary-aware tool.",
        }

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
        from src.tools.guardrails import mark_file_read

        mark_file_read(str(p.resolve()))
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


@tool(name="list_files", tags=["coding", "planning", "debug", "review"], permission_kind=PermissionKind.READ_FILE)
def list_dir(path: str = ".", workdir: Path = None) -> Dict[str, Any]:  # type: ignore[assignment]
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name in _OS_JUNK:
            continue
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"path": str(p), "status": "ok", "items": items}


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
def delete_file(
    path: str,
    workdir: Path = None,
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
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(path)
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
            import subprocess as _sp

            _gr = _sp.run(
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
                import logging as _logging

                _logging.getLogger(__name__).warning(f"delete_file: {git_warning}")
        except Exception:
            pass

        if p.is_dir():
            import shutil

            shutil.rmtree(p)
        else:
            p.unlink()
        result = {"path": deleted_path, "status": "ok", "deleted": True}
        if git_warning:
            result["warning"] = git_warning
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
def rename_file(
    path: str = "",
    new_path: str = "",
    workdir: Path = None,  # type: ignore[assignment]
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
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(resolved_src)
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


def sandbox_info(workdir: Path = None) -> Dict[str, Any]:  # type: ignore[assignment]
    if workdir is None:
        workdir = Path.cwd()
    return {"workdir": str(workdir.resolve())}


@tool(tags=["coding"], permission_kind=PermissionKind.READ_FILE)
def read_file_chunk(
    path: str,
    offset: int = 0,
    limit: int = -1,
    workdir: Path = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    if workdir is None:
        workdir = Path.cwd()
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    with p.open("r", encoding="utf-8") as f:
        f.seek(offset)
        content = f.read(limit)
        # GAP-S1: Mark file as read for guardrail enforcement
        try:
            from src.tools.guardrails import mark_file_read

            mark_file_read(str(p.resolve()))
        except Exception:
            pass
        return {
            "path": str(p),
            "status": "ok",
            "content": content,
            "offset": offset,
            "limit": limit,
        }


@tool(tags=["coding"], permission_kind=PermissionKind.READ_FILE)
def glob(pattern: str, workdir: Path = None) -> Dict[str, Any]:  # type: ignore[assignment]
    """Find files matching a glob pattern. Supports ** for recursive matching."""
    if workdir is None:
        workdir = Path.cwd()
    LIMIT = 500
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
        truncated = total_found > LIMIT
        # Sort by modification time descending (most recently modified first),
        # matching claw-code-main Rust glob_search behaviour.
        matches.sort(
            key=lambda rel_path: (base / rel_path).stat().st_mtime
            if (base / rel_path).exists()
            else 0.0,
            reverse=True,
        )
        matches = matches[:LIMIT]
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
    workdir: Path = None,  # type: ignore[assignment]
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
    content = p.read_text(encoding="utf-8")
    all_lines = content.splitlines(keepends=True)
    tail = all_lines[-n:] if n < len(all_lines) else all_lines
    return {
        "path": str(p),
        "status": "ok",
        "content": "".join(tail),
        "total_lines": len(all_lines),
        "lines_shown": len(tail),
    }


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
def create_directory(path: str, workdir: Path = None) -> Dict[str, Any]:  # type: ignore[assignment]
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
    workdir: Path = None,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Read a file as base64-encoded bytes. Useful for binary files, images, or compiled artifacts.

    Args:
        path: File path to read
        max_bytes: Maximum bytes to read (default 1MB)
        workdir: Working directory for path resolution
    """
    if workdir is None:
        workdir = Path.cwd()
    import base64

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
        data = p.read_bytes()[:mb]
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
