from typing import Dict, Any, List, Optional
import subprocess
import shutil
import os
import re


def run_tests(workdir: str, test_files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run pytest with STRUCTURED output.

    Returns normalized dict with:
    - status: ok | fail | error | skipped
    - passed: number of passed tests
    - failed: number of failed tests
    - failed_tests: list of failed test names
    - errors: list of error details
    - tracebacks: full error traces for debugging
    """
    try:
        if not shutil.which("pytest"):
            return {"status": "skipped", "reason": "pytest not installed"}

        # Build command - use specific test files if provided
        cmd = ["pytest", "-v", "--tb=short"]
        if test_files:
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


def run_linter(workdir: str, fix: bool = False) -> Dict[str, Any]:
    """Run ruff linter with STRUCTURED output.

    Returns:
    - status: ok | fail | error | skipped
    - error_count: number of errors
    - warning_count: number of warnings
    - errors: list of {file, line, message} dicts
    """
    try:
        if not shutil.which("ruff"):
            return {"status": "skipped", "reason": "ruff not installed"}

        cmd = ["ruff", "check", workdir]
        if fix:
            cmd.append("--fix")

        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=60
        )

        # Parse ruff output
        errors = _parse_ruff_output(proc.stdout)
        error_count = len([e for e in errors if e.get("severity") == "error"])
        warning_count = len([e for e in errors if e.get("severity") == "warning"])

        return {
            "status": "ok" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "error_count": error_count,
            "warning_count": warning_count,
            "errors": errors,
            "summary": proc.stdout[-1000:] if proc.stdout else "",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


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


def syntax_check(workdir: str) -> Dict[str, Any]:
    """Quick syntax check by running python -m py_compile.

    Returns:
    - status: ok | fail | error
    - checked_files: number of files checked
    - syntax_errors: list of {file, line, error} dicts
    """
    out = {"checked_files": 0, "syntax_errors": []}
    try:
        for root, dirs, files in os.walk(workdir):
            # Skip common non-code directories
            dirs[:] = [
                d
                for d in dirs
                if d not in [".git", "__pycache__", "node_modules", ".venv", "venv"]
            ]

            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    try:
                        import py_compile

                        py_compile.compile(path, doraise=True)
                        out["checked_files"] += 1
                    except Exception as e:
                        # Extract line number from error
                        line_match = re.search(r"line (\d+)", str(e))
                        line_num = int(line_match.group(1)) if line_match else None
                        out["syntax_errors"].append(
                            {
                                "file": path,
                                "line": line_num,
                                "error": str(e),
                                "type": "syntax_error",
                            }
                        )

        out["status"] = "ok" if not out["syntax_errors"] else "fail"
        out["error_count"] = len(out["syntax_errors"])
        return out
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_js_tests(workdir: str, test_files: Optional[List[str]] = None) -> Dict[str, Any]:
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
                cmd, cwd=workdir, capture_output=True, text=True, check=False, timeout=120
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

        return {"status": "skipped", "reason": "No JS test runner found (jest/vitest/mocha). Install via npm."}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "JS tests timed out after 120 seconds"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
            return {"status": "skipped", "reason": "tsc not found and npx not available"}

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
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "column": int(m.group(3)),
                "code": m.group(4),
                "message": m.group(5),
            })
    return errors


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
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "column": int(m.group(3)),
                "severity": m.group(4).lower(),
                "message": m.group(5),
            })
    return errors


# Alias for backwards compatibility
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
