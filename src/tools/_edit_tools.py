"""Edit tools — fuzzy find, edit_file, edit_by_line_range, edit_file_atomic, multiedit.

Extracted from file_tools.py.  All public names are re-exported from
src.tools.file_tools for backward compatibility.

Circular-import note: _safe_resolve is imported directly from _path_utils
(the underlying implementation) rather than from file_tools, to avoid a
circular dependency.  The _safe_resolve wrapper in file_tools.py remains
the authoritative public symbol for test compatibility.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional

from src.tools._edit_matcher import (  # noqa: F401
    _EditMatch,
    _LINE_NUM_PREFIX_RE,
    _apply_edit_match,
    _common_indent_prefix,
    _dedent_lines,
    _find_closest_block_hint,
    _find_edit_match,
    _fuzzy_find,
    _reindent_replacement,
    _slice_for_line_window,
    _strip_line_number_prefixes,
)


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically using a sibling temp file + os.replace.

    Guarantees that readers either see the old or the new content — never a
    partially-written file — even if the process is interrupted mid-write.
    """
    _target = path.resolve()
    try:
        from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

        if is_current_tool_cancelled():
            raise TimeoutError("Tool execution was cancelled or timed out before write committed.")
    except ImportError:
        pass

    orig_mode: Optional[int] = None
    try:
        if _target.exists():
            orig_mode = stat.S_IMODE(_target.stat().st_mode)
    except OSError:
        orig_mode = None

    fd, tmp = tempfile.mkstemp(
        dir=str(_target.parent),
        prefix=f".{_target.stem}.write.",
        suffix=_target.suffix or ".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                pass
        if orig_mode is not None:
            try:
                os.chmod(tmp, orig_mode)
            except OSError:
                pass
        try:
            from src.core.orchestration.tool_execution_pipeline import is_current_tool_cancelled

            if is_current_tool_cancelled():
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise TimeoutError("Tool execution was cancelled or timed out before write committed.")
        except ImportError:
            pass
        try:
            os.replace(tmp, str(_target))
        except OSError:
            shutil.move(tmp, str(_target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _invalidate_context_cache(path: Path) -> None:
    """Invalidate ContextBuilder cache for *path* (best-effort, non-fatal)."""
    try:
        from src.core.context.context_builder import ContextBuilder as _CB

        _CB.invalidate_path(str(path))
    except Exception:
        pass

from src.tools._tool import tool, PermissionKind  # noqa: E402
from src.tools._diff_gate import (  # noqa: E402
    _publish_diff_preview,
    register_preview_gate,
)
from src.tools._lint_verify import verify_candidate_content as _verify_new_content  # noqa: E402

_logger = logging.getLogger(__name__)

# Net-change warning threshold (authoritative value — re-exported from file_tools)
_EDIT_NET_CHANGE_WARN = 200  # edit_file warns on large net-line changes


class UnifiedDiffError(ValueError):
    """Raised when a unified diff cannot be safely applied to its source."""


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _apply_unified_diff(content: str, patch: str) -> str:
    """Apply a single-file unified diff without relying on an external binary.

    The source line at every context/removal operation must match exactly.  The
    caller writes the returned value only after this function completes, so a
    malformed or stale patch cannot partially modify the target file.
    """
    source_lines = content.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    patch_index = 0
    saw_hunk = False

    while patch_index < len(patch_lines):
        line = patch_lines[patch_index]
        if line.startswith(("--- ", "+++ ")):
            patch_index += 1
            continue

        match = _HUNK_HEADER_RE.match(line)
        if not match:
            raise UnifiedDiffError(f"Expected hunk header, got {line.rstrip()!r}")

        saw_hunk = True
        old_start = int(match.group("old_start"))
        old_count = int(match.group("old_count") or "1")
        new_count = int(match.group("new_count") or "1")
        hunk_source_index = max(0, old_start - 1)
        if hunk_source_index < source_index or hunk_source_index > len(source_lines):
            raise UnifiedDiffError("Hunk position does not match the source file")

        output.extend(source_lines[source_index:hunk_source_index])
        source_index = hunk_source_index
        patch_index += 1
        consumed_old = 0
        produced_new = 0

        while patch_index < len(patch_lines):
            hunk_line = patch_lines[patch_index]
            if _HUNK_HEADER_RE.match(hunk_line):
                break
            if hunk_line.startswith("\\ No newline at end of file"):
                patch_index += 1
                continue
            if not hunk_line:
                raise UnifiedDiffError("Malformed empty line in unified diff")

            operation, expected = hunk_line[0], hunk_line[1:]
            if operation == " ":
                if source_index >= len(source_lines) or source_lines[source_index] != expected:
                    raise UnifiedDiffError("Patch context does not match the source file")
                output.append(source_lines[source_index])
                source_index += 1
                consumed_old += 1
                produced_new += 1
            elif operation == "-":
                if source_index >= len(source_lines) or source_lines[source_index] != expected:
                    raise UnifiedDiffError("Patch removal does not match the source file")
                source_index += 1
                consumed_old += 1
            elif operation == "+":
                output.append(expected)
                produced_new += 1
            else:
                raise UnifiedDiffError(f"Unexpected unified diff operation {operation!r}")
            patch_index += 1

        if consumed_old != old_count or produced_new != new_count:
            raise UnifiedDiffError(
                "Hunk line counts do not match its header "
                f"(expected -{old_count}/+{new_count}, got -{consumed_old}/+{produced_new})"
            )

    if not saw_hunk:
        raise UnifiedDiffError("Unified diff does not contain a hunk")

    output.extend(source_lines[source_index:])
    return "".join(output)


def _is_in_workspace(path: Path, workdir: Path) -> bool:
    """Return True iff *path* is strictly inside *workdir* (no escaping via ..)."""
    try:
        path.resolve().relative_to(workdir.resolve())
        return True
    except ValueError:
        return False



def _count_diff_changes(diff_lines: list[str]) -> tuple[int, int]:
    """Count added/removed content lines in a unified diff, excluding +++ / --- headers."""
    lines_added = sum(
        1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++")
    )
    lines_removed = sum(
        1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---")
    )
    return lines_added, lines_removed


def _read_original_content(p: Path) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Read original content of a file for editing, returning (content, None) on success or (None, err_dict) if binary/unreadable."""
    try:
        with p.open("rb") as fh:
            header = fh.read(512)
            if b"\x00" in header:
                return None, {
                    "path": str(p),
                    "status": "error",
                    "error": f"Cannot edit binary file: '{p.name}'. Editing binary files as text is not supported.",
                }
            raw = header + fh.read()
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError:
            return None, {
                "path": str(p),
                "status": "error",
                "error": f"Cannot edit binary or non-UTF-8 file: '{p.name}'. File is not valid UTF-8.",
            }
    except Exception as exc:
        return None, {
            "path": str(p),
            "status": "error",
            "error": f"Failed to read file '{p.name}': {exc}",
        }


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
def edit_file(
    path: str,
    patch: str,
    workdir: Path | None = None,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """Edit a file using a unified diff patch. Returns diff in result for TUI display."""
    if workdir is None:
        workdir = Path.cwd()
    from src.tools._file_io import _pre_write_check

    p, pre_err = _pre_write_check(
        path,
        Path(workdir),
        operation="edit_file",
        user_approved=user_approved,
        check_binary=True,
        check_read=True,
    )
    if pre_err is not None or p is None:
        return pre_err or {"path": path, "status": "error", "error": "Pre-write check failed"}
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    import difflib

    # Read original content BEFORE modification for diff generation
    original_content, err_dict = _read_original_content(p)
    if err_dict is not None or original_content is None:
        return err_dict or {"path": str(p), "status": "error", "error": "Unable to read file"}

    if not patch.strip().startswith("---") and not patch.strip().startswith("@@"):
        return {
            "path": str(p),
            "status": "error",
            "error": "Invalid patch format. Must be unified diff.",
        }

    try:
        new_content = _apply_unified_diff(original_content, patch)
    except UnifiedDiffError as exc:
        return {
            "path": str(p),
            "status": "error",
            "error": f"Patch failed: {exc}",
        }

    ver_err = _verify_new_content(p, new_content, workdir)
    if ver_err:
        return ver_err

    _atomic_write(p, new_content)
    _invalidate_context_cache(p)

    # Generate unified diff for TUI display
    original_lines = original_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm="\n"
        )
    )
    diff = "".join(diff_lines)

    lines_added, lines_removed = _count_diff_changes(diff_lines)

    # M4: Publish diff preview (post-apply for edit_file since patch is atomic)
    _publish_diff_preview(str(p), diff, is_new_file=False)

    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }
    # F13: Signal when a patch is unreasonably large — agent should split the task.
    net_changed = lines_added + lines_removed
    if net_changed > _EDIT_NET_CHANGE_WARN:
        result["requires_split"] = True
        result["error"] = (
            f"edit_file patch changed {net_changed} lines in a single call. "
            "Split into multiple targeted edits."
        )
    return result


@tool(side_effects=["write"], tags=["coding"], permission_kind=PermissionKind.WRITE_FILE)
def edit_by_line_range(
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    workdir: Path | None = None,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """
    Replace lines [start_line, end_line] (1-indexed, inclusive) in a file with new_content.
    Returns a unified diff identical in shape to edit_file.

    F6: Required for precise multi-line replacements without full-file rewrites.
    Integrated with WorkspaceGuard and safe_resolve for security.
    """
    if workdir is None:
        workdir = Path.cwd()

    # P2-6: Coerce to int — LLM may pass string representations
    try:
        start_line = int(start_line)
        end_line = int(end_line)
    except (TypeError, ValueError) as _ce:
        return {
            "path": path,
            "status": "error",
            "error": f"start_line/end_line must be integers: {_ce}",
        }

    from src.tools._file_io import _pre_write_check

    try:
        p, pre_err = _pre_write_check(
            path,
            Path(workdir),
            operation="edit_by_line_range",
            user_approved=user_approved,
            check_binary=True,
            check_read=True,
        )
    except (PermissionError, ValueError) as exc:
        return {"path": path, "status": "error", "error": str(exc)}
    if pre_err is not None or p is None:
        return pre_err or {"path": path, "status": "error", "error": "Pre-write check failed"}

    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    original_content, err_dict = _read_original_content(p)
    if err_dict is not None or original_content is None:
        return err_dict or {"path": str(p), "status": "error", "error": "Unable to read file"}
    original_lines = original_content.splitlines(keepends=True)
    total_lines = len(original_lines)

    if start_line < 1 or end_line < start_line or start_line > total_lines:
        return {
            "path": str(p),
            "status": "error",
            "error": (
                f"Invalid line range [{start_line}, {end_line}] for file with {total_lines} lines. "
                "start_line must be >= 1 and <= total_lines, end_line >= start_line."
            ),
        }

    # Clamp end_line to file length
    end_line = min(end_line, total_lines)

    # Build replacement lines (ensure trailing newline on last line)
    replacement = new_content
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    replacement_lines = replacement.splitlines(keepends=True) if replacement else []

    # Splice: lines before + replacement + lines after
    new_lines = (
        original_lines[: start_line - 1] + replacement_lines + original_lines[end_line:]
    )
    new_content_str = "".join(new_lines)
    # PRE-WRITE VERIFICATION
    ver_err = _verify_new_content(p, new_content_str, workdir)
    if ver_err:
        return ver_err

    _atomic_write(p, new_content_str)
    _invalidate_context_cache(p)

    from src.tools.patch_tools import generate_unified_diff as _gen_diff

    diff = _gen_diff(
        original_content, new_content_str, from_file=str(p), to_file=str(p)
    )
    diff_lines = diff.splitlines(keepends=True)

    # M4: Publish diff preview (post-write since diff requires splice result)
    _publish_diff_preview(str(p), diff, is_new_file=False)

    lines_added, lines_removed = _count_diff_changes(diff_lines)
    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }
    try:
        from src.core.context.instruction_files import maybe_collect_nested_instructions

        nested = maybe_collect_nested_instructions(p, Path(workdir))
        if nested:
            result["nested_instructions"] = nested
    except Exception:
        pass

    return result


@tool(
    side_effects=["write"],
    tags=["coding"],
    permission_kind=PermissionKind.WRITE_FILE,
    description=(
        "edit_file_atomic(path, old_string, new_string) -> "
        "Replace old_string (must appear exactly once) with new_string. "
        "Preferred for surgical edits: no line-number drift, fails loudly if ambiguous."
    ),
)
def edit_file_atomic(
    path: str,
    old_string: str,
    new_string: str,
    workdir: Path | None = None,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """
    Edit a file by replacing an exact string occurrence with new_string.

    old_string must appear exactly once in the file. If it appears zero times
    the edit is rejected (nothing to replace). If it appears more than once the
    edit is rejected to prevent ambiguous changes — make old_string longer to
    uniquely identify the target location.

    Returns a unified diff in the result, identical in shape to edit_file.
    """
    if workdir is None:
        workdir = Path.cwd()
    from src.tools._file_io import _pre_write_check

    _check_read = True
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
        from src.core.orchestration.tool_output_reducer import is_surgical_preview_edit_allowed

        _orch = _PARENT_ORCHESTRATOR_VAR.get(None)
        if _orch is not None and is_surgical_preview_edit_allowed(
            _orch, "edit_file_atomic", {"old_string": old_string}, str((Path(workdir) / path).resolve())
        ):
            _check_read = False
    except Exception:
        pass

    p, pre_err = _pre_write_check(
        path,
        Path(workdir),
        operation="edit_file_atomic",
        user_approved=user_approved,
        check_binary=True,
        check_read=_check_read,
    )
    if pre_err is not None or p is None:
        return pre_err or {"path": path, "status": "error", "error": "Pre-write check failed"}
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    original_content, err_dict = _read_original_content(p)
    if err_dict is not None or original_content is None:
        return err_dict or {"path": str(p), "status": "error", "error": "Unable to read file"}

    match, match_err = _find_edit_match(original_content, old_string)
    if match_err and match_err.startswith("ambiguous:"):
        _, strat, cnt = match_err.split(":", 2)
        return {
            "path": str(p),
            "status": "error",
            "error": (
                f"old_string appears {cnt} times in the file (strategy={strat}); "
                "it must appear exactly once for a safe atomic edit. "
                "Add more surrounding context to old_string to make it unique."
            ),
        }

    if match is None:
        hint = _find_closest_block_hint(original_content, old_string)
        msg = (
            "old_string not found in file (tried exact match, line-number prefix stripping, "
            "trailing whitespace, normalised whitespace, and indentation-flexible passes). "
            "Ensure old_string is a verbatim copy of the file content."
        )
        if hint:
            msg = f"{msg}\n\n{hint}"
        return {
            "path": str(p),
            "status": "error",
            "error": msg,
        }

    new_content = _apply_edit_match(original_content, match, new_string)

    # D-04: Idempotency guard — skip write when replacement produces identical content
    if new_content == original_content:
        return {
            "path": str(p),
            "status": "no_change",
            "diff": "",
            "lines_added": 0,
            "lines_removed": 0,
        }

    # PRE-WRITE VERIFICATION
    ver_err = _verify_new_content(p, new_content, workdir)
    if ver_err:
        return ver_err

    # PREV-1: Blocking diff preview gate (fail-closed on timeout or error).
    _require_preview = False
    try:
        from src.tools.tools_config import requires_preview_confirmation

        _require_preview = bool(requires_preview_confirmation())
    except Exception:
        _require_preview = False

    try:
        from src.tools.patch_tools import generate_unified_diff as _preview_diff

        _preview_text = _preview_diff(
            original_content, new_content, from_file=str(p), to_file=str(p)
        )
        _path_key = str(p)

        from src.core.orchestration.event_bus import get_event_bus as _get_bus

        _has_preview_sub = _get_bus().has_subscribers("file.diff.preview")

        if _require_preview and _has_preview_sub:
            _gate_ev = register_preview_gate(_path_key)
            _publish_diff_preview(_path_key, _preview_text, is_new_file=False)
            _signaled = bool(_gate_ev.wait(timeout=300.0))
            from src.tools._diff_gate import pop_preview_rejection

            _was_rejected = pop_preview_rejection(_path_key)
            if not _signaled or _was_rejected:
                return {
                    "path": str(p),
                    "status": "rejected",
                    "message": (
                        "Edit rejected by user."
                        if _was_rejected
                        else "Edit preview confirmation timed out (fail-closed)."
                    ),
                }
        else:
            _publish_diff_preview(_path_key, _preview_text, is_new_file=False)
    except Exception as _gate_exc:
        if _require_preview:
            _logger.error("PREV-1 preview gate failed closed: %s", _gate_exc)
            return {
                "path": str(p),
                "status": "rejected",
                "error": f"Preview confirmation gate error (fail-closed): {_gate_exc}",
            }
        _logger.debug(
            "PREV-1 preview publish error (non-fatal): %s", _gate_exc
        )

    _atomic_write(p, new_content)
    _invalidate_context_cache(p)

    # P9: Auto-format after write (best-effort; never blocks)
    try:
        from src.tools.verification_tools import format_file as _fmt

        _fmt_result = _fmt(str(p))
        # Re-read after formatting so diff reflects formatted output
        if _fmt_result.get("status") == "ok":
            new_content = p.read_text(encoding="utf-8")
    except Exception:
        pass

    from src.tools.patch_tools import generate_unified_diff as _gen_diff

    diff = _gen_diff(original_content, new_content, from_file=str(p), to_file=str(p))
    diff_lines = diff.splitlines(keepends=True)

    lines_added, lines_removed = _count_diff_changes(diff_lines)
    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }
    try:
        from src.core.context.instruction_files import maybe_collect_nested_instructions

        nested = maybe_collect_nested_instructions(p, Path(workdir))
        if nested:
            result["nested_instructions"] = nested
    except Exception:
        pass

    return result


@tool(
    side_effects=["write"],
    tags=["coding"],
    permission_kind=PermissionKind.WRITE_FILE,
    description=(
        "multiedit(path, edits) -> Apply multiple old_string→new_string edits to a single "
        "file atomically.  All replacements are validated in memory first; the file is "
        "written only if every edit succeeds.  Use instead of multiple edit_file_atomic "
        "calls to avoid re-reading the file between edits."
    ),
)
def multiedit(
    path: str,
    edits: list,
    workdir: Optional[Path] = None,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """Apply multiple string replacements to a single file atomically.

    All edits are applied in-memory in the order given.  If any replacement
    fails (string not found or found multiple times), the entire operation is
    aborted and the file is left unchanged.

    Args:
        path: File path relative to workdir.
        edits: List of {"old_string": "...", "new_string": "..."} dicts.
               Applied in order; each edit sees the result of the previous one.
        workdir: Working directory.
        user_approved: WorkspaceGuard override (rarely needed).

    Returns:
        status, path, diff, edits_applied (count), lines_added, lines_removed.
    """
    if workdir is None:
        workdir = Path.cwd()
    import difflib

    if not edits or not isinstance(edits, list):
        return {
            "path": path,
            "status": "error",
            "error": "edits must be a non-empty list",
        }

    from src.tools._file_io import _pre_write_check

    p, pre_err = _pre_write_check(
        path,
        Path(workdir),
        operation="multiedit",
        user_approved=user_approved,
        check_binary=True,
        check_read=True,
    )
    if pre_err is not None or p is None:
        return pre_err or {"path": path, "status": "error", "error": "Pre-write check failed"}
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    original_content, err_dict = _read_original_content(p)
    if err_dict is not None or original_content is None:
        return err_dict or {"path": str(p), "status": "error", "error": "Unable to read file"}
    working_content = original_content

    for i, edit in enumerate(edits):
        if (
            not isinstance(edit, dict)
            or "old_string" not in edit
            or "new_string" not in edit
        ):
            return {
                "path": str(p),
                "status": "error",
                "error": f"Edit #{i + 1} must have 'old_string' and 'new_string' keys.",
            }
        old = edit["old_string"]
        new = edit["new_string"]
        match, match_err = _find_edit_match(working_content, old)
        if match_err and match_err.startswith("ambiguous:"):
            _, strat, cnt = match_err.split(":", 2)
            return {
                "path": str(p),
                "status": "error",
                "error": (
                    f"Edit #{i + 1}: old_string appears {cnt} times (strategy={strat}); "
                    "must appear exactly once. Add more surrounding context to make it unique."
                ),
                "edit_index": i,
            }
        if match is None:
            hint = _find_closest_block_hint(working_content, old)
            msg = f"Edit #{i + 1}: old_string not found in file content."
            if hint:
                msg = f"{msg}\n\n{hint}"
            return {
                "path": str(p),
                "status": "error",
                "error": msg,
                "edit_index": i,
            }
        working_content = _apply_edit_match(working_content, match, new)

    # All edits validated — write once
    # PRE-WRITE VERIFICATION
    ver_err = _verify_new_content(p, working_content, workdir)
    if ver_err:
        return ver_err

    _atomic_write(p, working_content)
    _invalidate_context_cache(p)

    # P9: Auto-format after write (best-effort)
    try:
        from src.tools.verification_tools import format_file as _fmt

        _fmt_result = _fmt(str(p))
        if _fmt_result.get("status") == "ok":
            working_content = p.read_text(encoding="utf-8")
    except Exception:
        pass

    original_lines = original_content.splitlines(keepends=True)
    new_lines = working_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm="\n"
        )
    )
    diff = "".join(diff_lines)

    lines_added, lines_removed = _count_diff_changes(diff_lines)
    return {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "edits_applied": len(edits),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }
