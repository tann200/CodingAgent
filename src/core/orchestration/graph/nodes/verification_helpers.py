"""Polyglot, targeted, SHA-256 deduplicated, and deferred-rollback helpers for verification_node."""

from __future__ import annotations

import logging
import os
import py_compile
import re
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

    # Fallback for small workspaces or tests whose filename doesn't mirror the module stem
    # (e.g. test_all.py importing modified module `math_ops`): bounded shallow scan only.
    if not matched:
        mod_stems = {
            resolve_modified_path(str(raw), workdir).stem
            for raw in modified_files
            if str(raw).endswith(".py")
        } - {"", "__init__"}
        if mod_stems:
            shallow_tests: List[Path] = []
            for base_dir in (workdir, tests_root, tests_root / "unit"):
                if not base_dir.is_dir():
                    continue
                for entry in sorted(base_dir.iterdir()):
                    if entry.is_file() and entry.suffix == ".py" and (
                        entry.name.startswith("test_") or entry.name.endswith("_test.py")
                    ):
                        shallow_tests.append(entry)
                    if len(shallow_tests) >= 12:
                        break
            for tf in shallow_tests:
                try:
                    txt = tf.read_text(encoding="utf-8", errors="ignore")[:8000]
                except OSError:
                    continue
                if any(stem in txt for stem in mod_stems):
                    _add(tf)
            if not matched and 1 <= len(shallow_tests) <= 3:
                for tf in shallow_tests:
                    _add(tf)
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
    *,
    trusted: bool = True,
) -> Dict[str, Any] | None:
    """Execute validated commands from .codingagent/verify.yaml if present and untampered.

    REM-04: Untrusted workspaces must never execute repo-supplied verification
    commands automatically. Tampering with ``verify.yaml`` during the active
    session is still rejected fail-closed regardless of trust status.
    """
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
    resolved_cfg_norm = os.path.normcase(resolved_cfg)
    mod_paths = [resolve_modified_path(str(m), workdir) for m in session_modified if m]
    tampered = False
    for m_path in mod_paths:
        if str(m_path) == resolved_cfg or os.path.normcase(str(m_path)) == resolved_cfg_norm:
            tampered = True
            break
        try:
            if m_path.exists() and os.path.samefile(cfg_path, m_path):
                tampered = True
                break
        except OSError:
            pass
    if tampered:
        logger.warning("verification_helpers: rejecting verify.yaml modified during active session")
        return {
            "custom_verify": {
                "status": "fail",
                "error": "Refusing to execute verify.yaml modified during the same agent session.",
            }
        }

    if not trusted:
        logger.warning(
            "verification_helpers: skipping %s because workspace is untrusted",
            cfg_path,
        )
        return None

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


# ---------------------------------------------------------------------------
# OPT-05: Normalized validation command keys (modeled on haze workState.ts)
# ---------------------------------------------------------------------------

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_TRAILING_REDIR_RE = re.compile(
    r"(?:\s+(?:[12]?>>?|&>)\s*(?:/dev/null|&[12]))+\s*$"
)
_PYTHON_BIN_RE = re.compile(r"^python3(?:\.\d+)*$")


def split_validation_stages(command: str) -> List[str]:
    """Split a shell validation command into top-level ``&&`` / ``;`` stages.

    Respects single quotes, double quotes, and backslash escapes so quoted
    arguments containing ``&&`` or ``;`` are never split prematurely.
    """
    text = (command or "").strip()
    if not text:
        return []

    stages: List[str] = []
    buf: List[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if ch == ";":
                stage = "".join(buf).strip()
                if stage:
                    stages.append(stage)
                buf = []
                i += 1
                continue
            if ch == "&" and i + 1 < n and text[i + 1] == "&":
                stage = "".join(buf).strip()
                if stage:
                    stages.append(stage)
                buf = []
                i += 2
                continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stages.append(tail)
    return stages


def normalize_validation_command_key(command: str) -> str:
    """Normalize a single validation command stage into a canonical comparison key.

    Strips shell wrappers (``env``, ``time``, ``nice``, ``command``, ``timeout``),
    leading ``KEY=VALUE`` env assignments, trailing ``2>&1`` / ``2>/dev/null``
    redirections, and virtualenv/absolute binary paths (e.g. ``.venv/bin/python``
    -> ``python``) so semantically identical verification runs share one key.
    """
    raw = (command or "").strip()
    if not raw:
        return ""

    # Strip trailing stderr/stdout redirections to /dev/null or &1/&2
    cleaned = _TRAILING_REDIR_RE.sub("", raw).strip()
    if not cleaned:
        return ""

    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        tokens = cleaned.split()

    # Strip wrappers and env assignments from the front
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if _ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        if tok in ("env", "command", "time"):
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
            continue
        if tok == "nice":
            idx += 1
            if idx < len(tokens) and tokens[idx] == "-n":
                idx += 2
            elif idx < len(tokens) and tokens[idx].startswith("-n"):
                idx += 1
            continue
        if tok == "timeout":
            idx += 1
            while idx < len(tokens) and tokens[idx].startswith("-"):
                idx += 1
            if idx < len(tokens):
                idx += 1  # skip duration argument
            continue
        break

    tokens = tokens[idx:]
    if not tokens:
        return ""

    # Drop any standalone redirection tokens that survived shlex splitting
    while tokens and tokens[-1] in ("2>&1", "1>&2", "2>/dev/null", ">/dev/null", "1>/dev/null"):
        tokens.pop()
    if not tokens:
        return ""

    # Normalize binary path (e.g. .venv/bin/python3 -> python, /usr/bin/pytest -> pytest)
    head = Path(tokens[0]).name
    if _PYTHON_BIN_RE.match(head):
        head = "python"
    tokens[0] = head

    return " ".join(tokens)


def extract_validation_command_keys(command: str) -> List[str]:
    """Return normalized command keys for every stage in a (possibly compound) command."""
    keys: List[str] = []
    for stage in split_validation_stages(command):
        key = normalize_validation_command_key(stage)
        if key:
            keys.append(key)
    return keys


def command_satisfies_validation_key(
    executed_command: str,
    target_command: str,
) -> bool:
    """Return True if *executed_command* covers all normalized stages in *target_command*."""
    target_keys = extract_validation_command_keys(target_command)
    if not target_keys:
        return False
    executed_keys = set(extract_validation_command_keys(executed_command))
    return all(k in executed_keys for k in target_keys)
