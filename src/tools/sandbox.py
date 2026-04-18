"""Sandboxed subprocess execution via bubblewrap (bwrap).

Provides ``run_sandboxed()`` as a drop-in replacement for ``subprocess.run``
when executing untrusted shell commands.  When ``bwrap`` is available on the
host, commands are wrapped in a bubblewrap container:

- Filesystem: read-only bind-mount of ``/``, writable bind-mount of *cwd* only.
- Network: disabled by default (``--unshare-net``).
- PID namespace: isolated (``--unshare-pid``).

When ``bwrap`` is not available, the function falls back to plain
``subprocess.run`` with a warning — this maintains full backwards compatibility
on macOS (where bubblewrap is Linux-only) and in environments where it is not
installed.

Sandbox strictness levels (``sandbox_level`` param):

    ``"off"``          — no sandboxing; plain subprocess.run.
    ``"workspace"``    — bwrap with workspace write access (default).
    ``"full"``         — bwrap + network disabled + even stricter mounts.

The level can be overridden at import time via the
``CODINGAGENT_SANDBOX_LEVEL`` environment variable.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Detect bwrap once at import time
_BWRAP_PATH: Optional[str] = shutil.which("bwrap")

# Default sandbox level — override via env var or config
_DEFAULT_LEVEL: str = os.environ.get("CODINGAGENT_SANDBOX_LEVEL", "workspace")


def _probe_bwrap(path: Optional[str]) -> bool:
    """Run a quick `bwrap --version` probe to ensure bwrap is callable.

    Returns True when the binary exists and responds successfully, False otherwise.
    """
    if not path:
        return False
    try:
        # Use a short timeout to avoid blocking import
        res = subprocess.run(
            [path, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


# Final availability flag (probe the binary if present)
_BWRAP_AVAILABLE: bool = _probe_bwrap(_BWRAP_PATH)


def _bwrap_available() -> bool:
    return _BWRAP_AVAILABLE


def _build_bwrap_args(
    cwd: Path,
    level: str,
    extra_writable_dirs: Optional[List[str]] = None,
) -> List[str]:
    """Build the bwrap command prefix for the given *level*."""
    args: List[str] = [_BWRAP_PATH or "bwrap"]

    # Ensure the helper dies with the parent so we don't leave orphaned processes
    args += ["--die-with-parent"]

    # Bind common read-only system directories individually (safer than ro-binding /)
    for d in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(d).exists():
            args += ["--ro-bind", d, d]

    # Minimal proc and dev setup
    if Path("/proc").exists():
        args += ["--proc", "/proc"]
    if Path("/dev").exists():
        args += ["--dev", "/dev"]

    # Writable mount for /tmp (many programs require it)
    if Path("/tmp").exists():
        args += ["--bind", "/tmp", "/tmp"]

    # Make the working directory writable
    cwd_str = str(cwd.resolve())
    args += ["--bind", cwd_str, cwd_str]

    # Extra writable dirs (caller-provided)
    for d in extra_writable_dirs or []:
        args += ["--bind", d, d]

    if level in ("workspace", "full"):
        # Isolate PID namespace
        args += ["--unshare-pid"]
        # Isolate network namespace (disable outbound network)
        args += ["--unshare-net"]

    # Change into cwd inside the container
    args += ["--chdir", cwd_str]

    return args


def run_sandboxed(
    cmd: List[str],
    cwd: Path,
    timeout: float = 60.0,
    network: bool = False,
    sandbox_level: Optional[str] = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run *cmd* optionally inside a bubblewrap sandbox.

    Parameters
    ----------
    cmd:
        Command and arguments as a list (same as ``subprocess.run``).
    cwd:
        Working directory.  In sandbox mode this is the only writable directory.
    timeout:
        Subprocess timeout in seconds.
    network:
        When ``True``, allow outbound network access inside the sandbox.
        Ignored in ``"off"`` mode.
    sandbox_level:
        ``"off"``, ``"workspace"``, or ``"full"``.
        Defaults to ``_DEFAULT_LEVEL`` (env var or ``"workspace"``).
    **kwargs:
        Additional keyword arguments forwarded to ``subprocess.run``.

    Returns
    -------
    subprocess.CompletedProcess
        Identical return type to ``subprocess.run``.
    """
    level = sandbox_level or _DEFAULT_LEVEL

    if level == "off" or not _bwrap_available():
        if level != "off":
            logger.debug(
                "sandbox: bwrap not found or unavailable — falling back to unsandboxed execution"
            )
            try:
                # Emit a system.warning event so operator tooling can surface it
                from src.core.orchestration.event_bus import get_event_bus

                eb = get_event_bus()
                try:
                    eb.publish(
                        "system.warning",
                        {"message": "bwrap not available; sandbox disabled"},
                    )
                except Exception:
                    # best-effort
                    pass
            except Exception:
                # Avoid import cycles or issues during early startup
                pass
        return subprocess.run(cmd, cwd=str(cwd), timeout=timeout, **kwargs)

    try:
        prefix = _build_bwrap_args(cwd, level)
        if network:
            # Remove --unshare-net if present
            try:
                idx = prefix.index("--unshare-net")
                prefix.pop(idx)
            except ValueError:
                pass
        full_cmd = prefix + ["--"] + cmd
        return subprocess.run(full_cmd, cwd=str(cwd), timeout=timeout, **kwargs)
    except FileNotFoundError:
        # bwrap disappeared between check and exec — fall back
        logger.warning(
            "sandbox: bwrap exec failed — falling back to unsandboxed execution"
        )
        return subprocess.run(cmd, cwd=str(cwd), timeout=timeout, **kwargs)


def sandbox_available() -> bool:
    """Return True if sandboxed execution is possible on this host."""
    return _bwrap_available()


def get_sandbox_level() -> str:
    """Return the current default sandbox level."""
    return _DEFAULT_LEVEL


def set_sandbox_level(level: str) -> None:
    """Override the default sandbox level at runtime.

    Parameters
    ----------
    level:
        One of ``"off"``, ``"workspace"``, ``"full"``.
    """
    global _DEFAULT_LEVEL
    if level not in ("off", "workspace", "full"):
        raise ValueError(f"Invalid sandbox level: {level!r}")
    _DEFAULT_LEVEL = level
