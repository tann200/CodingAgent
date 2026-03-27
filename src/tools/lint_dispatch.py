"""Post-write quick-lint dispatcher.

Runs the fastest available syntax check for a given file's extension
and returns any errors found. Used by write_file and edit_file_atomic
to provide automatic lint feedback to the agent.

Timeout: 10 seconds per check. Never raises — returns None on failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def quick_lint(path: str, workdir: Path) -> Optional[Dict[str, Any]]:
    """Run a fast syntax check for the given file's extension.

    Returns None if no linter is available, or a dict with
    ``lint_errors`` (list of ``{line, message, code}``) otherwise.
    Timeout: 10s. Never raises.
    """
    ext = Path(path).suffix.lower()
    timeout = 10

    try:
        if ext == ".py":
            return _lint_python(path, timeout)
        elif ext in (".js", ".mjs", ".cjs", ".jsx"):
            return _lint_js(path, timeout)
        elif ext in (".ts", ".tsx"):
            return _lint_ts(path, timeout)
        elif ext == ".go":
            return _lint_go(path, workdir, timeout)
        elif ext == ".rs":
            return _lint_rust(path, timeout)
    except Exception:
        pass
    return None


def _lint_python(path: str, timeout: int) -> Dict[str, Any]:
    """py_compile — zero dependencies, instant."""
    import py_compile
    import tempfile

    try:
        py_compile.compile(path, doraise=True)
        return {"lint_errors": []}
    except py_compile.PyCompileError as exc:
        msg = str(exc)
        return {"lint_errors": [{"line": None, "message": msg, "code": "E999"}]}


def _lint_js(path: str, timeout: int) -> Dict[str, Any]:
    """node --check <file>"""
    try:
        result = subprocess.run(
            ["node", "--check", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"lint_errors": []}  # node not available
    errors = _parse_node_check(result.stderr, path)
    return {"lint_errors": errors}


def _lint_ts(path: str, timeout: int) -> Dict[str, Any]:
    """tsc --noEmit on a single TypeScript file.

    Runs tsc in standalone mode (no tsconfig.json).  Must supply --module and
    --target so tsc does not complain about missing project configuration.
    Falls back to node --check (works for .ts in Node >=22) if tsc unavailable.
    """
    # Try standalone tsc first (most informative)
    try:
        result = subprocess.run(
            [
                "npx", "--no-install", "tsc",
                "--noEmit",
                "--allowJs",
                "--module", "commonjs",
                "--target", "es2020",
                "--lib", "es2020",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        errors = _parse_tsc_output(result.stdout + result.stderr, path)
        return {"lint_errors": errors}
    except FileNotFoundError:
        pass
    # Fallback: node --check (syntax only, available on Node 22+)
    return _lint_js(path, timeout)


def _lint_go(path: str, workdir: Path, timeout: int) -> Dict[str, Any]:
    """go build -o /dev/null in the file's directory (if go available).

    Running 'go build' on the package directory is more reliable than
    passing a relative path, which can fail when workdir and path don't
    share a common root (e.g. after symlink resolution).
    """
    file_dir = str(Path(path).parent)
    try:
        result = subprocess.run(
            ["go", "build", "-o", "/dev/null", "."],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=file_dir,
        )
    except FileNotFoundError:
        return {"lint_errors": []}
    except Exception:
        return {"lint_errors": []}
    errors = _parse_go_errors(result.stderr, path)
    return {"lint_errors": errors}


def _lint_rust(path: str, timeout: int) -> Dict[str, Any]:
    """rustc --edition=2021 --emit=metadata <file>"""
    try:
        result = subprocess.run(
            ["rustc", "--edition=2021", "--emit=metadata", path, "-o", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"lint_errors": []}
    errors = _parse_rustc_errors(result.stderr, path)
    return {"lint_errors": errors}


# ---- Output parsers ----


def _parse_node_check(stderr: str, path: str) -> List[Dict[str, Any]]:
    """Parse 'node --check' stderr for syntax errors.

    node --check output format on error:
        /path/to/file.js:10
        SyntaxError: Unexpected token ...
            at ...
    """
    errors = []
    lines = stderr.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for file:lineno pattern that matches our file
        if path in line and ":" in line:
            try:
                # Extract line number: last colon-separated numeric token after the path
                after_path = line[line.index(path) + len(path):]
                ln_str = after_path.lstrip(":").split(":")[0].split()[0]
                ln = int(ln_str)
                # Grab next line as the error message if it contains SyntaxError
                msg = lines[i + 1].strip() if i + 1 < len(lines) else line.strip()
                errors.append({"line": ln, "message": msg, "code": "SyntaxError"})
                i += 2
                continue
            except (ValueError, IndexError):
                pass
        if "SyntaxError" in line and not errors:
            errors.append({"line": None, "message": line.strip(), "code": "SyntaxError"})
        i += 1
    return errors


def _parse_tsc_output(output: str, path: str) -> List[Dict[str, Any]]:
    """Parse tsc output for errors."""
    errors = []
    for line in output.strip().splitlines():
        if path in line and "error TS" in line:
            try:
                # Format: file.ts(10,5): error TS1005: message
                idx = line.index("(")
                rest = line[idx + 1 :]
                ln_str = rest.split(",")[0]
                ln = int(ln_str)
                msg_part = rest.split(")")[1].strip() if ")" in rest else line
                errors.append({"line": ln, "message": msg_part, "code": "TSC"})
            except (ValueError, IndexError):
                errors.append({"line": None, "message": line.strip(), "code": "TSC"})
    return errors


def _parse_go_errors(stderr: str, path: str) -> List[Dict[str, Any]]:
    """Parse go build output for errors."""
    errors = []
    for line in stderr.strip().splitlines():
        if path in line and ":" in line:
            try:
                parts = line.split(":", 3)
                ln = int(parts[1])
                msg = parts[3].strip() if len(parts) > 3 else line.strip()
                errors.append({"line": ln, "message": msg, "code": "GoBuild"})
            except (ValueError, IndexError):
                errors.append(
                    {"line": None, "message": line.strip(), "code": "GoBuild"}
                )
    return errors


def _parse_rustc_errors(stderr: str, path: str) -> List[Dict[str, Any]]:
    """Parse rustc output for errors."""
    errors = []
    for line in stderr.strip().splitlines():
        if "-->" in line and path in line:
            try:
                # Format: --> file.rs:10:5
                after = line.split("-->")[1].strip()
                parts = after.split(":")
                ln = int(parts[1])
                errors.append({"line": ln, "message": line.strip(), "code": "RustC"})
            except (ValueError, IndexError):
                errors.append({"line": None, "message": line.strip(), "code": "RustC"})
    return errors
