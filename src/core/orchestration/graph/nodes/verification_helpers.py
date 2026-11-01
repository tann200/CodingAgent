"""Polyglot, targeted, SHA-256 deduplicated, and deferred-rollback helpers for verification_node."""

from __future__ import annotations

import logging
import py_compile
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Set, Tuple

import yaml

from src.core.orchestration.graph.nodes.in_loop_verification import compute_file_sha256
from src.tools import verification_tools
from src.tools.bash_security import BashRiskLevel, analyze_bash_command

logger = logging.getLogger(__name__)

_ALLOWED_VERIFY_BINARIES: frozenset[str] = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "ruff",
        "mypy",
        "flake8",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "tsc",
        "eslint",
        "vitest",
        "jest",
        "cargo",
        "go",
    }
)

_FORBIDDEN_ARG_CHARS = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")


def resolve_modified_path(raw_path: str, workdir: Path) -> Path:
    p = Path(raw_path)
    if not p.is_absolute():
        p = workdir / p
    try:
        return p.resolve()
    except Exception:
        return p


def filter_unverified_files(
    modified_files: Iterable[str],
    verified_hashes: Mapping[str, str],
    workdir: Path,
) -> Tuple[List[str], Dict[str, str], int]:
    """Return (unverified_resolved_paths, current_hashes, skipped_cached_count)."""
    unverified: List[str] = []
    current_hashes: Dict[str, str] = {}
    skipped = 0
    for raw in sorted(set(modified_files)):
        if not raw:
            continue
        resolved = resolve_modified_path(str(raw), workdir)
        key = str(resolved)
        digest = compute_file_sha256(resolved)
        if not digest:
            # File deleted or unreadable; do not cache as verified
            continue
        current_hashes[key] = digest
        if verified_hashes.get(key) == digest:
            skipped += 1
        else:
            unverified.append(key)
    return unverified, current_hashes, skipped


def check_targeted_python_syntax(
    unverified_paths: Iterable[str],
    skipped_cached: int = 0,
) -> Dict[str, Any]:
    """Compile only unverified modified .py files without walking the repository."""
    py_files = [p for p in unverified_paths if str(p).endswith(".py") and Path(p).is_file()]
    syntax_errors: List[Dict[str, str]] = []
    for pf in py_files:
        try:
            py_compile.compile(pf, doraise=True)
        except Exception as exc:
            syntax_errors.append({"file": pf, "error": str(exc)})
    return {
        "status": "fail" if syntax_errors else "ok",
        "checked_files": len(py_files),
        "skipped_cached": skipped_cached,
        "syntax_errors": syntax_errors,
    }


def run_targeted_ruff(unverified_paths: Iterable[str], workdir: Path) -> Dict[str, Any]:
    """Run ruff check scoped strictly to unverified modified .py files."""
    py_files = [p for p in unverified_paths if str(p).endswith(".py") and Path(p).is_file()]
    if not py_files:
        return {"status": "ok", "checked_files": 0, "errors": []}
    if not shutil.which("ruff"):
        return {"status": "skipped", "checked_files": 0, "errors": []}
    cmd = ["ruff", "check", *py_files]
    proc = verification_tools._run_subprocess(
        cmd,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "command": " ".join(cmd),
        "checked_files": len(py_files),
        "stdout": (proc.stdout or "")[-1500:],
        "error": (proc.stderr or proc.stdout or "")[-1000:] if proc.returncode != 0 else "",
    }


def discover_targeted_pytest_files(
    modified_files: Iterable[str],
    workdir: Path,
) -> List[str]:
    """Discover existing pytest files directly matching modified Python modules."""
    matched: List[str] = []
    seen: Set[str] = set()

    def _add(candidate: Path) -> None:
        try:
            resolved = str(candidate.resolve())
        except Exception:
            resolved = str(candidate)
        if resolved not in seen and candidate.is_file():
            seen.add(resolved)
            matched.append(resolved)

    tests_root = workdir / "tests"
    for raw in sorted(set(modified_files)):
        p = resolve_modified_path(str(raw), workdir)
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            _add(p)
            continue
        stem = p.stem
        if not stem or stem == "__init__":
            continue
        for candidate in (
            p.parent / f"test_{stem}.py",
            p.parent / f"{stem}_test.py",
            tests_root / f"test_{stem}.py",
            tests_root / "unit" / f"test_{stem}.py",
            tests_root / "integration" / f"test_{stem}.py",
        ):
            _add(candidate)
    return matched


def has_rust_project(workdir: Path) -> bool:
    return (workdir / "Cargo.toml").is_file()


def has_go_project(workdir: Path) -> bool:
    return (workdir / "go.mod").is_file()


def run_rust_verification(
    workdir: Path,
    run_full_suite: bool,
    is_cancelled: Callable[[], bool],
) -> Dict[str, Any]:
    """Run cargo check (and cargo test when run_full_suite) inside sandbox."""
    out: Dict[str, Any] = {}
    if not shutil.which("cargo"):
        return {"cargo": {"status": "skipped", "reason": "cargo not installed"}}
    check_cmd = ["cargo", "check", "--quiet"]
    proc = verification_tools._run_subprocess(
        check_cmd, cwd=str(workdir), capture_output=True, text=True, check=False, timeout=120
    )
    out["cargo_check"] = {
        "status": "ok" if proc.returncode == 0 else "fail",
        "command": "cargo check --quiet",
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-1200:],
        "error": (proc.stderr or "")[-1500:] if proc.returncode != 0 else "",
    }
    if run_full_suite and proc.returncode == 0 and not is_cancelled():
        test_cmd = ["cargo", "test", "--quiet"]
        tproc = verification_tools._run_subprocess(
            test_cmd, cwd=str(workdir), capture_output=True, text=True, check=False, timeout=120
        )
        out["cargo_test"] = {
            "status": "ok" if tproc.returncode == 0 else "fail",
            "command": "cargo test --quiet",
            "returncode": tproc.returncode,
            "stdout": (tproc.stdout or "")[-1200:],
            "error": (tproc.stderr or tproc.stdout or "")[-1500:] if tproc.returncode != 0 else "",
        }
    return out


def run_go_verification(
    workdir: Path,
    run_full_suite: bool,
    is_cancelled: Callable[[], bool],
) -> Dict[str, Any]:
    """Run go vet (and go test when run_full_suite) inside sandbox."""
    out: Dict[str, Any] = {}
    if not shutil.which("go"):
        return {"go": {"status": "skipped", "reason": "go not installed"}}
    vet_cmd = ["go", "vet", "./..."]
    proc = verification_tools._run_subprocess(
        vet_cmd, cwd=str(workdir), capture_output=True, text=True, check=False, timeout=90
    )
    out["go_vet"] = {
        "status": "ok" if proc.returncode == 0 else "fail",
        "command": "go vet ./...",
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-1200:],
        "error": (proc.stderr or "")[-1500:] if proc.returncode != 0 else "",
    }
    if run_full_suite and proc.returncode == 0 and not is_cancelled():
        test_cmd = ["go", "test", "./..."]
        tproc = verification_tools._run_subprocess(
            test_cmd, cwd=str(workdir), capture_output=True, text=True, check=False, timeout=120
        )
        out["go_test"] = {
            "status": "ok" if tproc.returncode == 0 else "fail",
            "command": "go test ./...",
            "returncode": tproc.returncode,
            "stdout": (tproc.stdout or "")[-1200:],
            "error": (tproc.stderr or tproc.stdout or "")[-1500:] if tproc.returncode != 0 else "",
        }
    return out


def run_custom_verify_yaml(
    workdir: Path,
    session_modified: Iterable[str],
    is_cancelled: Callable[[], bool],
) -> Dict[str, Any] | None:
    """Execute validated commands from .codingagent/verify.yaml if present and untampered."""
    cfg_path: Path | None = None
    for candidate in (
        workdir / ".codingagent" / "verify.yaml",
        workdir / ".codingAgent" / "verify.yaml",
    ):
        if candidate.is_file():
            cfg_path = candidate
            break
    if cfg_path is None:
        return None

    resolved_cfg = str(resolve_modified_path(str(cfg_path), workdir))
    resolved_mods = {str(resolve_modified_path(str(m), workdir)) for m in session_modified if m}
    if resolved_cfg in resolved_mods:
        logger.warning("verification_helpers: rejecting verify.yaml modified during active session")
        return {
            "custom_verify": {
                "status": "fail",
                "error": "Refusing to execute verify.yaml modified during the same agent session.",
            }
        }

    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"custom_verify": {"status": "fail", "error": f"Invalid verify.yaml: {exc}"}}

    raw_commands = loaded.get("commands") if isinstance(loaded, Mapping) else None
    if not isinstance(raw_commands, list) or not raw_commands:
        return None

    results: Dict[str, Any] = {}
    for idx, entry in enumerate(raw_commands[:5]):
        if is_cancelled():
            break
        key = f"custom_cmd_{idx + 1}"
        if isinstance(entry, list) and all(isinstance(tok, str) for tok in entry) and entry:
            argv = [str(tok) for tok in entry]
        elif isinstance(entry, str) and entry.strip():
            if any(ch in entry for ch in _FORBIDDEN_ARG_CHARS):
                results[key] = {
                    "status": "fail",
                    "error": f"verify.yaml command rejected (shell metacharacters forbidden): {entry!r}",
                }
                break
            try:
                argv = shlex.split(entry)
            except ValueError as exc:
                results[key] = {"status": "fail", "error": f"Invalid command syntax: {exc}"}
                break
        else:
            results[key] = {"status": "fail", "error": "Empty or non-string command in verify.yaml"}
            break

        if not argv:
            continue
        binary_name = Path(argv[0]).name
        if binary_name not in _ALLOWED_VERIFY_BINARIES:
            results[key] = {
                "status": "fail",
                "error": f"Binary {binary_name!r} is not in verification allowlist",
            }
            break
        if any(any(ch in tok for ch in _FORBIDDEN_ARG_CHARS) for tok in argv):
            results[key] = {
                "status": "fail",
                "error": f"Shell metacharacters forbidden in verify.yaml arguments: {argv!r}",
            }
            break
        joined = " ".join(shlex.quote(t) for t in argv)
        risk, reasons = analyze_bash_command(joined)
        if risk in (BashRiskLevel.BLOCKED, BashRiskLevel.DANGEROUS):
            results[key] = {
                "status": "fail",
                "error": f"verify.yaml command blocked by bash_security ({risk.value}): {', '.join(reasons)}",
            }
            break

        proc = verification_tools._run_subprocess(
            argv, cwd=str(workdir), capture_output=True, text=True, check=False, timeout=90
        )
        results[key] = {
            "status": "ok" if proc.returncode == 0 else "fail",
            "command": " ".join(argv),
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-1200:],
            "error": (proc.stderr or proc.stdout or "")[-1500:] if proc.returncode != 0 else "",
        }
        if proc.returncode != 0:
            break
    return results or None


def build_deferred_rollback_feedback(
    results: Mapping[str, Any],
    attempt: int,
    recovery_limit: int,
    modified_files: Iterable[str],
) -> str:
    """Format concise verification failure feedback when edits are preserved on disk."""
    lines: List[str] = [
        f'<verification_failure attempt="{attempt}/{recovery_limit}" disk_state="EDITS_PRESERVED">',
        "Your previous edits ARE STILL ON DISK (rollback deferred so you can apply a minimal surgical fix).",
    ]
    mods = sorted(set(str(m) for m in modified_files if m))
    if mods:
        lines.append(f"Modified files in open transaction: {', '.join(mods[:8])}")
    for check_name, payload in results.items():
        if not isinstance(payload, Mapping) or payload.get("status") != "fail":
            continue
        detail = (
            payload.get("error")
            or payload.get("tracebacks")
            or payload.get("errors")
            or payload.get("syntax_errors")
            or payload.get("summary")
            or payload.get("stdout")
            or "check failed"
        )
        snippet = str(detail).strip()
        if len(snippet) > 900:
            snippet = snippet[:900] + "... [truncated]"
        lines.append(f"[{check_name}] FAIL:\n{snippet}")
    lines.append(
        "Fix only the failing issue above without rewriting working code from scratch, then re-verify."
    )
    lines.append("</verification_failure>")
    return "\n".join(lines)
