from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, Optional

# Import WorkspaceGuard for protected file checking
try:
    from src.core.orchestration.workspace_guard import WorkspaceGuard
except ImportError:
    # When used as standalone module, provide a no-op guard
    class WorkspaceGuard:
        """No-op guard when src.core is not available."""

        def guard_operation(self, *args, **kwargs):
            return {"status": "ok"}


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
    except Exception:
        pass  # Never block the write if event bus is unavailable


# Default working directory.  External projects should call
# ``tools_config.configure(default_workdir=Path("/my/project"))`` at startup
# rather than relying on this module-level constant.
DEFAULT_WORKDIR = Path.cwd()


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
        lines_added = len([line for line in diff_lines if line.startswith("+")])
        lines_removed = len([line for line in diff_lines if line.startswith("-")])
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
    if lines_added > 500:
        return {
            "path": str(p),
            "status": "error",
            "error": (
                f"write_file refused: {lines_added} lines exceeds 500-line hard limit. "
                "Split into multiple smaller writes."
            ),
        }

    # F14: Publish diff preview BEFORE writing so the TUI can show the proposed change
    _publish_diff_preview(str(p), diff, is_new_file=not bool(original_content))

    # Write new content after the preview event so the user sees it first
    p.write_text(content, encoding="utf-8")

    result: Dict[str, Any] = {
        "path": str(p),
        "status": "ok",
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "is_new_file": not bool(original_content),
    }
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
    if lines_added > 200:
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
    content = p.read_text(encoding="utf-8")
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
    return {"path": str(p), "status": "ok", "content": content}


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
    src_path: str, dst_path: str, workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    """Rename (move) a file within the workspace.

    Both src_path and dst_path are resolved against workdir and validated with
    _safe_resolve to prevent path traversal attacks.
    """
    try:
        src = _safe_resolve(src_path, workdir)
        dst = _safe_resolve(dst_path, workdir)
    except PermissionError as pe:
        return {"status": "error", "error": str(pe)}

    if not src.exists():
        return {"src_path": str(src), "status": "not_found"}

    # Read-before-write guardrail: rename is destructive on the source file
    try:
        from src.tools.guardrails import check_read_before_write

        rbw = check_read_before_write(src_path)
        if rbw:
            return {"status": "error", **rbw}
    except Exception:
        pass

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return {"ok": True, "status": "ok", "renamed": str(dst)}
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
        if net_changed > 200:
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
def bash(command: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Execute a shell command and return its output."""
    import subprocess
    import shlex
    import re as _re

    cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"status": "error", "error": f"Invalid command: {e}"}

    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    # ============================================================
    # TIERED COMMAND ALLOWLIST — constants imported from _security.py
    # ============================================================
    # SAFE_COMMANDS, TEST_COMPILE_COMMANDS, RESTRICTED_COMMANDS,
    # CODE_EXEC_INTERPRETERS, CODE_EXEC_FLAGS, TAR_EXTRACT_FLAGS
    # are all imported at the top of this module.

    # Check for restricted commands first.
    # Normalise whitespace before matching so double-space bypass is blocked (NEW-7).
    # NOTE: _re already imported above for DANGEROUS_PATTERNS check.
    cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in RESTRICTED_COMMANDS:
        if pattern in cmd_lower:
            # Check if this is an approved restricted command (e.g., npm test is OK)
            if cmd_lower.startswith("npm test") or cmd_lower.startswith("npm run"):
                break  # Allow npm test/run commands
            if cmd_lower.startswith("cargo test") or cmd_lower.startswith(
                "cargo build"
            ):
                break  # Allow cargo test/build
            if cmd_lower.startswith("go test") or cmd_lower.startswith("go build"):
                break  # Allow go test/build

            # For truly restricted commands, return error with guidance
            return {
                "status": "error",
                "error": f"Command '{cmd_parts[0]}' requires user approval or sandboxed execution. "
                f"Restricted commands include: pip, npm install, curl, wget, apt, sudo. "
                f"Use safe alternatives or request user approval.",
                "requires_approval": True,
            }

    # Determine command tier
    first_cmd = cmd_parts[0].lower()

    # Block code-execution flags for interpreter commands (C3 fix)
    # python3 -c "...", node -e "...", ruby -e "...", php -r "..." all allow arbitrary code execution
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked. "
                    "Run a script file instead (e.g. python3 script.py).",
                }

    # F5: Block in-place edit / extract flags for commands in the safe-command list.
    # `sed` is allowed for text transformation but `-i`/`--in-place` writes files.
    # `tar` is allowed for listing archives but `-x`/`--extract` unpacks arbitrary content.
    # `unzip` without `-l` (list-only) extracts files — block it.
    if first_cmd == "sed":
        # F6 fix: detect -i in any form — bare "-i", bundled "-ni", or "--in-place[=...]"
        _sed_inplace = False
        for _part in cmd_parts[1:]:
            if (
                _part == "-i"
                or _part == "--in-place"
                or _part.startswith("--in-place=")
            ):
                _sed_inplace = True
                break
            # Bundled short options: -ni, -rni, etc.  Any short-option group containing 'i'.
            if (
                _part.startswith("-")
                and not _part.startswith("--")
                and "i" in _part[1:]
            ):
                _sed_inplace = True
                break
        if _sed_inplace:
            return {
                "status": "error",
                "error": "sed -i (in-place edit) is not allowed. Use edit_file or edit_file_atomic instead.",
            }
    elif first_cmd == "tar":
        for part in cmd_parts[1:]:
            # Handle combined short flags like -xvf or separate -x
            stripped = part.lstrip("-")
            if part in TAR_EXTRACT_FLAGS or (
                part.startswith("-") and not part.startswith("--") and "x" in stripped
            ):
                return {
                    "status": "error",
                    "error": "tar extract is not allowed. Use tar -t / --list to inspect archives.",
                }
    elif first_cmd == "unzip":
        if "-l" not in cmd_parts[1:]:
            return {
                "status": "error",
                "error": "unzip without -l (list) is not allowed. Use unzip -l to inspect archive contents.",
            }

    if first_cmd in SAFE_COMMANDS:
        pass  # Auto-allowed
    elif first_cmd in TEST_COMPILE_COMMANDS:
        # For npm/node, only allow test/run commands
        if first_cmd == "npm" and not any(
            x in cmd_lower for x in ["test", "run ", "start", "build", "lint"]
        ):
            return {
                "status": "error",
                "error": "npm: Only 'npm test', 'npm run', 'npm start', 'npm build', 'npm lint' are allowed. "
                "Use 'npm install' requires user approval.",
                "requires_approval": True,
            }
    else:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed. Allowed: {sorted(SAFE_COMMANDS | TEST_COMPILE_COMMANDS)}",
        }

    # F12 fix: removed duplicate DANGEROUS_PATTERNS check here — the canonical check above
    # (whitespace-normalized, lowercased) already covers all shell operators before we
    # reach this point. Keeping two inconsistent checks caused confusing bypass edge-cases.

    try:
        result = subprocess.run(
            cmd_parts,
            shell=False,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "status": "ok",
            "command": command,
            "stdout": result.stdout[:50000],
            "stderr": result.stderr[:5000],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Command timed out after 60 seconds"}
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


@tool(side_effects=["execute"], tags=["coding", "debug", "review", "planning"])
def bash_readonly(command: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Execute a read-only shell command (ls, grep, git status, cat, etc.).

    Only SAFE_COMMANDS (tier 1) are allowed. No test runners, no compilers,
    no file-writing operations. Prefer this over bash() for inspection tasks.
    """
    import subprocess
    import shlex
    import re as _re

    cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"status": "error", "error": f"Invalid command: {e}"}

    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    # Check for restricted commands
    for pattern in RESTRICTED_COMMANDS:
        if pattern in cmd_lower:
            return {
                "status": "error",
                "error": f"Command '{cmd_parts[0]}' is not allowed in read-only mode.",
                "requires_approval": True,
            }

    # Only SAFE_COMMANDS (tier 1) are allowed — no test runners or compilers
    first_cmd = cmd_parts[0].lower()
    if first_cmd not in SAFE_COMMANDS:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed in read-only mode. Allowed: {sorted(SAFE_COMMANDS)}",
        }

    # Block code-execution flags for interpreter commands
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked.",
                }

    # Block sed -i and tar -x (same as bash)
    if first_cmd == "sed":
        for _part in cmd_parts[1:]:
            if (
                _part == "-i"
                or _part == "--in-place"
                or _part.startswith("--in-place=")
            ):
                return {
                    "status": "error",
                    "error": "sed -i is not allowed in read-only mode.",
                }
            if (
                _part.startswith("-")
                and not _part.startswith("--")
                and "i" in _part[1:]
            ):
                return {
                    "status": "error",
                    "error": "sed -i is not allowed in read-only mode.",
                }
    elif first_cmd == "tar":
        for part in cmd_parts[1:]:
            stripped = part.lstrip("-")
            if part in TAR_EXTRACT_FLAGS or (
                part.startswith("-") and not part.startswith("--") and "x" in stripped
            ):
                return {
                    "status": "error",
                    "error": "tar extract is not allowed in read-only mode.",
                }
    elif first_cmd == "unzip":
        if "-l" not in cmd_parts[1:]:
            return {
                "status": "error",
                "error": "unzip without -l (list) is not allowed in read-only mode.",
            }

    try:
        result = subprocess.run(
            cmd_parts,
            shell=False,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "status": "ok",
            "command": command,
            "stdout": result.stdout[:50000],
            "stderr": result.stderr[:5000],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Command timed out after 60 seconds"}
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


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

    diff_lines = list(
        difflib.unified_diff(
            original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm="\n"
        )
    )
    diff = "".join(diff_lines)

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
        matches = sorted(matches)[:LIMIT]
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
    if count == 0:
        return {
            "path": str(p),
            "status": "error",
            "error": "old_string not found in file.",
        }
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

    new_content = original_content.replace(old_string, new_string, 1)
    p.write_text(new_content, encoding="utf-8")

    original_lines = original_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm="\n"
        )
    )
    diff = "".join(diff_lines)

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
        return {"status": "error", "error": f"max_bytes must be an integer, got {max_bytes!r}"}
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
