"""File I/O tools — read, write, list, delete, rename, glob, tail, mkdir.

Extracted from file_tools.py.  All public names are re-exported from
src.tools.file_tools for backward compatibility.

Circular-import note: _safe_resolve is imported directly from _path_utils
(the underlying implementation) rather than from file_tools, to avoid a
circular dependency.  The _safe_resolve wrapper in file_tools.py remains
the authoritative public symbol for test compatibility.
"""

from __future__ import annotations

import difflib
import fnmatch
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any

from src.tools._glob_and_list import (  # noqa: F401
    _GLOB_PRUNED_DIRS,
    _GLOB_RESULT_LIMIT,
    _OS_JUNK,
    _glob_pattern_matches,
    create_directory,
    glob,
    list_dir,
    read_file_bytes,
    read_file_chunk,
    tail_log_file,
)

from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import tool, PermissionKind
from src.tools._diff_gate import (
    pop_preview_rejection as _pop_preview_rejection,
    register_preview_gate as _register_preview_gate,
)
from src.tools._workspace_guard import (
    WorkspaceGuard,  # type: ignore[assignment]
    check_file_safety as _check_file_safety,
)
from src.tools._lint_verify import verify_candidate_content as _verify_write_candidate
from src.tools.guardrails import (
    check_read_before_write as _check_read_before_write,
    mark_file_read as _mark_file_read,
)
from src.core.orchestration.event_bus import get_event_bus as _get_event_bus
from src.tools.verification_tools import run_formatter as _run_formatter
from src.tools._diff_gate import _publish_diff_preview
from src.core.security.redaction import (
    is_secret_path as _is_secret_path,
    redact_secrets as _redact_secrets,
)

_logger = logging.getLogger(__name__)
_get_bus = _get_event_bus

_WRITE_HARD_LINE_LIMIT = 500
_WRITE_WARN_LINE_LIMIT = 200
_READ_FILE_MAX_LINE = 2_000
_READ_FILE_MAX_CHARS = 100_000


def get_project_deny_write_patterns(workdir: str) -> list[str]:
    """Return ``deny_write`` patterns from ``.codingAgent/config.json``.

    Delegates to the canonical reader in ``config_loader`` so the schema is
    defined in exactly one place.  Never raises.
    """
    try:
        from src.core.config_loader import (
            get_project_deny_write_patterns as _canonical,
        )

        return _canonical(str(workdir))
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

    OP-5: Reads ``deny_write`` patterns from ``.codingAgent/config.json``
    (relative to *workdir*) and applies them via ``fnmatch.fnmatch`` against
    the relative path of *abs_path*.  No-ops silently when the config file is
    absent or when no patterns are configured.
    """

    try:
        patterns = get_project_deny_write_patterns(str(workdir))
        if not patterns:
            return
        try:
            rel = abs_path.resolve().relative_to(workdir.resolve())
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


def _is_binary_file(p: Path) -> bool:
    """Return True if *p* exists as a file and contains NUL bytes in its header."""
    try:
        if not p.is_file():
            return False
        with p.open("rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return False


def _pre_write_check(
    path: str,
    workdir: Path,
    *,
    operation: str = "write_file",
    user_approved: bool = False,
    check_binary: bool = True,
    check_read: bool = True,
) -> tuple[Path | None, Dict[str, Any] | None]:
    """Unified pre-write guardrail sequence: check_file_safety -> safe_resolve -> WorkspaceGuard -> deny_write -> read-before-write -> binary check."""
    safety_err = _check_file_safety(
        path, workdir=workdir, for_write=True, allow_secret_override=user_approved
    )
    if safety_err is not None:
        return None, safety_err

    guard = WorkspaceGuard(workspace_root=workdir.resolve())
    guard_result = guard.guard_operation(operation, path, user_approved)
    if guard_result.get("status") == "error":
        return None, {"path": path, "status": "error", "error": guard_result.get("error")}

    try:
        p = _safe_resolve(path, workdir)
    except ValueError as exc:
        return None, {"path": path, "status": "error", "error": str(exc)}

    guard_res_resolved = guard.guard_operation(operation, str(p), user_approved)
    if guard_res_resolved.get("status") == "error":
        return None, {"path": str(p), "status": "error", "error": guard_res_resolved.get("error")}

    try:
        _check_project_deny_write(p, workdir)
    except PermissionError as _pe:
        return None, {"path": path, "status": "error", "error": str(_pe)}

    if check_read:
        rbw = _check_read_before_write(str(p))
        if rbw:
            return None, {"path": str(p), "status": "error", **rbw}

    if check_binary and _is_binary_file(p):
        return None, {
            "path": str(p),
            "status": "error",
            "error": f"Cannot perform text operation on binary file: {path}",
        }

    return p, None


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
    p, pre_err = _pre_write_check(
        path, workdir, operation="write_file", user_approved=user_approved
    )
    if pre_err is not None or p is None:
        return pre_err or {"path": path, "status": "error", "error": "Pre-write check failed"}

    # Read original content BEFORE modification for diff generation
    existed = p.exists() and p.is_file()
    orig_mode: int | None = None
    original_content = ""
    if existed:
        try:
            orig_mode = stat.S_IMODE(p.stat().st_mode)
        except OSError:
            orig_mode = None
        try:
            original_content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                original_content = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                original_content = ""
        except Exception:
            original_content = ""

    # D-04: Idempotency guard — skip write ONLY when an existing file already
    # has identical content (never skip creating a brand-new empty file like __init__.py).
    if existed and original_content == content:
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
    # accepts or rejects the diff in the TUI (fail closed on timeout/error).
    _require_preview = False
    try:
        from src.tools.tools_config import requires_preview_confirmation

        _require_preview = bool(requires_preview_confirmation())
    except Exception:
        _require_preview = False

    try:
        _path_key = str(p)
        _is_new = not existed

        if _require_preview:
            _has_sub = _get_bus().has_subscribers("file.diff.preview")
            if _has_sub:
                _gate_ev = _register_preview_gate(_path_key)
                _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
                _signaled = bool(_gate_ev.wait(timeout=300.0))
                _was_rejected = _pop_preview_rejection(_path_key)
                if not _signaled or _was_rejected:
                    return {
                        "path": str(p),
                        "status": "rejected",
                        "message": (
                            "Write rejected by user."
                            if _was_rejected
                            else "Write preview confirmation timed out (fail-closed)."
                        ),
                    }
            else:
                _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
        else:
            _publish_diff_preview(_path_key, diff, is_new_file=_is_new)
    except Exception as _prev_exc:
        if _require_preview:
            _logger.error("write_file preview gate failed closed: %s", _prev_exc)
            return {
                "path": str(p),
                "status": "rejected",
                "error": f"Preview confirmation gate error (fail-closed): {_prev_exc}",
            }
        _logger.debug("write_file preview publish error (non-fatal): %s", _prev_exc)

    # Create parent directories only AFTER all pre-write checks & approval gates pass.
    p.parent.mkdir(parents=True, exist_ok=True)

    # Cooperative cancellation check: abort if tool execution timed out or was cancelled
    try:
        from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

        if is_current_tool_cancelled():
            return {
                "path": str(p),
                "status": "error",
                "error": "Tool execution was cancelled or timed out before write committed.",
                "cancelled": True,
            }
    except Exception:
        pass

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
                from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

                if is_current_tool_cancelled():
                    try:
                        os.unlink(_tmp_path)
                    except Exception:
                        pass
                    return {
                        "path": str(p),
                        "status": "error",
                        "error": "Tool execution was cancelled or timed out before write committed.",
                        "cancelled": True,
                    }
            except Exception:
                pass
            if orig_mode is not None:
                try:
                    os.chmod(_tmp_path, orig_mode)
                except OSError:
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
    try:
        from src.core.context.instruction_files import maybe_collect_nested_instructions

        nested = maybe_collect_nested_instructions(p, workdir)
        if nested:
            result["nested_instructions"] = nested
    except Exception:
        pass
    return result


def _resolve_read_guard_limits() -> tuple[int | None, int | None]:
    """Return ``(char_limit, line_limit)`` for Context-Aware Read Guard when an orchestrator is active."""
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        orch = _PARENT_ORCHESTRATOR_VAR.get(None)
    except Exception:
        orch = None
    if orch is None:
        return None, None

    try:
        from src.core.orchestration.pipeline.post_hooks import resolve_tool_output_max_chars

        max_chars = resolve_tool_output_max_chars(orch)
    except Exception:
        max_chars = 12_000

    remaining_chars: int | None = None
    try:
        tm = getattr(orch, "token_monitor", None)
        if tm is not None and hasattr(tm, "get_budget"):
            sid = getattr(orch, "_current_task_id", "default")
            budget = tm.get_budget(session_id=sid)
            max_tok = int(getattr(budget, "max_tokens", 0) or 0)
            used_tok = int(getattr(budget, "used_tokens", 0) or 0)
            if max_tok > 0:
                remaining_chars = max(0, (max_tok - used_tok) * 4)
    except Exception:
        remaining_chars = None

    policy_tier = ""
    pol = getattr(orch, "_runtime_policy", None) or getattr(orch, "runtime_policy", None)
    if pol is not None:
        raw_tier = pol.get("policy_tier") if isinstance(pol, dict) else getattr(pol, "policy_tier", "")
        policy_tier = str(raw_tier or "").lower()
        if remaining_chars is None:
            ctx_w = pol.get("context_window") if isinstance(pol, dict) else getattr(pol, "context_window", 0)
            if isinstance(ctx_w, int) and ctx_w > 0:
                remaining_chars = ctx_w * 4

    char_limit = max_chars
    if remaining_chars is not None:
        char_limit = min(max_chars, max(500, int(remaining_chars * 0.40)))

    line_limit = 500 if policy_tier in ("compact", "nano") else None
    return char_limit, line_limit


@tool(
    tags=["coding", "planning", "debug", "review"],
    permission_kind=PermissionKind.READ_FILE,
)
def read_file(
    path: str,
    summarize: bool = False,
    start_line: int | None = None,
    end_line: int | None = None,
    workdir: Path | None = None,
) -> Dict[str, Any]:
    """Read UTF-8 contents of a file, optionally sliced to 1-based inclusive [start_line, end_line]."""
    if workdir is None:
        workdir = Path.cwd()
    safety_err = _check_file_safety(path, workdir=workdir, for_write=False)
    if safety_err is not None:
        return safety_err
    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    if _is_secret_path(p):
        return {
            "path": str(p),
            "status": "error",
            "error": f"Access denied: '{p.name}' is a protected secret file. Reading credentials and private keys is restricted.",
        }

    if p.is_dir():
        _mark_file_read(str(p.resolve()))
        items = [c.name for c in sorted(p.iterdir()) if c.name not in _OS_JUNK]
        return {
            "path": str(p),
            "status": "ok",
            "content": f"Directory containing {len(items)} items: {', '.join(items[:50])}",
            "is_directory": True,
        }

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

    content = _redact_secrets(content)
    all_lines = content.splitlines(keepends=True)
    total_lines = len(all_lines)

    # Explicit line-range slicing (1-based inclusive)
    if start_line is not None or end_line is not None:
        s = max(1, int(start_line)) if start_line is not None else 1
        e = min(total_lines, int(end_line)) if end_line is not None else total_lines
        if s > total_lines and total_lines > 0:
            sliced_lines: list[str] = []
        else:
            sliced_lines = all_lines[s - 1 : max(s - 1, e)]
        sliced = "".join(
            (ln[:_READ_FILE_MAX_LINE] + "… [line truncated]\n")
            if len(ln) > _READ_FILE_MAX_LINE
            else ln
            for ln in sliced_lines
        )
        try:
            _mark_file_read(str(p.resolve()))
        except Exception:
            pass
        res_slice: Dict[str, Any] = {
            "path": str(p),
            "status": "ok",
            "content": sliced,
            "start_line": s,
            "end_line": min(e, total_lines),
            "total_lines": total_lines,
        }
        try:
            from src.core.context.instruction_files import maybe_collect_nested_instructions

            nested = maybe_collect_nested_instructions(p, workdir)
            if nested:
                res_slice["nested_instructions"] = nested
        except Exception:
            pass
        return res_slice

    # TASK-30: Context-Aware Read Guard (triggered only when no line range requested)
    if not summarize:
        char_limit, line_limit = _resolve_read_guard_limits()
        exceeds_chars = char_limit is not None and len(content) > char_limit
        exceeds_lines = line_limit is not None and total_lines > line_limit
        if exceeds_chars or exceeds_lines:
            preview_lines = [
                (ln[:_READ_FILE_MAX_LINE] + "… [line truncated]\n")
                if len(ln) > _READ_FILE_MAX_LINE
                else ln
                for ln in all_lines[:30]
            ]
            # CRITICAL: Do NOT call _mark_file_read here — a preview must not unlock blind full-file overwrite.
            return {
                "path": str(p),
                "status": "ok",
                "truncated_by_read_guard": True,
                "total_lines": total_lines,
                "total_chars": len(content),
                "content": "".join(preview_lines),
                "hint": (
                    f"File is large ({total_lines} lines, {len(content)} chars). "
                    f"Use grep or read_file(path={path!r}, start_line=..., end_line=...) to inspect relevant sections."
                ),
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
        summary = _redact_secrets(summary)
        return {"path": str(p), "status": "ok", "content": summary, "truncated": True}

    # Per-line cap: single pass — avoids scanning lines twice.
    capped = []
    any_capped = False
    for ln in all_lines:
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
    try:
        from src.core.context.instruction_files import maybe_collect_nested_instructions

        nested = maybe_collect_nested_instructions(p, workdir)
        if nested:
            result["nested_instructions"] = nested
    except Exception:
        pass
    return result


@tool(
    side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE
)
def delete_file(
    path: str,
    workdir: Path | None = None,
    user_approved: bool = False,  # type: ignore[assignment]
) -> Dict[str, Any]:
    """Delete a file or directory at the specified path within the workspace."""
    if workdir is None:
        workdir = Path.cwd()
    workdir_resolved = Path(workdir).resolve()
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard(workspace_root=workdir_resolved)
    guard_result = guard.guard_operation("delete_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

    try:
        p = _safe_resolve(path, workdir)
        if p.resolve() == workdir_resolved:
            return {"status": "error", "error": "Cannot delete the workspace root directory."}

        guard_res_resolved = guard.guard_operation("delete_file", str(p), user_approved)
        if guard_res_resolved.get("status") == "error":
            return {"path": str(p), "status": "error", "error": guard_res_resolved.get("error")}

        try:
            _check_project_deny_write(p, workdir)
        except PermissionError as _pe:
            return {"path": path, "status": "error", "error": str(_pe)}

        # Read-before-write guardrail: deletion is destructive
        try:
            rbw = _check_read_before_write(str(p))
            if rbw:
                return {"path": str(p), "status": "error", **rbw}
        except Exception:
            pass

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

        try:
            from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

            if is_current_tool_cancelled():
                return {
                    "path": str(p),
                    "status": "error",
                    "error": "Tool execution was cancelled or timed out before delete committed.",
                    "cancelled": True,
                }
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
    user_approved: bool = False,
) -> Dict[str, Any]:
    """Rename (move) a file within the workspace.

    Args:
        path: Current path of the file (also accepted as src_path, src, old_path).
        new_path: New path for the file (also accepted as dst_path, dst, new_name).
        user_approved: Whether explicit user approval was granted for protected paths.

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

    workdir_resolved = Path(workdir).resolve() if workdir is not None else Path.cwd().resolve()
    # WorkspaceGuard integration - check protected files for both paths
    guard = WorkspaceGuard(workspace_root=workdir_resolved)
    guard_src = guard.guard_operation("rename_file", resolved_src, user_approved)
    if guard_src.get("status") == "error":
        return {"path": resolved_src, "status": "error", "error": guard_src.get("error")}
    guard_dst = guard.guard_operation("rename_file", resolved_dst, user_approved)
    if guard_dst.get("status") == "error":
        return {"path": resolved_dst, "status": "error", "error": guard_dst.get("error")}

    try:
        src_resolved = _safe_resolve(resolved_src, workdir)
        dst_resolved = _safe_resolve(resolved_dst, workdir)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    guard_src_res = guard.guard_operation("rename_file", str(src_resolved), user_approved)
    if guard_src_res.get("status") == "error":
        return {"path": str(src_resolved), "status": "error", "error": guard_src_res.get("error")}
    guard_dst_res = guard.guard_operation("rename_file", str(dst_resolved), user_approved)
    if guard_dst_res.get("status") == "error":
        return {"path": str(dst_resolved), "status": "error", "error": guard_dst_res.get("error")}

    # Apply project-level deny_write patterns
    try:
        _check_project_deny_write(src_resolved, workdir)
        _check_project_deny_write(dst_resolved, workdir)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    if not src_resolved.exists():
        return {"path": str(src_resolved), "status": "not_found"}

    # Read-before-write guardrail: rename is destructive on the source file
    try:
        rbw = _check_read_before_write(str(src_resolved))
        if rbw:
            return {"status": "error", **rbw}
    except Exception:
        pass

    try:
        # Reject overwrite if destination exists (safety guard)
        if dst_resolved.exists() and dst_resolved.resolve() != src_resolved.resolve():
            return {
                "status": "error",
                "error": f"destination exists: {dst_resolved}",
            }
        try:
            from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

            if is_current_tool_cancelled():
                return {
                    "status": "error",
                    "error": "Tool execution was cancelled or timed out before rename committed.",
                    "cancelled": True,
                }
        except Exception:
            pass

        dst_resolved.parent.mkdir(parents=True, exist_ok=True)
        src_resolved.rename(dst_resolved)
        return {"ok": True, "status": "ok", "renamed": str(dst_resolved)}
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}
    except Exception as e:
        return {"status": "error", "error": str(e)}
