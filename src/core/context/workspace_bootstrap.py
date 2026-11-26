"""Turn-1 deterministic workspace bootstrap summary (TASK-34).

Performs stat-only / lightweight root manifest reads (zero subprocesses!) to
produce a compact <= 5-line ``<workspace_bootstrap>`` block injected once into
turn-1 context so small models skip 2-4 blind discovery turns.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple


def _read_small(path: Path, max_bytes: int = 16_384) -> str:
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as fh:
            return fh.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _detect_python(wd: Path) -> Optional[Tuple[str, str, str]]:
    pyproject = wd / "pyproject.toml"
    pytest_ini = wd / "pytest.ini"
    setup_cfg = wd / "setup.cfg"
    reqs = wd / "requirements.txt"
    if not any(p.is_file() for p in (pyproject, pytest_ini, setup_cfg, reqs)):
        return None

    text = _read_small(pyproject) + "\n" + _read_small(setup_cfg) + "\n" + _read_small(reqs)
    lower = text.lower()

    venv_py = wd / ".venv" / "bin" / "python"
    py_bin = ".venv/bin/python" if venv_py.is_file() else "python"

    if (wd / "uv.lock").is_file():
        pm = "uv"
    elif (wd / "poetry.lock").is_file():
        pm = "poetry"
    else:
        pm = "pip"

    frameworks: List[str] = []
    if pytest_ini.is_file() or "pytest" in lower:
        frameworks.append("pytest")
    if "ruff" in lower:
        frameworks.append("ruff")
    if "mypy" in lower:
        frameworks.append("mypy")

    test_cmd = f"{py_bin} -m pytest -q" if ("pytest" in frameworks or (wd / "tests").is_dir()) else f"{py_bin} -m unittest"
    fw_str = ", ".join(frameworks) if frameworks else "stdlib"
    return f"Python ({pm})", fw_str, test_cmd


def _detect_js(wd: Path) -> Optional[Tuple[str, str, str]]:
    pkg = wd / "package.json"
    if not pkg.is_file():
        return None
    raw = _read_small(pkg)
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    if (wd / "pnpm-lock.yaml").is_file():
        pm = "pnpm"
    elif (wd / "yarn.lock").is_file():
        pm = "yarn"
    elif (wd / "bun.lockb").is_file() or (wd / "bun.lock").is_file():
        pm = "bun"
    else:
        pm = "npm"

    deps: dict[str, object] = {}
    if isinstance(data, dict):
        deps.update(data.get("dependencies") or {})
        deps.update(data.get("devDependencies") or {})
    raw_scripts = data.get("scripts") if isinstance(data, dict) else None
    scripts: dict[str, object] = raw_scripts if isinstance(raw_scripts, dict) else {}

    lang = "TypeScript" if ("typescript" in deps or (wd / "tsconfig.json").is_file()) else "JavaScript"
    frameworks: List[str] = []
    for cand in ("vitest", "jest", "mocha", "eslint", "next", "react", "vite"):
        if cand in deps:
            frameworks.append(cand)
    test_cmd = f"{pm} test" if "test" in scripts else (f"npx {frameworks[0]}" if frameworks else f"{pm} test")
    return f"{lang} ({pm})", ", ".join(frameworks) if frameworks else "node", test_cmd


def _detect_rust(wd: Path) -> Optional[Tuple[str, str, str]]:
    if not (wd / "Cargo.toml").is_file():
        return None
    return "Rust (cargo)", "cargo", "cargo test"


def _detect_go(wd: Path) -> Optional[Tuple[str, str, str]]:
    if not (wd / "go.mod").is_file():
        return None
    return "Go (go modules)", "go test", "go test ./..."


def _detect_make_targets(wd: Path) -> List[str]:
    mf = wd / "Makefile"
    if not mf.is_file():
        return []
    text = _read_small(mf, max_bytes=8_192)
    targets = []
    for target in ("test", "check", "lint", "build"):
        if re.search(rf"^{target}\s*:", text, re.MULTILINE):
            targets.append(target)
    return targets


def build_workspace_bootstrap_summary(workdir: str | Path) -> str:
    """Build a deterministic <= 5-line workspace summary without spawning subprocesses."""
    try:
        wd = Path(workdir).resolve()
    except OSError:
        return ""
    if not wd.is_dir():
        return ""

    stacks: List[Tuple[str, str, str]] = []
    for detector in (_detect_python, _detect_js, _detect_rust, _detect_go):
        hit = detector(wd)
        if hit is not None:
            stacks.append(hit)

    make_targets = _detect_make_targets(wd)
    if not stacks and not make_targets:
        return ""

    lines: List[str] = ["<workspace_bootstrap>"]
    if stacks:
        langs = ", ".join(s[0] for s in stacks)
        fws = ", ".join(s[1] for s in stacks if s[1])
        test_cmds = " | ".join(s[2] for s in stacks)
        lines.append(f"Stack: {langs}")
        lines.append(f"Tools/Frameworks: {fws}")
        lines.append(f"Test command: {test_cmds}")
    if make_targets:
        lines.append(f"Makefile targets: {', '.join(make_targets)}")
    lines.append("</workspace_bootstrap>")
    # Guarantee <= 5 content lines (excluding wrapper tags -> total <= 7 lines, body <= 4 lines)
    return "\n".join(lines)
