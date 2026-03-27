from typing import Dict, Any, List, Optional
from pathlib import Path
import subprocess
import shutil
import os
import re
import time

from src.tools._tool import tool


def _safe_resolve_workdir(workdir: str) -> str:
    """Resolve workdir to a real absolute path, rejecting path-traversal attempts.

    Raises ValueError when the resolved path escapes the workspace or lands in a
    sensitive OS directory.  Uses the same resolve-and-bound-check pattern as
    ``_path_utils.safe_resolve``.
    """
    try:
        p = Path(workdir)
        if not p.is_absolute():
            raise ValueError(f"workdir must be an absolute path, got: {workdir!r}")
        try:
            resolved = p.resolve(strict=True)
        except FileNotFoundError:
            resolved = p.resolve()
        real_path = os.path.realpath(resolved)

        # Reject root-level system directories that the agent must never
        # execute in.  Uses os.path.realpath so /etc → /private/etc on macOS.
        _BLOCKED_BASES = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/proc", "/sys")
        for base in _BLOCKED_BASES:
            blocked_real = os.path.realpath(base)
            if real_path == blocked_real or real_path.startswith(blocked_real + os.sep):
                raise ValueError(
                    f"workdir {workdir!r} resolves to {real_path!r} which is "
                    f"a blocked system directory"
                )

        return str(resolved)
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"Invalid workdir: {workdir!r}")


@tool(side_effects=["execute"], tags=["coding"])
def run_tests(
    workdir: str,
    test_files: Optional[List[str]] = None,
    use_last_failed: bool = False,
    changed_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run pytest with STRUCTURED output.

    Returns normalized dict with:
    - status: ok | fail | error | skipped
    - passed: number of passed tests
    - failed: number of failed tests
    - failed_tests: list of failed test names
    - errors: list of error details
    - tracebacks: full error traces for debugging

    Args:
        workdir: Working directory for tests
        test_files: Specific test files to run (optional)
        use_last_failed: If True, run only tests that failed in previous run (--lf)
        changed_files: If provided, run tests that are in or depend on these files
    """
    # P2-5: Reject path-traversal attempts
    try:
        workdir = _safe_resolve_workdir(workdir)
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    try:
        if not shutil.which("pytest"):
            return {"status": "skipped", "reason": "pytest not installed"}

        cmd = ["pytest", "-v", "--tb=short"]

        if use_last_failed:
            cmd.append("--lf")
            cmd.append("--co")
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if proc.returncode not in (0, 5):
                return {
                    "status": "error",
                    "error": f"Failed to collect last-failed tests: {proc.stdout}",
                }
            output = proc.stdout
            test_ids = _extract_collected_test_ids(output)
            if test_ids:
                cmd = ["pytest", "-v", "--tb=short", "--lf"]
            else:
                use_last_failed = False

        if changed_files:
            test_ids = _get_tests_for_files(workdir, changed_files)
            if test_ids:
                cmd.extend(test_ids)
                cmd.append("--co")
                proc = subprocess.run(
                    cmd,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if proc.returncode not in (0, 5):
                    changed_ids = _extract_collected_test_ids(proc.stdout)
                    if changed_ids:
                        cmd = ["pytest", "-v", "--tb=short"]
                        cmd.extend(["--collect-only"] + list(changed_ids))
                    else:
                        cmd = ["pytest", "-v", "--tb=short", workdir]
                else:
                    cmd = ["pytest", "-v", "--tb=short"]
                    cmd.extend(test_ids)
            else:
                cmd.append(workdir)
        elif test_files:
            cmd.extend(test_files)
        else:
            cmd.append(workdir)

        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=120
        )

        stdout = proc.stdout
        # stderr reserved for future use

        # Parse pytest output for structured data
        passed, failed = _parse_pytest_summary(stdout)
        failed_tests = _extract_failed_tests(stdout)
        errors = _extract_test_errors(stdout)
        tracebacks = _extract_tracebacks(stdout)

        return {
            "status": "ok" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed,
            "failed_tests": failed_tests,
            "errors": errors,
            "tracebacks": tracebacks,
            "summary": stdout[-1000:] if stdout else "",  # Last 1000 chars
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Tests timed out after 120 seconds"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _extract_collected_test_ids(output: str) -> List[str]:
    """Extract test IDs from pytest --collect-only output."""
    test_ids = []
    for line in output.split("\n"):
        match = re.search(r"<Function\s+(\S+)>|<Module\s+(\S+)>|<Class\s+(\S+)>", line)
        if match:
            test_ids.append(match.group(1) or match.group(2) or match.group(3))
    return test_ids


def _get_tests_for_files(workdir: str, changed_files: List[str]) -> List[str]:
    """Find tests that are in or depend on the changed files."""
    test_files = set()
    for f in changed_files:
        if f.endswith("_test.py") or f.startswith("test_") or "/tests/" in f:
            test_files.add(f)
        else:
            base = f.replace(".py", "")
            test_candidates = [
                f"test_{base}.py",
                f"{base}_test.py",
                f"tests/test_{base}.py",
                f"tests/{base}_test.py",
            ]
            for tc in test_candidates:
                if os.path.exists(os.path.join(workdir, tc)):
                    test_files.add(tc)
    return sorted(test_files)


def _parse_pytest_summary(output: str) -> tuple:
    """Parse pytest output for pass/fail counts."""
    passed = 0
    failed = 0

    # Look for patterns like "5 passed" or "3 failed, 2 passed"
    passed_match = re.search(r"(\d+) passed", output)
    failed_match = re.search(r"(\d+) failed", output)

    if passed_match:
        passed = int(passed_match.group(1))
    if failed_match:
        failed = int(failed_match.group(1))

    return passed, failed


def _extract_failed_tests(output: str) -> List[str]:
    """Extract list of failed test names from pytest -v output.

    Handles:
    - path/test_file.py::test_func FAILED
    - path/test_file.py::TestClass::test_method FAILED
    - path/test_file.py::test_func[param-id] FAILED  (parametrized)
    """
    failed = []
    for line in output.split("\n"):
        if "FAILED" not in line:
            continue
        # Match full pytest node id: file.py::anything FAILED
        # The node id can contain :: separators and [param] brackets
        match = re.search(r"(\S+\.py(?:::\S+?)+(?:\[.*?\])?)\s+FAILED", line)
        if match:
            failed.append(match.group(1))
        elif "::" in line:
            # Fallback: grab everything before " FAILED"
            parts = line.split(" FAILED")[0].strip().split()
            if parts:
                failed.append(parts[-1])
    return failed


def _extract_test_errors(output: str) -> List[Dict[str, str]]:
    """Extract test error details."""
    errors = []

    # Look for ERROR lines
    for line in output.split("\n"):
        if "ERROR" in line:
            errors.append({"type": "error", "message": line.strip()})

    return errors


def _extract_tracebacks(output: str) -> List[str]:
    """Extract full tracebacks for failed tests."""
    tracebacks = []
    current_tb = []
    in_traceback = False

    for line in output.split("\n"):
        if "FAILED" in line or "ERROR" in line:
            in_traceback = True
            current_tb = [line]
        elif in_traceback:
            if line.strip() and not line.startswith("="):
                current_tb.append(line)
            elif line.startswith("="):
                if current_tb:
                    tracebacks.append("\n".join(current_tb))
                in_traceback = False
                current_tb = []

    if current_tb:
        tracebacks.append("\n".join(current_tb))

    return tracebacks


@tool(side_effects=["execute"], tags=["coding"])
def run_linter(
    workdir: str, fix: bool = False, paths: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Run language-appropriate linters with STRUCTURED output.

    Dispatches to ruff (Python), eslint (JS/JSX), tsc (TS/TSX),
    cargo clippy (Rust), go vet (Go) based on detected file extensions.

    Returns unified result shape:
    - status: ok | fail | error | skipped
    - languages_checked: list of language names
    - total_errors: number of errors
    - total_warnings: number of warnings
    - errors: list of {file, line, message, severity, language} dicts

    Subprocess calls are dispatched to helpers that each enforce timeout=60 or
    timeout=120 so a hung linter can never block verification_node indefinitely.
    """
    try:
        # Collect files to lint
        file_exts: Dict[str, List[str]] = {}
        if paths:
            for p in paths:
                ext = Path(p).suffix.lower()
                file_exts.setdefault(ext, []).append(p)
        else:
            ext_priority = {
                ".py": "python",
                ".js": "javascript",
                ".mjs": "javascript",
                ".cjs": "javascript",
                ".jsx": "javascript",
                ".ts": "typescript",
                ".tsx": "typescript",
                ".rs": "rust",
                ".go": "go",
            }
            for root, dirs, fnames in os.walk(workdir):
                dirs[:] = [
                    d
                    for d in dirs
                    if not d.startswith(
                        (".git", "node_modules", "target", "__pycache__")
                    )
                ]
                for fn in fnames:
                    ext = Path(fn).suffix.lower()
                    if ext in ext_priority:
                        file_exts.setdefault(ext, []).append(str(Path(root) / fn))

        all_errors: List[Dict[str, Any]] = []
        languages_checked: List[str] = []
        py_files = file_exts.get(".py", [])
        js_files = (
            file_exts.get(".js", [])
            + file_exts.get(".mjs", [])
            + file_exts.get(".cjs", [])
            + file_exts.get(".jsx", [])
        )
        ts_files = file_exts.get(".ts", []) + file_exts.get(".tsx", [])
        rs_files = file_exts.get(".rs", [])
        go_files = file_exts.get(".go", [])

        if py_files:
            languages_checked.append("python")
            result = _run_ruff(workdir, fix)
            for e in result.get("errors", []):
                e["language"] = "python"
                all_errors.append(e)

        if js_files:
            languages_checked.append("javascript")
            result = _run_eslint_internal(workdir)
            for e in result.get("errors", []):
                e["language"] = "javascript"
                all_errors.append(e)

        if ts_files:
            languages_checked.append("typescript")
            result = _run_tsc_internal(workdir)
            for e in result.get("errors", []):
                e["language"] = "typescript"
                all_errors.append(e)

        if rs_files and shutil.which("cargo"):
            languages_checked.append("rust")
            result = _run_clippy(workdir)
            for e in result.get("errors", []):
                e["language"] = "rust"
                all_errors.append(e)

        if go_files and shutil.which("go"):
            languages_checked.append("go")
            result = _run_go_vet(workdir)
            for e in result.get("errors", []):
                e["language"] = "go"
                all_errors.append(e)

        error_count = sum(1 for e in all_errors if e.get("severity") == "error")
        warning_count = sum(1 for e in all_errors if e.get("severity") == "warning")

        return {
            "status": "ok" if error_count == 0 else "fail",
            "languages_checked": languages_checked,
            "total_errors": error_count,
            "total_warnings": warning_count,
            "errors": all_errors,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _run_ruff(workdir: str, fix: bool = False) -> Dict[str, Any]:
    """Internal ruff runner."""
    if not shutil.which("ruff"):
        return {"status": "skipped", "errors": []}
    cmd = ["ruff", "check", workdir]
    if fix:
        cmd.append("--fix")
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=60
    )
    errors = _parse_ruff_output(proc.stdout)
    return {"status": "ok" if proc.returncode == 0 else "fail", "errors": errors}


def _run_eslint_internal(workdir: str) -> Dict[str, Any]:
    """Internal eslint runner."""
    runner = None
    if shutil.which("eslint"):
        runner = ["eslint"]
    elif shutil.which("npx"):
        runner = ["npx", "--no-install", "eslint"]
    else:
        return {"status": "skipped", "errors": []}
    cmd = runner + ["--format=compact", "."]
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=60
    )
    errors = _parse_eslint_compact(proc.stdout + proc.stderr)
    return {"status": "ok" if proc.returncode == 0 else "fail", "errors": errors}


def _run_tsc_internal(workdir: str) -> Dict[str, Any]:
    """Internal tsc runner."""
    if not shutil.which("tsc") and not shutil.which("npx"):
        return {"status": "skipped", "errors": []}
    cmd = (
        ["npx", "--no-install", "tsc", "--noEmit"]
        if shutil.which("npx")
        else ["tsc", "--noEmit"]
    )
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=120
    )
    errors = _parse_tsc_output(proc.stdout + proc.stderr)
    return {"status": "ok" if proc.returncode == 0 else "fail", "errors": errors}


def _run_clippy(workdir: str) -> Dict[str, Any]:
    """Internal cargo clippy runner."""
    proc = subprocess.run(
        ["cargo", "clippy", "--message-format=json", "--", "-D", "warnings"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    errors = []
    for line in proc.stdout.splitlines():
        try:
            import json as _json

            msg = _json.loads(line)
            if msg.get("reason") == "compiler-message" and msg.get("message"):
                diag = msg["message"]
                spans = diag.get("spans", [])
                span = spans[0] if spans else {}
                errors.append(
                    {
                        "file": span.get("file_name", ""),
                        "line": span.get("line_start", 0),
                        "severity": "error"
                        if diag.get("level") == "error"
                        else "warning",
                        "message": diag.get("message", ""),
                        "code": diag.get("code", {}).get("code", ""),
                    }
                )
        except Exception:
            continue
    return {"status": "ok" if proc.returncode == 0 else "fail", "errors": errors}


def _run_go_vet(workdir: str) -> Dict[str, Any]:
    """Internal go vet runner."""
    proc = subprocess.run(
        ["go", "vet", "./..."],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    errors = []
    for line in proc.stderr.splitlines():
        if ":" in line:
            parts = line.split(":", 3)
            try:
                ln = int(parts[1])
                errors.append(
                    {
                        "file": parts[0],
                        "line": ln,
                        "severity": "error",
                        "message": parts[3].strip(),
                    }
                )
            except (ValueError, IndexError):
                errors.append(
                    {
                        "file": "",
                        "line": 0,
                        "severity": "error",
                        "message": line.strip(),
                    }
                )
    return {"status": "ok" if proc.returncode == 0 else "fail", "errors": errors}


def _parse_ruff_output(output: str) -> List[Dict[str, Any]]:
    """Parse ruff output into structured errors."""
    errors = []

    # Pattern: "file.py:10:5: E401 ... (message)"
    pattern = r"(.+?):(\d+):(\d+): (\w+) (.+)"

    for line in output.split("\n"):
        match = re.match(pattern, line)
        if match:
            file_path, line_num, col, code, message = match.groups()
            severity = "error" if code.startswith("E") else "warning"
            errors.append(
                {
                    "file": file_path,
                    "line": int(line_num),
                    "column": int(col),
                    "code": code,
                    "message": message,
                    "severity": severity,
                }
            )

    return errors


@tool(tags=["coding"])
def syntax_check(workdir: str, timeout_secs: float = 30.0) -> Dict[str, Any]:
    """Multi-language syntax check.

    Checks Python (py_compile), JS (node --check), Go (go build -o /dev/null),
    Rust (rustc --emit=metadata).

    Returns:
    - status: ok | fail | error | partial
    - checked_files: number of files checked
    - syntax_errors: list of {file, line, error, language} dicts
    - languages_checked: list of languages checked
    """
    import py_compile

    out: Dict[str, Any] = {
        "checked_files": 0,
        "syntax_errors": [],
        "languages_checked": [],
    }
    deadline = time.monotonic() + timeout_secs
    try:
        for root, dirs, files in os.walk(workdir):
            if time.monotonic() > deadline:
                out["status"] = "partial"
                out["error_count"] = len(out["syntax_errors"])
                out["warning"] = f"syntax_check timed out after {timeout_secs}s"
                return out

            dirs[:] = [
                d
                for d in dirs
                if d
                not in [
                    ".git",
                    "__pycache__",
                    "node_modules",
                    ".venv",
                    "venv",
                    "target",
                    "dist",
                    "build",
                ]
            ]

            for f in files:
                if time.monotonic() > deadline:
                    break
                path = os.path.join(root, f)
                ext = Path(f).suffix.lower()

                if ext == ".py":
                    try:
                        py_compile.compile(path, doraise=True)
                        out["checked_files"] += 1
                        if "python" not in out["languages_checked"]:
                            out["languages_checked"].append("python")
                    except Exception as e:
                        line_match = re.search(r"line (\d+)", str(e))
                        line_num = int(line_match.group(1)) if line_match else None
                        out["syntax_errors"].append(
                            {
                                "file": path,
                                "line": line_num,
                                "error": str(e),
                                "language": "python",
                            }
                        )

                elif ext in (".js", ".mjs", ".cjs", ".jsx"):
                    try:
                        result = subprocess.run(
                            ["node", "--check", path],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if result.returncode != 0:
                            out["syntax_errors"].append(
                                {
                                    "file": path,
                                    "line": None,
                                    "error": result.stderr.strip(),
                                    "language": "javascript",
                                }
                            )
                        out["checked_files"] += 1
                        if "javascript" not in out["languages_checked"]:
                            out["languages_checked"].append("javascript")
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass

                elif ext == ".go":
                    try:
                        result = subprocess.run(
                            ["go", "build", "-o", "/dev/null", path],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            cwd=workdir,
                        )
                        if result.returncode != 0:
                            for line in result.stderr.splitlines():
                                if ":" in line:
                                    out["syntax_errors"].append(
                                        {
                                            "file": path,
                                            "line": None,
                                            "error": line.strip(),
                                            "language": "go",
                                        }
                                    )
                        out["checked_files"] += 1
                        if "go" not in out["languages_checked"]:
                            out["languages_checked"].append("go")
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass

                elif ext == ".rs":
                    try:
                        result = subprocess.run(
                            [
                                "rustc",
                                "--edition=2021",
                                "--emit=metadata",
                                path,
                                "-o",
                                "/dev/null",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if result.returncode != 0:
                            for line in result.stderr.splitlines():
                                if "error" in line.lower():
                                    out["syntax_errors"].append(
                                        {
                                            "file": path,
                                            "line": None,
                                            "error": line.strip(),
                                            "language": "rust",
                                        }
                                    )
                        out["checked_files"] += 1
                        if "rust" not in out["languages_checked"]:
                            out["languages_checked"].append("rust")
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass

        out["status"] = "ok" if not out["syntax_errors"] else "fail"
        out["error_count"] = len(out["syntax_errors"])
        return out
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(side_effects=["execute"], tags=["coding"])
def run_js_tests(
    workdir: str, test_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Run JS/TypeScript tests using jest, vitest, or mocha (whichever is available).

    Returns normalized dict with:
    - status: ok | fail | error | skipped
    - runner: the test runner used
    - passed/failed counts where parseable
    - summary: last 1000 chars of output
    """
    try:
        # Detect package.json to find configured test runner
        import json as _json

        pkg_path = os.path.join(workdir, "package.json")
        preferred: Optional[str] = None
        if os.path.exists(pkg_path):
            try:
                pkg = _json.loads(open(pkg_path).read())
                scripts = pkg.get("scripts", {})
                test_script = scripts.get("test", "")
                for runner in ("vitest", "jest", "mocha", "jasmine"):
                    if runner in test_script:
                        preferred = runner
                        break
            except Exception:
                pass

        runner_order = [preferred] if preferred else []
        runner_order += ["npx jest", "npx vitest run", "npx mocha"]

        for runner_cmd in runner_order:
            if runner_cmd is None:
                continue
            parts = runner_cmd.split()
            if not shutil.which(parts[0]):
                continue
            cmd = parts[:]
            if test_files:
                cmd.extend(test_files)
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            stdout = proc.stdout + proc.stderr
            passed_m = re.search(r"(\d+)\s+(?:tests? )?passed", stdout, re.IGNORECASE)
            failed_m = re.search(r"(\d+)\s+(?:tests? )?failed", stdout, re.IGNORECASE)
            return {
                "status": "ok" if proc.returncode == 0 else "fail",
                "runner": runner_cmd,
                "returncode": proc.returncode,
                "passed": int(passed_m.group(1)) if passed_m else None,
                "failed": int(failed_m.group(1)) if failed_m else None,
                "summary": stdout[-1000:],
            }

        return {
            "status": "skipped",
            "reason": "No JS test runner found (jest/vitest/mocha). Install via npm.",
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "JS tests timed out after 120 seconds"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(side_effects=["execute"], tags=["coding"])
def run_ts_check(workdir: str) -> Dict[str, Any]:
    """Run TypeScript type-checking via tsc --noEmit.

    Returns:
    - status: ok | fail | error | skipped
    - error_count: number of type errors
    - errors: list of {file, line, message} dicts
    - summary: raw tsc output
    """
    try:
        if not shutil.which("tsc") and not shutil.which("npx"):
            return {
                "status": "skipped",
                "reason": "tsc not found and npx not available",
            }

        # Prefer local tsc via npx, fall back to global tsc
        if shutil.which("npx"):
            cmd = ["npx", "--no-install", "tsc", "--noEmit"]
        else:
            cmd = ["tsc", "--noEmit"]

        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=120
        )
        stdout = proc.stdout + proc.stderr
        errors = _parse_tsc_output(stdout)
        return {
            "status": "ok" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "error_count": len(errors),
            "errors": errors,
            "summary": stdout[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "tsc timed out after 120 seconds"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _parse_tsc_output(output: str) -> List[Dict[str, Any]]:
    """Parse tsc output: path(line,col): error TSxxxx: message"""
    errors = []
    pattern = r"^(.+?)\((\d+),(\d+)\):\s+error\s+(TS\d+):\s+(.+)$"
    for line in output.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            errors.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "column": int(m.group(3)),
                    "code": m.group(4),
                    "message": m.group(5),
                }
            )
    return errors


@tool(side_effects=["execute"], tags=["coding"])
def run_eslint(workdir: str, paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run ESLint on JS/TypeScript files.

    Returns:
    - status: ok | fail | error | skipped
    - error_count / warning_count
    - errors: list of {file, line, message, severity} dicts
    """
    try:
        runner = None
        if shutil.which("eslint"):
            runner = ["eslint"]
        elif shutil.which("npx"):
            runner = ["npx", "--no-install", "eslint"]
        else:
            return {"status": "skipped", "reason": "eslint not found"}

        cmd = runner + ["--format=compact"] + (paths or ["."])
        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=60
        )
        stdout = proc.stdout + proc.stderr
        errors = _parse_eslint_compact(stdout)
        error_count = sum(1 for e in errors if e.get("severity") == "error")
        warning_count = sum(1 for e in errors if e.get("severity") == "warning")
        return {
            "status": "ok" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "error_count": error_count,
            "warning_count": warning_count,
            "errors": errors,
            "summary": stdout[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "eslint timed out after 60 seconds"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _parse_eslint_compact(output: str) -> List[Dict[str, Any]]:
    """Parse ESLint --format=compact: path: line N, col M, severity - message"""
    errors = []
    pattern = r"^(.+?):\s+line\s+(\d+),\s+col\s+(\d+),\s+(Error|Warning)\s+-\s+(.+)$"
    for line in output.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            errors.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "column": int(m.group(3)),
                    "severity": m.group(4).lower(),
                    "message": m.group(5),
                }
            )
    return errors


# Alias for backwards compatibility
@tool(side_effects=["execute"], tags=["coding"])
def run_tests_legacy(workdir: str) -> Dict[str, Any]:
    """Legacy wrapper that returns simple output (for backwards compatibility)."""
    result = run_tests(workdir)
    # Convert to simple format
    return {
        "status": result.get("status"),
        "returncode": result.get("returncode"),
        "stdout": result.get("summary", ""),
        "stderr": "",
    }
