"""JS/TS verification tools and auto-formatters extracted from verification_tools.py (OPT-06).

All public and helper symbols are re-exported from ``src/tools/verification_tools.py``
and look up ``_run_subprocess`` / ``shutil`` / formatter caches via ``verification_tools``
so existing monkeypatches remain 100% transparent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._tool import PermissionKind, tool

_logger = logging.getLogger(__name__)


def _vt() -> Any:
    from src.tools import verification_tools

    return verification_tools


@tool(side_effects=["execute"], tags=["coding"], permission_kind=PermissionKind.EXECUTE_BASH)
def run_js_tests(
    workdir: str, test_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Run JS/TypeScript tests using jest, vitest, or mocha (whichever is available)."""
    vt = _vt()
    try:
        pkg_path = os.path.join(workdir, "package.json")
        preferred: Optional[str] = None
        if os.path.exists(pkg_path):
            try:
                pkg = json.loads(Path(pkg_path).read_text(encoding="utf-8"))
                scripts = pkg.get("scripts", {})
                test_script = scripts.get("test", "")
                for runner in ("vitest", "jest", "mocha", "jasmine"):
                    if runner in test_script:
                        preferred = runner
                        break
            except Exception:
                pass

        runner_order = [preferred] if preferred else []
        runner_order += [
            "npx --no-install jest",
            "npx --no-install vitest run",
            "npx --no-install mocha",
        ]

        for runner_cmd in runner_order:
            if runner_cmd is None:
                continue
            parts = runner_cmd.split()
            if not vt.shutil.which(parts[0]):
                continue
            if parts[0] == "npx" and not os.path.exists(os.path.join(workdir, "node_modules")):
                continue
            cmd = parts[:]
            if test_files:
                cmd.extend(test_files)
            proc = vt._run_subprocess(
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


def _parse_tsc_output(output: str) -> List[Dict[str, Any]]:
    """Parse tsc output: path(line,col): error TSxxxx: message."""
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


@tool(side_effects=["execute"], tags=["coding"], permission_kind=PermissionKind.EXECUTE_BASH)
def run_ts_check(workdir: str) -> Dict[str, Any]:
    """Run TypeScript type-checking via tsc --noEmit."""
    vt = _vt()
    try:
        has_local_tsc = os.path.exists(os.path.join(workdir, "node_modules", ".bin", "tsc"))
        has_global_tsc = bool(vt.shutil.which("tsc"))
        if has_local_tsc and vt.shutil.which("npx"):
            cmd = ["npx", "--no-install", "tsc", "--noEmit"]
        elif has_global_tsc:
            cmd = ["tsc", "--noEmit"]
        else:
            return {
                "status": "skipped",
                "reason": "tsc not found and npx not available",
            }

        proc = vt._run_subprocess(
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


def _parse_eslint_compact(output: str) -> List[Dict[str, Any]]:
    """Parse ESLint --format=compact: path: line N, col M, severity - message."""
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


@tool(side_effects=["execute"], tags=["coding"], permission_kind=PermissionKind.EXECUTE_BASH)
def run_eslint(workdir: str, paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run ESLint on JS/TypeScript files."""
    vt = _vt()
    try:
        runner = None
        if vt.shutil.which("eslint"):
            runner = ["eslint"]
        elif vt.shutil.which("npx"):
            runner = ["npx", "--no-install", "eslint"]
        else:
            return {"status": "skipped", "reason": "eslint not found"}

        cmd = runner + ["--format=compact"] + (paths or ["."])
        proc = vt._run_subprocess(
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


def format_file(path: str) -> Dict[str, Any]:
    """Auto-format a file using the appropriate formatter for its language."""
    vt = _vt()
    p = Path(path)
    if not p.exists():
        return {"status": "skipped", "reason": "file does not exist"}

    suffix = p.suffix.lower()
    formatter = None
    cmd = None

    if suffix == ".py":
        if vt.shutil.which("ruff"):
            formatter = "ruff"
            cmd = ["ruff", "format", "--quiet", str(p)]
        elif vt.shutil.which("black"):
            formatter = "black"
            cmd = ["black", "--quiet", str(p)]
    elif suffix in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
        if vt.shutil.which("prettier"):
            formatter = "prettier"
            cmd = ["prettier", "--write", "--log-level", "error", str(p)]
    elif suffix == ".go":
        if vt.shutil.which("gofmt"):
            formatter = "gofmt"
            cmd = ["gofmt", "-w", str(p)]
    elif suffix == ".rs":
        if vt.shutil.which("rustfmt"):
            formatter = "rustfmt"
            cmd = ["rustfmt", "--edition", "2021", str(p)]

    if cmd is None:
        return {
            "status": "skipped",
            "reason": f"no formatter available for {suffix or 'unknown'}",
        }

    try:
        result = vt._run_subprocess(
            cmd,
            cwd=p.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "formatter": formatter,
                "path": path,
                "error": (
                    result.stderr or result.stdout or "formatter exited non-zero"
                ).strip()[:500],
            }
        return {"status": "ok", "formatter": formatter, "path": path}
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "formatter": formatter,
            "path": path,
            "error": "formatter timed out",
        }
    except Exception as exc:
        return {
            "status": "error",
            "formatter": formatter,
            "path": path,
            "error": str(exc),
        }


def _load_config() -> Dict[str, Any]:
    """Load formatters.yaml; return built-in fallback on error."""
    vt = _vt()
    if vt._yaml is None:
        return {}
    try:
        if vt._FORMATTER_CONFIG_PATH.exists():
            return vt._yaml.safe_load(vt._FORMATTER_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {
        ".py": {"cmd": ["black", "--quiet", "{file}"]},
        ".go": {"cmd": ["gofmt", "-w", "{file}"]},
        ".rs": {"cmd": ["rustfmt", "{file}"]},
    }


def _get_config() -> Dict[str, Any]:
    vt = _vt()
    if vt._FORMATTER_CONFIG is None:
        vt._FORMATTER_CONFIG = _load_config()
    return vt._FORMATTER_CONFIG


def _get_cmd(ext: str, file_path: str) -> Optional[List[str]]:
    """Return the configured formatter command for *ext*, or None if unavailable."""
    vt = _vt()
    if ext in vt._CMD_CACHE:
        cached = vt._CMD_CACHE[ext]
        if cached is None:
            return None
        return [part.replace("{file}", file_path) for part in cached]

    cfg = _get_config()
    entry = cfg.get(ext)
    if not entry:
        vt._CMD_CACHE[ext] = None
        return None

    template: List[str] = entry.get("cmd", [])
    if not template:
        vt._CMD_CACHE[ext] = None
        return None

    binary = template[0]
    if not vt.shutil.which(binary):
        _logger.debug("formatter: %s not found on PATH — skipping for %s", binary, ext)
        vt._CMD_CACHE[ext] = None
        return None

    vt._CMD_CACHE[ext] = template
    return [part.replace("{file}", file_path) for part in template]


def run_formatter(file_path: str, timeout: float = 15.0) -> bool:
    """Run the configured auto-formatter for *file_path* (merged from formatter.py)."""
    ext = Path(file_path).suffix.lower()
    cmd = _get_cmd(ext, str(Path(file_path).resolve()))
    if cmd is None:
        return False

    try:
        from src.tools.sandbox import run_sandboxed

        result = run_sandboxed(
            cmd,
            cwd=Path(file_path).resolve().parent,
            capture_output=True,
            text=True,
            timeout=timeout,
            network=False,
        )
        if result.returncode != 0:
            _logger.debug(
                "formatter: %s exited %d for %s: %s",
                cmd[0],
                result.returncode,
                file_path,
                result.stderr.strip()[:200],
            )
            return False
        _logger.debug("formatter: %s ran successfully on %s", cmd[0], file_path)
        return True
    except subprocess.TimeoutExpired:
        _logger.debug("formatter: %s timed out for %s", cmd[0], file_path)
        return False
    except Exception as exc:
        _logger.debug("formatter: unexpected error running %s: %s", cmd[0], exc)
        return False
