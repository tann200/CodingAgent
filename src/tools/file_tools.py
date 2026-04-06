from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Any, Optional

_logger = logging.getLogger(__name__)

# Provide a no-op fallback so pyright sees a concrete class even when
# src.core is unavailable.  The real import shadows it at runtime.


class WorkspaceGuard:
    """No-op guard when src.core is not available."""

    def guard_operation(self, *args: object, **kwargs: object) -> Dict[str, str]:
        return {"status": "ok"}


try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard  # type: ignore[assignment]
except ImportError:
    pass  # fallback class above is used


from src.tools._path_utils import safe_resolve
from src.tools._tool import tool
from src.tools._security import (
    DANGEROUS_PATTERNS,
    SAFE_COMMANDS,
    TEST_COMPILE_COMMANDS,
    RESTRICTED_COMMANDS,
    RESTRICTED_ALLOWED_SUBCOMMANDS,
    CODE_EXEC_INTERPRETERS,
    CODE_EXEC_FLAGS,
    TAR_EXTRACT_FLAGS,
    TAR_CREATE_FLAGS,
    GIT_SAFE_SUBCOMMANDS,
    SED_WRITE_FLAGS,
)


def _publish_diff_preview(path: str, diff: str, is_new_file: bool = False) -> None:
    """M4: Publish a diff preview event before a file write is applied.

    Subscribers (e.g. TUI) receive this to show the user what is about
    to change, giving them a chance to see (and in future, reject) edits.
    """
    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
        bus.publish(
            "file.diff.preview",
            {
                "path": path,
                "diff": diff,
                "is_new_file": is_new_file,
            },
        )
    except Exception as _exc:
        _logger.debug(
            "_publish_diff_preview: event bus unavailable (non-fatal): %s", _exc
        )
        pass  # Never block the write if event bus is unavailable


# ── TUI-05: blocking diff preview gate ────────────────────────────────────────
# edit_file_atomic() registers a threading.Event here before publishing the
# file.diff.preview event, then waits for it.  The orchestrator's
# preview.confirmed / preview.rejected handlers call resolve_preview_gate().
_pending_previews: dict[str, threading.Event] = {}
_preview_rejected: set[str] = set()
_preview_gate_lock: threading.Lock = threading.Lock()


def register_preview_gate(path_key: str) -> threading.Event:
    """Register a pending diff-preview approval; return the Event to wait on."""
    ev = threading.Event()
    with _preview_gate_lock:
        _pending_previews[path_key] = ev
    return ev


def resolve_preview_gate(path_key: str, approved: bool) -> None:
    """Resolve a pending diff preview gate.  Called from EventBus handler."""
    with _preview_gate_lock:
        if not approved:
            _preview_rejected.add(path_key)
        ev = _pending_previews.pop(path_key, None)
    if ev is not None:
        ev.set()


# Default working directory.  External projects should call
# ``tools_config.configure(default_workdir=Path("/my/project"))`` at startup
# rather than relying on this module-level constant.
DEFAULT_WORKDIR = Path.cwd()

# Output size caps — defined once at module level so both bash() and
# bash_readonly() share the same policy without local redefinition.
_BASH_STDOUT_MAX = 16_384
_BASH_STDERR_MAX = 6_000  # raised from 2 KB — Python tracebacks routinely exceed 2 KB
# Token-based caps: when the tokenizer is available, these token budgets take
# precedence so the LLM never receives unexpectedly large context from a single
# bash call.  Byte caps remain as a safety net when tiktoken is unavailable.
_BASH_STDOUT_MAX_TOKENS = 2_000  # ~8 KB of typical code at 1 tok ≈ 4 chars
_BASH_STDERR_MAX_TOKENS = 600  # enough for a full Python traceback
_READ_FILE_MAX_CHARS = 50_000
_READ_FILE_MAX_LINE = 2_000

# D-11: Named write-size constants so the two guards in write_file() are
# obviously consistent and easy to tune in a single place.
_WRITE_HARD_LINE_LIMIT = 500  # write_file hard-rejects inputs larger than this
_WRITE_WARN_LINE_LIMIT = 200  # write_file attaches requires_split warning above this
_EDIT_NET_CHANGE_WARN = 200  # edit_file warns on large net-line changes


def _truncate_bash_output(stdout: str, stderr: str) -> tuple[str, str, bool, bool]:
    """Cap bash stdout/stderr and append a notice when truncated.

    Returns (stdout, stderr, stdout_was_truncated, stderr_was_truncated).

    Truncation uses token budgets when ``src.core.inference.tokenizer`` is
    available (tiktoken or the character-heuristic fallback).  Byte caps are
    kept as a fast safety net for when the tokenizer cannot be imported.
    """
    try:
        from src.core.inference.tokenizer import count_tokens

        def _token_truncate(text: str, max_tokens: int, label: str) -> tuple[str, bool]:
            if not text:
                return text, False
            tok = count_tokens(text)
            if tok <= max_tokens:
                return text, False
            # Binary-search the char boundary that brings tokens under budget.
            lo, hi = 0, len(text)
            while hi - lo > 64:
                mid = (lo + hi) // 2
                if count_tokens(text[:mid]) < max_tokens:
                    lo = mid
                else:
                    hi = mid
            cut = lo
            omitted_tokens = tok - count_tokens(text[:cut])
            return (
                text[:cut]
                + f"\n... [{label} truncated: ~{omitted_tokens} tokens omitted, reason: size_limit]",
                True,
            )

        stdout, stdout_cut = _token_truncate(stdout, _BASH_STDOUT_MAX_TOKENS, "output")
        stderr, stderr_cut = _token_truncate(stderr, _BASH_STDERR_MAX_TOKENS, "stderr")
        return stdout, stderr, stdout_cut, stderr_cut

    except ImportError:
        pass

    # Byte-based fallback
    stdout_cut = False
    if len(stdout) > _BASH_STDOUT_MAX:
        omitted = len(stdout) - _BASH_STDOUT_MAX
        stdout = (
            stdout[:_BASH_STDOUT_MAX]
            + f"\n... [output truncated: {omitted} chars omitted, reason: size_limit]"
        )
        stdout_cut = True
    stderr_cut = False
    if len(stderr) > _BASH_STDERR_MAX:
        omitted = len(stderr) - _BASH_STDERR_MAX
        stderr = (
            stderr[:_BASH_STDERR_MAX]
            + f"\n... [stderr truncated: {omitted} chars omitted, reason: size_limit]"
        )
        stderr_cut = True
    return stdout, stderr, stdout_cut, stderr_cut


def _check_shell_flags(cmd_parts: list, first_cmd: str) -> Optional[Dict[str, Any]]:
    """Check for disallowed archive/inplace-edit flags.

    Returns an error dict if a blocked flag is found, else None.
    Shared by both ``bash()`` and ``bash_readonly()`` to avoid duplication.
    """
    if first_cmd == "sed":
        for _part in cmd_parts[1:]:
            if (
                _part == "-i"
                or _part == "--in-place"
                or _part.startswith("--in-place=")
                or (
                    _part.startswith("-")
                    and not _part.startswith("--")
                    and "i" in _part[1:]
                )
            ):
                return {
                    "status": "error",
                    "error": "sed -i (in-place edit) is not allowed. Use edit_file or edit_file_atomic instead.",
                }
    elif first_cmd == "tar":
        for part in cmd_parts[1:]:
            stripped = part.lstrip("-")
            if part in TAR_EXTRACT_FLAGS or (
                part.startswith("-") and not part.startswith("--") and "x" in stripped
            ):
                return {
                    "status": "error",
                    "error": "tar extract is not allowed. Use tar -t / --list to inspect archives.",
                }
            if part in TAR_CREATE_FLAGS or (
                part.startswith("-") and not part.startswith("--") and "c" in stripped
            ):
                return {
                    "status": "error",
                    "error": "tar archive creation is not allowed. SAFE_COMMANDS permits tar for inspection only.",
                }
    elif first_cmd == "unzip":
        if "-l" not in cmd_parts[1:]:
            return {
                "status": "error",
                "error": "unzip without -l (list) is not allowed. Use unzip -l to inspect archive contents.",
            }
    elif first_cmd == "env":
        if "-i" in cmd_parts[1:] or "--ignore-environment" in cmd_parts[1:]:
            return {
                "status": "error",
                "error": "env -i (clear environment) is not allowed.",
            }
    return None


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
        min_ind = min((len(l) - len(l.lstrip()) for l in ls if l.strip()), default=0)
        return "\n".join(l[min_ind:] if len(l) > min_ind else l for l in ls)

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


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    """Backward-compatible wrapper around the shared safe_resolve utility (#29)."""
    return safe_resolve(path, workdir)


@tool(side_effects=["write"], tags=["coding"])
def write_file(
    path: str,
    content: str,
    workdir: Path = DEFAULT_WORKDIR,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """Write content to a file. Returns diff in result for TUI display."""
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
        # New file - show all lines as added
        diff_lines = ["--- /dev/null\n", f"+++ {p}\n"]
        for i, line in enumerate(new_lines, 1):
            diff_lines.append(f"@@ -0,0 +{i} @@\n")
            diff_lines.append(line)
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

    # F14: Publish diff preview BEFORE writing so the TUI can show the proposed change
    _publish_diff_preview(str(p), diff, is_new_file=not bool(original_content))

    # Write new content after the preview event so the user sees it first
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


@tool(tags=["coding", "planning", "debug", "review"])
def read_file(
    path: str, summarize: bool = False, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
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


@tool(name="list_files", tags=["coding", "planning", "debug", "review"])
def list_dir(path: str = ".", workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name in _OS_JUNK:
            continue
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"path": str(p), "status": "ok", "items": items}


@tool(side_effects=["write"], tags=["coding"])
def delete_file(
    path: str, workdir: Path = DEFAULT_WORKDIR, user_approved: bool = False
) -> Dict[str, Any]:
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


@tool(side_effects=["write"], tags=["coding"])
def rename_file(
    path: str = "",
    new_path: str = "",
    workdir: Path = DEFAULT_WORKDIR,
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


def sandbox_info(workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    return {"workdir": str(workdir.resolve())}


@tool(tags=["coding"])
def read_file_chunk(
    path: str, offset: int = 0, limit: int = -1, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
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


@tool(side_effects=["write"], tags=["coding"])
def edit_file(
    path: str, patch: str, workdir: Path = DEFAULT_WORKDIR, user_approved: bool = False
) -> Dict[str, Any]:
    """Edit a file using a unified diff patch. Returns diff in result for TUI display."""
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("edit_file", path, user_approved)
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

    p = _safe_resolve(path, workdir)
    if not p.exists():
        return {"path": str(p), "status": "not_found"}

    import subprocess
    import tempfile
    import os
    import difflib

    # Read original content BEFORE modification for diff generation
    original_content = p.read_text(encoding="utf-8")

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
        proc = subprocess.run(
            ["patch", "-u", "-f", str(p), "-i", patch_file],
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

        # Read new content AFTER modification to compute diff
        new_content = p.read_text(encoding="utf-8")

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


@tool(side_effects=["execute"], tags=["coding"])
def bash(
    command: str,
    workdir: Path = DEFAULT_WORKDIR,
    description: str = "",
    timeout_secs: float = 60.0,
    run_in_background: bool = False,
) -> Dict[str, Any]:
    """Execute a shell command and return its output.

    Args:
        command: The shell command to run.
        workdir: Working directory for the command.
        description: Brief description of the command's intent (advisory; logged for auditability).
        timeout_secs: Maximum seconds to wait for the command (default 60). Ignored when run_in_background=True.
        run_in_background: If True, spawn the process without waiting and return a background_task_id (PID).
    """
    import logging as _logging

    if description:
        _logging.getLogger(__name__).info(f"bash: {description} | cmd={command!r}")
    import subprocess
    import shlex
    import re as _re

    # Gate 1: Shell-operator / metacharacter block (DANGEROUS_PATTERNS).
    # Blocks &&, ||, ;, |, >, >>, <, $(, ` and destructive keywords on the
    # normalised (whitespace-collapsed, lowercased) command string so spacing
    # tricks like "r m  -rf" or "ls  |  grep" cannot bypass the check.
    _cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in _cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    # Gate 2: AST-level bash security analysis — catches advanced injection vectors
    # ($(...), backtick substitution, pipe-to-shell, fork bombs, disk-wipe ops) that
    # DANGEROUS_PATTERNS may miss (e.g. creative whitespace, multi-arg tricks).
    try:
        from src.tools.bash_security import analyze_bash_command, BashRiskLevel

        _risk_level, _risk_reasons = analyze_bash_command(command)
        if _risk_level == BashRiskLevel.BLOCKED:
            return {
                "status": "error",
                "error": f"Command blocked by security analysis: {'; '.join(_risk_reasons)}",
            }
    except ImportError:
        pass  # bash_security unavailable; Gate 1 above is still active

    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"status": "error", "error": f"Invalid command: {e}"}

    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    first_cmd = cmd_parts[0].lower()
    cmd_lower = _re.sub(r"\s+", " ", command).lower()

    # Gate 2: Restricted-command check (tier-3 candidates are blocked unless in the
    # RESTRICTED_ALLOWED_SUBCOMMANDS list, e.g. "npm test").
    for pattern in RESTRICTED_COMMANDS:
        if pattern in cmd_lower:
            allowed = any(
                cmd_lower.startswith(ok) for ok in RESTRICTED_ALLOWED_SUBCOMMANDS
            )
            if not allowed:
                return {
                    "status": "error",
                    "error": f"Command '{cmd_parts[0]}' requires user approval or sandboxed execution. "
                    f"Restricted commands include: pip, npm install, curl, wget, apt, sudo. "
                    f"Use safe alternatives or request user approval.",
                    "requires_approval": True,
                }

    # Gate 3: Block inline code-execution flags (python3 -c, node -e, ruby -e, php -r).
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked. "
                    "Run a script file instead (e.g. python3 script.py).",
                }

    # Gate 4: Archive / inplace-edit flag check (shared helper — also used by bash_readonly).
    _flag_err = _check_shell_flags(cmd_parts, first_cmd)
    if _flag_err is not None:
        return _flag_err

    # Gate 4b: Git subcommand allowlist — only read-only git operations are
    # auto-allowed.  Write operations (commit, push, reset, rm, …) require
    # explicit user approval or must go through the RESTRICTED_COMMANDS path.
    if first_cmd == "git":
        sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
        if sub not in GIT_SAFE_SUBCOMMANDS:
            return {
                "status": "error",
                "error": (
                    f"git subcommand '{sub}' is not in the read-only allowlist. "
                    f"Allowed: {sorted(GIT_SAFE_SUBCOMMANDS)}. "
                    "Write operations (commit, push, add, reset, …) require user approval."
                ),
                "requires_approval": True,
            }

    # Gate 5: Tier allowlist.
    if first_cmd in SAFE_COMMANDS:
        pass  # Auto-allowed
    elif first_cmd == "git":
        pass  # Already validated by Gate 4b (subcommand allowlist)
    elif first_cmd in TEST_COMPILE_COMMANDS:
        if first_cmd == "npm" and not any(
            x in cmd_lower for x in ["test", "run ", "start", "build", "lint"]
        ):
            return {
                "status": "error",
                "error": "npm: Only 'npm test', 'npm run', 'npm start', 'npm build', 'npm lint' are allowed. "
                "'npm install' requires user approval.",
                "requires_approval": True,
            }
    else:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed. Allowed: {sorted(SAFE_COMMANDS | TEST_COMPILE_COMMANDS)}",
        }

    # TUI-03: tier-3 approval gate — pause and ask user before executing sensitive commands.
    # The gate is bypassed in autonomous mode so headless runs are never blocked.
    _gate_setup_ok = False
    try:
        from src.tools._approval import is_tier3
        from src.tools.tools_config import is_autonomous
        from src.core.orchestration.orchestrator import register_bash_gate, _bash_denied
        from src.core.orchestration.event_bus import get_event_bus

        _gate_setup_ok = True
        if is_tier3(command) and not is_autonomous():
            import uuid as _uuid_t3

            _tool_id = str(_uuid_t3.uuid4())[:8]
            _gate_ev = register_bash_gate(_tool_id)
            try:
                get_event_bus().publish(
                    "bash.approval_required",
                    {"tool_id": _tool_id, "command": command},
                )
            except Exception:
                pass
            _approved = _gate_ev.wait(timeout=120.0)
            if not _approved or _tool_id in _bash_denied:
                _bash_denied.discard(_tool_id)
                return {"status": "denied", "output": "Bash command denied by user."}
    except Exception as _gate_exc:
        # Gate setup failed — log a warning so this is visible in logs.
        # If the gate was not yet initialised (e.g. headless / test mode), proceed.
        # If it was initialised but failed partway through, that is unexpected: warn and block.
        if _gate_setup_ok:
            _logger.warning(
                "bash: tier-3 approval gate failed unexpectedly; blocking command for safety. "
                "Error: %s cmd=%r",
                _gate_exc,
                command,
            )
            return {
                "status": "error",
                "error": "Approval gate failure — command blocked for safety. Re-run to retry.",
            }
        else:
            _logger.debug(
                "bash: approval gate unavailable (headless/test mode) — proceeding. cmd=%r",
                command,
            )

    # Background execution: spawn without waiting, return PID as task ID.
    if run_in_background:
        try:
            proc = subprocess.Popen(
                cmd_parts,
                cwd=str(Path(workdir)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            return {
                "status": "ok",
                "command": command,
                "background_task_id": str(proc.pid),
                "no_output_expected": True,
                "interrupted": False,
            }
        except FileNotFoundError:
            return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
        except OSError as e:
            return {"status": "error", "error": f"OS error: {e}"}

    try:
        from src.tools.sandbox import run_sandboxed

        result = run_sandboxed(
            cmd_parts,
            cwd=Path(workdir),
            timeout=timeout_secs,
            capture_output=True,
            text=True,
        )
        stdout, stderr, _out_cut, _err_cut = _truncate_bash_output(
            result.stdout, result.stderr
        )
        _rci = f"exit_code:{result.returncode}" if result.returncode != 0 else None
        out: Dict[str, Any] = {
            "status": "ok",
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "interrupted": False,
            "no_output_expected": not stdout.strip() and not stderr.strip(),
        }
        if _rci is not None:
            out["return_code_interpretation"] = _rci
        if _out_cut:
            out["stdout_truncated"] = True
        if _err_cut:
            out["stderr_truncated"] = True
        return out
    except subprocess.TimeoutExpired as _te:
        _raw_stdout = _te.stdout or ""
        _raw_stderr = _te.stderr or ""
        if isinstance(_raw_stdout, bytes):
            _raw_stdout = _raw_stdout.decode(errors="replace")
        if isinstance(_raw_stderr, bytes):
            _raw_stderr = _raw_stderr.decode(errors="replace")
        _raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(_raw_stdout, _raw_stderr)
        return {
            "status": "ok",
            "command": command,
            "stdout": _raw_stdout,
            "stderr": _raw_stderr,
            "returncode": -1,
            "interrupted": True,
            "return_code_interpretation": "timeout",
            "no_output_expected": not _raw_stdout.strip() and not _raw_stderr.strip(),
        }
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


@tool(side_effects=["execute"], tags=["coding", "debug", "review", "planning"])
def bash_readonly(
    command: str,
    workdir: Path = DEFAULT_WORKDIR,
    timeout_secs: float = 60.0,
) -> Dict[str, Any]:
    """Execute a read-only shell command (ls, grep, git status, cat, etc.).

    Only SAFE_COMMANDS (tier 1) are allowed. No test runners, no compilers,
    no file-writing operations. Prefer this over bash() for inspection tasks.

    Args:
        command: The shell command to run.
        workdir: Working directory for the command.
        timeout_secs: Maximum seconds to wait (default 60).
    """
    import shlex
    import re as _re

    # Gate 1: Shell-operator / metacharacter block.
    _cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in _cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    # Gate 2: AST-level bash security analysis.
    try:
        from src.tools.bash_security import analyze_bash_command, BashRiskLevel

        _risk_level, _risk_reasons = analyze_bash_command(command)
        if _risk_level == BashRiskLevel.BLOCKED:
            return {
                "status": "error",
                "error": f"Command blocked by security analysis: {'; '.join(_risk_reasons)}",
            }
    except ImportError:
        pass

    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"status": "error", "error": f"Invalid command: {e}"}

    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    first_cmd = cmd_parts[0].lower()
    cmd_lower = _re.sub(r"\s+", " ", command).lower()

    # Gate 2: Restricted commands are never allowed in read-only mode.
    for pattern in RESTRICTED_COMMANDS:
        if pattern in cmd_lower:
            return {
                "status": "error",
                "error": f"Command '{cmd_parts[0]}' is not allowed in read-only mode.",
                "requires_approval": True,
            }

    # Gate 3: Only SAFE_COMMANDS (tier 1) — no test runners or compilers.
    # Git is handled separately via the subcommand allowlist (Gate 3b).
    if first_cmd != "git" and first_cmd not in SAFE_COMMANDS:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed in read-only mode. Allowed: {sorted(SAFE_COMMANDS)}",
        }

    # Gate 3b: Git subcommand allowlist (read-only mode is more restrictive).
    if first_cmd == "git":
        sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
        if sub not in GIT_SAFE_SUBCOMMANDS:
            return {
                "status": "error",
                "error": (
                    f"git subcommand '{sub}' is not allowed in read-only mode. "
                    f"Allowed: {sorted(GIT_SAFE_SUBCOMMANDS)}."
                ),
            }

    # Gate 3c: Block sed in-place edit flags (-i / --in-place) in read-only mode.
    if first_cmd == "sed":
        for token in cmd_parts[1:]:
            if token in SED_WRITE_FLAGS or token.startswith("-i"):
                return {
                    "status": "error",
                    "error": (
                        f"sed flag '{token}' performs in-place file modification "
                        "and is not allowed in read-only mode."
                    ),
                }

    # Gate 4: Block inline code-execution flags.
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked.",
                }

    # Gate 5: Archive / inplace-edit flag check (shared helper).
    _flag_err = _check_shell_flags(cmd_parts, first_cmd)
    if _flag_err is not None:
        return _flag_err

    # Execute inside sandbox (network disabled) — prevents exfiltration even for
    # read-only commands. Falls back to plain subprocess when bwrap unavailable.
    import subprocess

    try:
        from src.tools.sandbox import run_sandboxed

        result = run_sandboxed(
            cmd_parts,
            cwd=Path(workdir),
            timeout=timeout_secs,
            network=False,
            capture_output=True,
            text=True,
        )
        stdout, stderr, _out_cut, _err_cut = _truncate_bash_output(
            result.stdout, result.stderr
        )
        _rci = f"exit_code:{result.returncode}" if result.returncode != 0 else None
        out: Dict[str, Any] = {
            "status": "ok",
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "interrupted": False,
            "no_output_expected": not stdout.strip() and not stderr.strip(),
        }
        if _rci is not None:
            out["return_code_interpretation"] = _rci
        if _out_cut:
            out["stdout_truncated"] = True
        if _err_cut:
            out["stderr_truncated"] = True
        return out
    except subprocess.TimeoutExpired as _te:
        _raw_stdout = _te.stdout or ""
        _raw_stderr = _te.stderr or ""
        if isinstance(_raw_stdout, bytes):
            _raw_stdout = _raw_stdout.decode(errors="replace")
        if isinstance(_raw_stderr, bytes):
            _raw_stderr = _raw_stderr.decode(errors="replace")
        _raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(_raw_stdout, _raw_stderr)
        return {
            "status": "ok",
            "command": command,
            "stdout": _raw_stdout,
            "stderr": _raw_stderr,
            "returncode": -1,
            "interrupted": True,
            "return_code_interpretation": "timeout",
            "no_output_expected": not _raw_stdout.strip() and not _raw_stderr.strip(),
        }
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


@tool(tags=["coding"])
def check_background_task(
    task_id: str, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    """Poll the status of a background process started with bash(run_in_background=True).

    Args:
        task_id: The background_task_id (PID) returned by bash(run_in_background=True).
        workdir: Unused; kept for API consistency.

    Returns a dict with:
        running (bool): True if the process is still alive.
        pid (int): The process ID.
        exit_code (int | None): Exit code if the process has finished, else None.
    """
    import os
    import signal

    try:
        pid = int(task_id)
    except (ValueError, TypeError):
        return {
            "status": "error",
            "error": f"Invalid task_id: {task_id!r} — expected a PID string.",
        }

    try:
        # os.kill(pid, 0) succeeds if the process exists; raises OSError if not.
        os.kill(pid, 0)
        return {"status": "ok", "pid": pid, "running": True, "exit_code": None}
    except ProcessLookupError:
        return {"status": "ok", "pid": pid, "running": False, "exit_code": None}
    except PermissionError:
        # Process exists but we don't own it — it's running.
        return {"status": "ok", "pid": pid, "running": True, "exit_code": None}
    except OSError:
        return {"status": "ok", "pid": pid, "running": False, "exit_code": None}


@tool(side_effects=["write"], tags=["coding"])
def edit_by_line_range(
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    workdir: Path = DEFAULT_WORKDIR,
    user_approved: bool = False,
) -> Dict[str, Any]:
    """
    Replace lines [start_line, end_line] (1-indexed, inclusive) in a file with new_content.
    Returns a unified diff identical in shape to edit_file.

    F6: Required for precise multi-line replacements without full-file rewrites.
    Integrated with WorkspaceGuard and safe_resolve for security.
    """
    import difflib

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
    p.write_text(new_content_str, encoding="utf-8")

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
        "lines_added": len([ln for ln in diff_lines if ln.startswith("+")]),
        "lines_removed": len([ln for ln in diff_lines if ln.startswith("-")]),
    }
    # IMPL-5: Post-write auto-lint — informational, does not block the write
    try:
        from src.tools.lint_dispatch import quick_lint as _quick_lint

        lint_result = _quick_lint(str(p), workdir)
        if lint_result and lint_result.get("lint_errors"):
            result["lint_warnings"] = lint_result["lint_errors"]
            result["lint_status"] = "warnings"
    except Exception:
        pass
    return result


@tool(tags=["coding"])
def glob(pattern: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Find files matching a glob pattern. Supports ** for recursive matching."""
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
    workdir: Path = DEFAULT_WORKDIR,
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
    import difflib

    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("edit_file_atomic", path, user_approved)
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

    p = _safe_resolve(path, workdir)
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

    # TUI-05: Blocking diff preview gate.
    # When not in autonomous mode AND a TUI is subscribed to file.diff.preview,
    # publish the diff and block until the user accepts or rejects it.
    # If no subscriber is listening (headless / test / CLI run), publish for
    # telemetry only and proceed without blocking.
    try:
        from src.tools.tools_config import is_autonomous
        from src.tools.patch_tools import generate_unified_diff as _preview_diff

        if not is_autonomous():
            _preview_text = _preview_diff(
                original_content, new_content, from_file=str(p), to_file=str(p)
            )
            _path_key = str(p)

            # Only block if the TUI is connected and listening for preview events.
            from src.core.orchestration.event_bus import get_event_bus as _get_bus

            _has_preview_sub = _get_bus().has_subscribers("file.diff.preview")

            if _has_preview_sub:
                _gate_ev = register_preview_gate(_path_key)
                _publish_diff_preview(_path_key, _preview_text, is_new_file=False)
                # Block for up to 5 minutes waiting for user decision
                _gate_ev.wait(timeout=300.0)
                with _preview_gate_lock:
                    _was_rejected = _path_key in _preview_rejected
                    if _was_rejected:
                        _preview_rejected.discard(_path_key)
                if _was_rejected:
                    return {
                        "path": str(p),
                        "status": "rejected",
                        "message": "Edit rejected by user.",
                    }
            else:
                # No TUI — publish for telemetry/logging and proceed immediately
                _publish_diff_preview(_path_key, _preview_text, is_new_file=False)
    except Exception as _gate_exc:
        _logger.debug(
            "TUI-05 preview gate error (non-fatal, proceeding): %s", _gate_exc
        )

    p.write_text(new_content, encoding="utf-8")

    # MEM-1: Invalidate context cache so the next ContextBuilder re-reads from disk.
    try:
        from src.core.context.context_builder import ContextBuilder as _CB

        _CB.invalidate_path(str(p))
    except Exception:
        pass

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
    # IMPL-5: Post-write auto-lint — informational, does not block the write
    try:
        from src.tools.lint_dispatch import quick_lint as _quick_lint

        lint_result = _quick_lint(str(p), workdir)
        if lint_result and lint_result.get("lint_errors"):
            result["lint_warnings"] = lint_result["lint_errors"]
            result["lint_status"] = "warnings"
    except Exception:
        pass
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
    workdir: Path = DEFAULT_WORKDIR,
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
    p.write_text(working_content, encoding="utf-8")

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


@tool(tags=["coding", "debug"])
def tail_log_file(
    path: str, lines: int = 50, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    """Read the last N lines of a file. Useful for inspecting log files."""
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


@tool(side_effects=["write"], tags=["coding"])
def create_directory(path: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Create a directory and all necessary parents."""
    try:
        p = _safe_resolve(path, workdir)
    except (PermissionError, ValueError) as exc:
        return {"path": path, "status": "error", "error": str(exc)}
    try:
        p.mkdir(parents=True, exist_ok=True)
        return {"path": str(p), "status": "ok", "created": True}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding", "debug"])
def read_file_bytes(
    path: str, max_bytes: int = 1048576, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    """Read a file as base64-encoded bytes. Useful for binary files, images, or compiled artifacts.

    Args:
        path: File path to read
        max_bytes: Maximum bytes to read (default 1MB)
        workdir: Working directory for path resolution
    """
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
