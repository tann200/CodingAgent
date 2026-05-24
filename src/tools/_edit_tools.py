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
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically using a sibling temp file + os.replace.

    Guarantees that readers either see the old or the new content — never a
    partially-written file — even if the process is interrupted mid-write.
    """
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.write.",
        suffix=path.suffix or ".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
        os.replace(tmp, str(path))
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

from src.tools._path_utils import safe_resolve as _safe_resolve  # noqa: E402
from src.tools._tool import tool  # noqa: E402
from src.tools._diff_gate import (  # noqa: E402
    _publish_diff_preview,
    register_preview_gate,
)
from src.tools._workspace_guard import WorkspaceGuard  # noqa: E402
from src.tools._lint_verify import verify_candidate_content as _verify_new_content  # noqa: E402

_logger = logging.getLogger(__name__)

# Net-change warning threshold (authoritative value — re-exported from file_tools)
_EDIT_NET_CHANGE_WARN = 200  # edit_file warns on large net-line changes


def _is_in_workspace(path: Path, workdir: Path) -> bool:
    """Return True iff *path* is strictly inside *workdir* (no escaping via ..)."""
    try:
        path.resolve().relative_to(workdir.resolve())
        return True
    except ValueError:
        return False



def _fuzzy_find(content: str, target: str) -> Optional[str]:
    """Find target in content using progressively looser matching strategies.

    Returns the actual substring in content that matches target (for use as the
    replacement key), or None if no strategy succeeds.

    Strategies tried in order:
      1. Exact match (caller should check first — this is the fallback chain)
      2. Trailing-whitespace normalisation per line
      3. Internal-whitespace normalisation (tabs+spaces → single space)
      4. Leading-indentation-flexible match

    Only returns a match if it appears exactly once — ambiguous fuzzy matches
    are rejected to preserve the same safety guarantee as exact matching.
    """
    import re as _re

    old_lines = target.splitlines()
    n = len(old_lines)
    if n == 0:
        return None

    content_lines = content.splitlines(keepends=True)
    total = len(content_lines)

    def _rstrip_join(s: str) -> str:
        return "\n".join(ln.rstrip() for ln in s.splitlines())

    def _norm(s: str) -> str:
        return "\n".join(_re.sub(r"[ \t]+", " ", ln).rstrip() for ln in s.splitlines())

    def _strip_indent(s: str) -> str:
        ls = s.splitlines()
        min_ind = min(
            (len(line) - len(line.lstrip()) for line in ls if line.strip()), default=0
        )
        return "\n".join(line[min_ind:] if len(line) > min_ind else line for line in ls)

    stripped_target = _rstrip_join(target)
    norm_target = _norm(target)
    indent_target = _strip_indent(target)

    for start in range(total - n + 1):
        chunk = "".join(content_lines[start : start + n])
        # Strategy 1: trailing whitespace
        if _rstrip_join(chunk) == stripped_target:
            if content.count(chunk) == 1:
                return chunk
        # Strategy 2: normalised whitespace
        if _norm(chunk) == norm_target:
            if content.count(chunk) == 1:
                return chunk
        # Strategy 3: indentation-flexible
        if _strip_indent(chunk) == indent_target:
            if content.count(chunk) == 1:
                return chunk

    return None


@tool(side_effects=["write"], tags=["coding"])
def edit_file(
    path: str,
    patch: str,
    workdir: Path | None = None,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """Edit a file using a unified diff patch. Returns diff in result for TUI display."""
    if workdir is None:
        workdir = Path.cwd()
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("edit_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

    p = _safe_resolve(path, workdir)

    # GAP-S1: Read-before-write guardrail (check after resolve so path is absolute)
    try:
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(str(p))
        if rbw:
            return {"path": str(p), "status": "error", **rbw}
    except Exception:
        pass
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    import difflib

    # Read original content BEFORE modification for diff generation
    original_content = p.read_text(encoding="utf-8")

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
        patch_file = f.name

    tmp_target_path: str | None = None

    try:
        if not patch.strip().startswith("---") and not patch.strip().startswith("@@"):
            return {
                "path": str(p),
                "status": "error",
                "error": "Invalid patch format. Must be unified diff.",
            }

        fd, tmp_target_path = tempfile.mkstemp(
            dir=str(p.parent),
            prefix=f".{p.stem}.patch.",
            suffix=p.suffix or ".tmp",
        )
        os.close(fd)
        Path(tmp_target_path).write_text(original_content, encoding="utf-8")

        # Apply unified diff.
        proc = subprocess.run(
            ["patch", "-u", "-f", tmp_target_path, "-i", patch_file],
            capture_output=True,
            text=True,
            check=False,
        )

        if proc.returncode != 0:
            return {
                "path": str(p),
                "status": "error",
                "error": f"Patch failed code {proc.returncode}:\n{proc.stdout}\n{proc.stderr}",
            }

        new_content = Path(tmp_target_path).read_text(encoding="utf-8")
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

        lines_added = len([line for line in diff_lines if line.startswith("+")])
        lines_removed = len([line for line in diff_lines if line.startswith("-")])

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
    finally:
        try:
            os.remove(patch_file)
        except OSError:
            pass
        if tmp_target_path:
            try:
                os.remove(tmp_target_path)
            except OSError:
                pass


@tool(side_effects=["write"], tags=["coding"])
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

    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("edit_by_line_range", path, user_approved)
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

    try:
        p = _safe_resolve(path, workdir)
    except (PermissionError, ValueError) as exc:
        return {"path": path, "status": "error", "error": str(exc)}

    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    # P2-6: Coerce to int — LLM may pass string representations
    try:
        start_line = int(start_line)
        end_line = int(end_line)
    except (TypeError, ValueError) as _ce:
        return {
            "path": str(p),
            "status": "error",
            "error": f"start_line/end_line must be integers: {_ce}",
        }

    original_content = p.read_text(encoding="utf-8")
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

    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": len([ln for ln in diff_lines if ln.startswith("+")]),
        "lines_removed": len([ln for ln in diff_lines if ln.startswith("-")]),
    }

    return result


@tool(
    side_effects=["write"],
    tags=["coding"],
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

    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("edit_file_atomic", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

    p = _safe_resolve(path, workdir)

    # GAP-S1: Read-before-write guardrail (check after resolve so path is absolute)
    try:
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(str(p))
        if rbw:
            return {"path": str(p), "status": "error", **rbw}
    except Exception:
        pass
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    original_content = p.read_text(encoding="utf-8")

    count = original_content.count(old_string)
    if count > 1:
        return {
            "path": str(p),
            "status": "error",
            "error": (
                f"old_string appears {count} times in the file; it must appear exactly once "
                "for a safe atomic edit. Add more surrounding context to old_string to make "
                "it unique."
            ),
        }

    # Fuzzy match fallback when exact match fails.
    matched_old = (
        old_string if count == 1 else _fuzzy_find(original_content, old_string)
    )

    if matched_old is None:
        return {
            "path": str(p),
            "status": "error",
            "error": (
                "old_string not found in file (tried exact match and fuzzy strategies: "
                "trailing whitespace, normalised whitespace, indentation-flexible). "
                "Ensure old_string is a verbatim copy of the file content."
            ),
        }

    new_content = original_content.replace(matched_old, new_string, 1)

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

    # PREV-1: Blocking diff preview gate.
    # When require_preview_confirmation is True AND a TUI is subscribed to
    # file.diff.preview, publish the diff and block until the user accepts
    # or rejects it.  When confirmation is not required (default), publish
    # for informational display and proceed immediately.
    try:
        from src.tools.tools_config import requires_preview_confirmation
        from src.tools.patch_tools import generate_unified_diff as _preview_diff

        _preview_text = _preview_diff(
            original_content, new_content, from_file=str(p), to_file=str(p)
        )
        _path_key = str(p)

        from src.core.orchestration.event_bus import get_event_bus as _get_bus

        _has_preview_sub = _get_bus().has_subscribers("file.diff.preview")

        if requires_preview_confirmation() and _has_preview_sub:
            _gate_ev = register_preview_gate(_path_key)
            _publish_diff_preview(_path_key, _preview_text, is_new_file=False)
            # Block for up to 5 minutes waiting for user decision
            _gate_ev.wait(timeout=300.0)
            # Use helper to check/clear rejection state atomically
            from src.tools._diff_gate import pop_preview_rejection

            _was_rejected = pop_preview_rejection(_path_key)
            if _was_rejected:
                return {
                    "path": str(p),
                    "status": "rejected",
                    "message": "Edit rejected by user.",
                }
        else:
            # No gate — publish for informational display and proceed immediately
            _publish_diff_preview(_path_key, _preview_text, is_new_file=False)
    except Exception as _gate_exc:
        _logger.debug(
            "PREV-1 preview gate error (non-fatal, proceeding): %s", _gate_exc
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

    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": len([ln for ln in diff_lines if ln.startswith("+")]),
        "lines_removed": len([ln for ln in diff_lines if ln.startswith("-")]),
    }

    return result


@tool(
    side_effects=["write"],
    tags=["coding"],
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

    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("multiedit", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

    try:
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(path)
        if rbw:
            return {"path": path, "status": "error", **rbw}
    except Exception:
        pass

    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    original_content = p.read_text(encoding="utf-8")
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
        count = working_content.count(old)
        if count > 1:
            return {
                "path": str(p),
                "status": "error",
                "error": (
                    f"Edit #{i + 1}: old_string appears {count} times; must appear exactly once. "
                    "Add more surrounding context to make it unique."
                ),
                "edit_index": i,
            }
        matched = old if count == 1 else _fuzzy_find(working_content, old)
        if matched is None:
            return {
                "path": str(p),
                "status": "error",
                "error": f"Edit #{i + 1}: old_string not found in file content.",
                "edit_index": i,
            }
        working_content = working_content.replace(matched, new, 1)

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

    return {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "edits_applied": len(edits),
        "lines_added": len([ln for ln in diff_lines if ln.startswith("+")]),
        "lines_removed": len([ln for ln in diff_lines if ln.startswith("-")]),
    }
