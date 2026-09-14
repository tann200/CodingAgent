"""Shared helpers for ``codingagent`` CLI subcommands."""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any, Optional


def print_json(payload: Any) -> None:
    """Print *payload* as compact JSON (lossless for scripting)."""
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


def git_branch(workdir: str) -> str:
    """Return the current git branch for *workdir* ('' if not a repo)."""
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.stdout.strip() or ""
    except Exception:
        return ""


def resolve_workdir(workdir: Optional[str]) -> str:
    """Return an absolute version of *workdir* (or cwd)."""
    return str(Path(workdir).resolve() if workdir else Path.cwd().resolve())


def python_version() -> str:
    return f"{platform.python_implementation()} {platform.python_version()}"


def render_content(content: Any) -> str:
    """Render a message ``content`` field (str or list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def runtime_os() -> str:
    return platform.system() or "unknown"


def cli_version() -> str:
    """Best-effort project version."""
    try:
        from importlib.metadata import version as _version

        return _version("codingagent")
    except Exception:
        return "dev"
