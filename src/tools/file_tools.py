from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Any

# Import WorkspaceGuard for protected file checking
from src.core.orchestration.workspace_guard import WorkspaceGuard


# Default working directory used by tools (project root /output)
DEFAULT_WORKDIR = Path.cwd() / "output"
DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)


def _safe_resolve(path: str, workdir: Path = DEFAULT_WORKDIR) -> Path:
    """
    Safely resolve a path, preventing symlink traversal attacks.

    Phase 1.3 Security Fix:
    - Uses Path.resolve(strict=True) to follow symlinks strictly
    - Explicitly checks resolved path is within workdir
    - Prevents path traversal via symlinks pointing outside workdir
    """
    p = Path(path)
    if not p.is_absolute():
        p = workdir / p

    # Use strict=True to raise exception if symlink doesn't exist
    # This also resolves the path fully, following all symlinks
    try:
        p = p.resolve(strict=True)
    except FileNotFoundError:
        # For new files that don't exist yet, use non-strict resolve
        p = p.resolve()

    # Resolve workdir to absolute path
    workdir_resolved = workdir.resolve()

    # Explicit check: resolved path must be within workdir
    # Using os.path.realpath for extra safety
    real_path = os.path.realpath(p)
    real_workdir = os.path.realpath(workdir_resolved)

    if not real_path.startswith(real_workdir + os.sep) and real_path != real_workdir:
        raise PermissionError(
            f"Path '{path}' resolves to '{real_path}' which is outside "
            f"working directory '{real_workdir}'. Symlink traversal blocked."
        )

    return p


def write_file(
    path: str,
    content: str,
    workdir: Path = DEFAULT_WORKDIR,
    user_approved: bool = False,
) -> Dict[str, Any]:
    # Phase 4.3: WorkspaceGuard integration - check protected files
    guard = WorkspaceGuard()
    guard_result = guard.guard_operation("write_file", path, user_approved)
    if guard_result.get("status") == "error":
        return {"path": path, "status": "error", "error": guard_result.get("error")}

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
        # Node/npm (test only, not install)
        "npm",  # Limited - npm test allowed, npm install restricted
        "node",
        # Rust
        "cargo",
        # Java
        "javac",
        "java",
        "jar",
        "mvn",
        "gradle",
        # Go
        "go",
        # C/C++
        "gcc",
        "g++",
        "clang",
        "clang++",
        # Ruby
        "bundle",
        "rake",
        "rspec",
        "ruby",
        # PHP
        "composer",
        "php",
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

    # Check for restricted commands first
    cmd_lower = command.lower()
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
    """Find files matching a glob pattern."""

    try:
        matches = []
        base = Path(workdir)
        for path in base.rglob(pattern.replace("**/", "*").replace("**", "*")):
            if path.is_file():
                matches.append(str(path.relative_to(base)))
        return {"status": "ok", "pattern": pattern, "matches": matches}
    except Exception as e:
        return {"status": "error", "error": str(e)}
