from __future__ import annotations

from pathlib import Path
from typing import Dict, Any

# Import WorkspaceGuard for protected file checking
from src.core.orchestration.workspace_guard import WorkspaceGuard
from src.tools._path_utils import safe_resolve


# Default working directory used by tools (project root /output)
DEFAULT_WORKDIR = Path.cwd() / "output"
DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    """Backward-compatible wrapper around the shared safe_resolve utility (#29)."""
    return safe_resolve(path, workdir)


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

    import difflib

    p = _safe_resolve(path, workdir)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Read original content BEFORE modification for diff generation
    original_content = ""
    if p.exists():
        original_content = p.read_text(encoding="utf-8")

    # Write new content
    p.write_text(content, encoding="utf-8")

    # Generate unified diff for TUI display (show as new file if no original)
    original_lines = (
        original_content.splitlines(keepends=True) if original_content else []
    )
    new_lines = content.splitlines(keepends=True)

    if original_content:
        diff_lines = list(
            difflib.unified_diff(
                original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm=""
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

    return {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "is_new_file": not bool(original_content),
    }


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


_OS_JUNK = frozenset({
    ".DS_Store", "._.DS_Store", "Thumbs.db", "desktop.ini",
    ".Spotlight-V100", ".Trashes", ".fseventsd",
})


def list_dir(path: str = ".", workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    p = _safe_resolve(path, workdir)
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.name in _OS_JUNK:
            continue
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"path": str(p), "status": "ok", "items": items}


def delete_file(
    path: str, workdir: Path = DEFAULT_WORKDIR, user_approved: bool = False
) -> Dict[str, Any]:
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("delete_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

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


def edit_file(
    path: str, patch: str, workdir: Path = DEFAULT_WORKDIR, user_approved: bool = False
) -> Dict[str, Any]:
    """Edit a file using a unified diff patch. Returns diff in result for TUI display."""
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("edit_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

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
                original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm=""
            )
        )
        diff = "".join(diff_lines)

        return {
            "path": str(p),
            "status": "ok",
            "diff": diff,
            "lines_added": len([line for line in diff_lines if line.startswith("+")]),
            "lines_removed": len([line for line in diff_lines if line.startswith("-")]),
        }
    finally:
        try:
            os.remove(patch_file)
        except OSError:
            pass


def bash(command: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Execute a shell command and return its output."""
    import subprocess
    import shlex

    # Check for dangerous patterns BEFORE parsing
    DANGEROUS_PATTERNS = [
        "&&",
        "||",
        ";",
        "|",
        ">",
        ">>",
        "<",
        "$(",
        "`",
        "rm -rf",
        "rm -r",
        "rm -f",
        "del ",
        "format ",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
    ]
    cmd_lower = command.lower()
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
    # TIERED COMMAND ALLOWLIST - Phase 1.1 Security Fix
    # ============================================================

    # Tier 1: Safe read-only and utility commands (auto-allowed)
    SAFE_COMMANDS = {
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
        "tree",
        "sort",
        "uniq",
        "awk",
        "sed",
        "diff",
        "stat",
        "file",
        "du",
        "df",
        "id",
        "whoami",
        "hostname",
        # Process / system info
        "ps",
        "pgrep",
        "lsof",
        "uname",
        "uptime",
        "free",      # Linux: memory usage
        "top",       # read-only snapshot (non-interactive via -bn1)
        "htop",
        # Binary / object file inspection
        "nm",
        "objdump",
        "readelf",
        "ldd",
        "strings",
        # macOS-specific
        "sw_vers",       # macOS version
        "defaults",      # read macOS defaults (read-only use)
        "system_profiler",  # macOS hardware/software info
        "otool",         # macOS object file tool (like nm/objdump)
        "codesign",      # code signature inspection
        "xcode-select",  # Xcode CLI tools path query
        "plutil",        # plist utility (read)
        "pbpaste",       # macOS clipboard paste
        # Path / archive inspection
        "realpath",
        "basename",
        "dirname",
        "readlink",
        "tar",           # inspect archive contents (not extract by default)
        "zip",
        "unzip",
        "type",
        "md5sum",
        "sha256sum",
        "md5",           # macOS md5
        "shasum",        # macOS shasum
        "xxd",           # hex dump
        "less",
        "more",
        "column",
        "cut",
        "tr",
        "tee",
        "xargs",
        "test",
        "[",
        "true",
        "false",
        "touch",         # update timestamp / create empty file — non-destructive
    }

    # Tier 2: Test and compile commands (auto-allowed - needed for verification)
    TEST_COMPILE_COMMANDS = {
        # Python
        "python",
        "python3",
        "pytest",
        "py.test",
        "tox",
        "nox",
        "ruff",      # linter/formatter
        "mypy",      # type checker
        "pyright",
        "uv",        # modern Python package/project manager (test/run)
        "poetry",    # Python dependency manager (run/test)
        "pdm",       # Python dependency manager
        "hatch",     # Python project manager
        # Node/npm (test only, not install)
        "npm",       # Limited - npm test allowed, npm install restricted
        "npx",       # Run local packages (jest, tsc, eslint etc.)
        "node",
        "yarn",      # Limited - yarn test/run allowed
        "pnpm",      # Limited - pnpm test/run allowed
        # TypeScript
        "tsc",       # TypeScript compiler (--noEmit for type check)
        # JS/TS test runners
        "jest",
        "vitest",
        "mocha",
        "jasmine",
        # JS linters / formatters
        "eslint",
        "prettier",
        "biome",
        # Rust
        "cargo",
        "rustc",     # Rust compiler
        # Java
        "javac",
        "java",
        "jar",
        "mvn",
        "gradle",
        # Go
        "go",
        "gofmt",
        "golint",
        "staticcheck",
        # C/C++
        "gcc",
        "g++",
        "clang",
        "clang++",
        "make",
        "cmake",
        "ninja",
        # Ruby
        "bundle",
        "rake",
        "rspec",
        "ruby",
        # PHP
        "composer",
        "php",
        # Swift (macOS)
        "swift",
        "swiftc",
        # .NET
        "dotnet",
    }

    # Tier 3: Restricted commands (require user approval or sandbox)
    RESTRICTED_COMMANDS = {
        "pip",
        "pip3",
        "pip install",  # Package installers
        "curl",
        "wget",  # Network fetchers
        "npm install",
        "npm i",  # npm install
        "cargo install",  # Rust install
        "go install",
        "go get",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "brew",  # System package managers
        "sudo",
        "su",
        "chmod",
        "chown",  # Permission changes (already allowed above for safety)
        "rm",
        "del",  # File deletion (handled separately)
    }

    # Check for restricted commands first.
    # Normalise whitespace before matching so double-space bypass is blocked (NEW-7).
    import re as _re
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
    CODE_EXEC_INTERPRETERS = {"python", "python3", "node", "ruby", "php"}
    CODE_EXEC_FLAGS = {"-c", "-e", "-r", "--eval", "--execute"}
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked. "
                    "Run a script file instead (e.g. python3 script.py).",
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

    # Block shell operators
    DANGEROUS_PATTERNS = ["&&", "||", ";", "|", ">", ">>", "<", "$(", "`"]
    if any(p in command for p in DANGEROUS_PATTERNS):
        return {
            "status": "error",
            "error": "Command contains dangerous pattern. No shell operators allowed.",
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


def glob(pattern: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
    """Find files matching a glob pattern. Supports ** for recursive matching."""
    LIMIT = 500
    try:
        base = Path(workdir)
        if "**" in pattern:
            # Pattern already expresses recursion; use Path.glob() verbatim so ** is honoured
            raw = base.glob(pattern)
        else:
            # Simple pattern — search the whole tree recursively
            raw = base.rglob(pattern)
        matches = sorted(
            str(p.relative_to(base)) for p in raw if p.is_file()
        )[:LIMIT]
        return {"status": "ok", "pattern": pattern, "matches": matches}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
            original_lines, new_lines, fromfile=str(p), tofile=str(p), lineterm=""
        )
    )
    diff = "".join(diff_lines)

    return {
        "path": str(p),
        "status": "ok",
        "diff": diff,
        "lines_added": len([l for l in diff_lines if l.startswith("+")]),
        "lines_removed": len([l for l in diff_lines if l.startswith("-")]),
    }
