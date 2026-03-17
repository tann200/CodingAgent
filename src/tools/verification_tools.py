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
    """Extract list of failed test names."""
    failed = []
    # Pattern: "test_filename.py::test_function FAILED"
    for line in output.split("\n"):
        if "FAILED" in line and "::" in line:
            # Extract test name
            match = re.search(r"(test_\S+) FAILED", line)
            if match:
                failed.append(match.group(1))
            else:
                # Just take the part after ::
                parts = line.split("::")
                if len(parts) >= 2:
                    failed.append(parts[-1].split()[0])
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
            cmd, cwd=workdir, capture_output=True, text=True, check=False
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
