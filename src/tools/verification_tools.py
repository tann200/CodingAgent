from typing import Dict, Any
import subprocess
import shutil
import os


def run_tests(workdir: str) -> Dict[str, Any]:
    """Run pytest in the given workdir. Returns a simple dict with status and output."""
    try:
        if not shutil.which('pytest'):
            return {"status": "skipped", "reason": "pytest not installed"}
        proc = subprocess.run(['pytest', '-q'], cwd=workdir, capture_output=True, text=True, check=False)
        return {"status": "ok" if proc.returncode == 0 else "fail", "returncode": proc.returncode, "stdout": proc.stdout[:2000], "stderr": proc.stderr[:2000]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_linter(workdir: str) -> Dict[str, Any]:
    """Run ruff (if available) to lint the workdir. Fallback: return skipped."""
    try:
        if not shutil.which('ruff'):
            return {"status": "skipped", "reason": "ruff not installed"}
        proc = subprocess.run(['ruff', str(workdir)], cwd=workdir, capture_output=True, text=True, check=False)
        return {"status": "ok" if proc.returncode == 0 else "fail", "returncode": proc.returncode, "stdout": proc.stdout[:2000], "stderr": proc.stderr[:2000]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def syntax_check(workdir: str) -> Dict[str, Any]:
    """Quick syntax check by running python -m py_compile across the repo."""
    out = {"checked_files": 0, "errors": []}
    try:
        for root, dirs, files in os.walk(workdir):
            for f in files:
                if f.endswith('.py'):
                    path = os.path.join(root, f)
                    try:
                        import py_compile
                        py_compile.compile(path, doraise=True)
                        out['checked_files'] += 1
                    except Exception as e:
                        out['errors'].append({"file": path, "error": str(e)})
        out['status'] = 'ok' if not out['errors'] else 'fail'
        return out
    except Exception as e:
        return {"status": "error", "error": str(e)}
