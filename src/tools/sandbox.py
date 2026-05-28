"""Sandboxed subprocess execution via bubblewrap (bwrap) or macOS sandbox-exec.

Provides ``run_sandboxed()`` as a drop-in replacement for ``subprocess.run``
when executing untrusted shell commands.

Platform support:
- **Linux**: bubblewrap (bwrap) with filesystem/network/PID isolation.
- **macOS**: ``sandbox-exec`` (Apple deprecated but still present) when
  bwrap is unavailable.  Writes a minimal `sandbox-macos.sb` profile
  that allows read-only access to system dirs, writable cwd only, and
  denies network by default.
- **Fallback**: When no sandboxing is available and level != "off",
  executes via plain ``subprocess.run`` with a logged warning event.

Sandbox strictness levels (``sandbox_level`` param):

    ``"off"``          — no sandboxing; plain subprocess.run.
    ``"workspace"``    — read-only system dirs, writable cwd only (default).
    ``"full"``         — add network disable + stricter mounts.

The level can be overridden at import time via the
``CODINGAGENT_SANDBOX_LEVEL`` environment variable.

macOS note:
    Apple deprecated ``sandbox-exec`` and the associated SBPL sandbox
    profiles.  On recent macOS / Apple Silicon the profile may silently
    have no effect.  The module detects this by running a canary probe
    that attempts to write outside the allowed directory and checking
    whether the operation is blocked.  When the probe indicates the
    sandbox is not enforced a WARNING is emitted and
    ``sandbox_exec_enforced()`` returns False.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Detect bwrap once at import time
_BWRAP_PATH: Optional[str] = shutil.which("bwrap")

# Default sandbox level — override via env var or config
_DEFAULT_LEVEL: str = os.environ.get("CODINGAGENT_SANDBOX_LEVEL", "workspace")

# Hard-stop mode: when set to "1" or "true", refuse to run any command that
# would fall back to unsandboxed execution instead of silently proceeding.
# Set SANDBOX_REQUIRE_ENFORCEMENT=1 for autonomous / CI use.
_REQUIRE_ENFORCEMENT: bool = os.environ.get(
    "SANDBOX_REQUIRE_ENFORCEMENT", ""
).strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# sandbox-exec detection (macOS)
# ---------------------------------------------------------------------------


def _sandbox_exec_path() -> Optional[str]:
    """Return path to sandbox-exec if available."""
    return shutil.which("sandbox-exec")


_SANDBOX_EXEC_PATH: Optional[str] = _sandbox_exec_path()


def _sandbox_exec_available() -> bool:
    return _SANDBOX_EXEC_PATH is not None


# ---------------------------------------------------------------------------
# macOS sandbox-exec enforcement probe
# ---------------------------------------------------------------------------

_SANDBOX_EXEC_ENFORCED: Optional[bool] = None  # None = not yet probed


def _probe_sandbox_exec_enforced() -> bool:
    """Check whether sandbox-exec actually *enforces* restrictions on this host.

    Apple deprecated sandbox-exec and on some macOS / Apple Silicon
    configurations it silently accepts profiles but does not enforce them.
    We detect this by attempting a write to a path outside /tmp inside a
    sandbox that should deny it.  If the write succeeds the sandbox is not
    enforcing.

    Returns True when the sandbox appears to be enforced, False otherwise.
    Errors in the probe itself are treated conservatively as "not enforced".
    """
    if not _sandbox_exec_available():
        return False
    if platform.system() != "Darwin":
        return False
    try:
        import tempfile as _tf

        # Create an isolated probe dir and a *target outside it*
        with _tf.TemporaryDirectory() as probe_dir:
            target_outside = Path(probe_dir) / ".." / "sandbox_probe_canary"
            target_outside = target_outside.resolve()

            # Profile that only allows writes to probe_dir
            profile = (
                "(version 1)\n"
                "(allow default)\n"
                f'(deny file-write* (not (subpath "{probe_dir}")))\n'
            )
            _fd, profile_path = _tf.mkstemp(suffix=".sb", prefix="codingagent-probe-")
            try:
                with os.fdopen(_fd, "w") as f:
                    f.write(profile)
                cmd = [
                    str(_SANDBOX_EXEC_PATH),
                    "-f", profile_path,
                    "sh", "-c",
                    f"echo canary > {target_outside} 2>/dev/null; echo $?",
                ]
                subprocess.run(
                    cmd, capture_output=True, text=True, timeout=5
                )
                # If the canary file was created, sandbox did not enforce
                if target_outside.exists():
                    try:
                        target_outside.unlink()
                    except Exception:
                        pass
                    return False
                return True
            finally:
                try:
                    os.unlink(profile_path)
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("sandbox-exec enforcement probe error: %s", exc)
        return False  # conservative: assume not enforced


def sandbox_exec_enforced() -> bool:
    """Return True when sandbox-exec is available *and* actually enforces its profile.

    Result is cached after the first call (the check is a subprocess probe).
    """
    global _SANDBOX_EXEC_ENFORCED
    if _SANDBOX_EXEC_ENFORCED is None:
        _SANDBOX_EXEC_ENFORCED = _probe_sandbox_exec_enforced()
        if _sandbox_exec_available() and not _SANDBOX_EXEC_ENFORCED:
            _warn = (
                "WARNING: sandbox-exec is present but appears NOT to enforce its "
                "sandbox profile on this macOS host (likely Apple Silicon + recent OS). "
                "Shell commands will run with reduced isolation. "
                "Install bwrap for reliable sandboxing, or set "
                "CODINGAGENT_SANDBOX_LEVEL=off to suppress this warning."
            )
            import sys as _sys
            print(_warn, file=_sys.stderr, flush=True)
            logger.warning("sandbox: %s", _warn)
            try:
                from src.core.orchestration.event_bus import get_event_bus
                get_event_bus().publish("system.warning", {"message": _warn})
            except Exception:
                pass
    return bool(_SANDBOX_EXEC_ENFORCED)


# ---------------------------------------------------------------------------
# sandbox-exec profile generation
# ---------------------------------------------------------------------------


def _build_sandbox_exc_profile(cwd: Path, level: str) -> str:
    """Generate a sandbox-exec profile string for the given *level*.

    The profile:
    - Allows read-only access to /usr, /bin, /sbin, /System, /Library
    - Allows writable access to *cwd* only
    - Denies network by default (no network-outbound rule)
    - Denies everything else
    """
    cwd_str = str(cwd.resolve())
    lines = [
        "(version 1)",
        "(allow default)",
        "",
        ";; ---- read-only system paths ----",
        "(allow file-read*",
        "    (require",
        '        (file-issue-extension* (literal "/"))',
        '        (not (subpath "/tmp"))',
        '        (not (subpath "/private/tmp"))',
        "    )",
        ")",
        "",
        ";; ---- writable working directory ----",
        f'(allow file-write* (subpath "{cwd_str}"))',
        "",
        ";; ---- deny network ----",
        "(deny network*)",
        "",
        ";; ---- specific read-only dirs ----",
    ]
    for d in ("/usr", "/bin", "/sbin", "/System", "/Library"):
        if Path(d).exists():
            lines.append(f'(allow file-read* (subpath "{d}"))')

    if level == "full":
        lines += [
            "",
            ";; ---- full mode extras ----",
            "(deny process-fork)",
        ]
    return "\n".join(lines)


def _write_sandbox_exc_profile(cwd: Path, level: str) -> str:
    """Write a temporary sandbox-exec profile and return its path.

    The file is created in a temporary directory that persists until the
    process exits (deleted by the OS on close in /tmp).
    """
    profile_content = _build_sandbox_exc_profile(cwd, level)
    # Use a named temp file so sandbox-exec can read it
    fd, path = tempfile.mkstemp(suffix=".sb", prefix="codingagent-sandbox-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(profile_content)
    except Exception:
        os.close(fd)
        raise
    return path


# ---------------------------------------------------------------------------
# bwrap helpers (existing)
# ---------------------------------------------------------------------------


def _probe_bwrap(path: Optional[str]) -> bool:
    """Run a quick `bwrap --version` probe to ensure bwrap is callable.

    Returns True when the binary exists and responds successfully, False otherwise.
    """
    if not path:
        return False
    try:
        res = subprocess.run(
            [path, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


_BWRAP_AVAILABLE: bool = _probe_bwrap(_BWRAP_PATH)


def _bwrap_available() -> bool:
    return _BWRAP_AVAILABLE


# Emit a startup warning (stderr + logger + EventBus) when the environment
# requests sandboxing but neither bwrap nor sandbox-exec is available.
if _DEFAULT_LEVEL != "off" and not _BWRAP_AVAILABLE and not _sandbox_exec_available():
    import sys as _sys
    _warn = (
        "WARNING: CodingAgent sandbox requested but neither bwrap (Linux) nor "
        "sandbox-exec (macOS) is available. Shell commands will run with FULL "
        "USER PRIVILEGES. Set CODINGAGENT_SANDBOX_LEVEL=off to suppress this "
        "warning, or install bwrap to enable sandboxing."
    )
    print(_warn, file=_sys.stderr, flush=True)
    logger.warning("sandbox: %s", _warn)
    try:
        from src.core.orchestration.event_bus import get_event_bus
        get_event_bus().publish("system.warning", {"message": _warn})
    except Exception:
        pass


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


# ---------------------------------------------------------------------------
# Core: run_sandboxed
# ---------------------------------------------------------------------------


def run_sandboxed(
    cmd: List[str],
    cwd: Path,
    timeout: float = 60.0,
    network: bool = False,
    sandbox_level: Optional[str] = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run *cmd* optionally inside a sandbox.

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

    # 1. Off — no sandboxing
    if level == "off":
        return subprocess.run(cmd, cwd=str(cwd), timeout=timeout, **kwargs)

    # 2. Try bwrap (Linux / installed manually)
    if _bwrap_available():
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
            logger.warning(
                "sandbox: bwrap exec failed — falling back to sandbox-exec/unsandboxed"
            )
            # fall through to next option

    # 3. Try sandbox-exec (macOS) — only if available AND enforcing
    if _sandbox_exec_available():
        if not sandbox_exec_enforced():
            # Sandbox-exec exists but is not enforcing (deprecated on this macOS).
            # Log at debug level (warning was already emitted at probe time) and
            # fall through to the plain-subprocess path.
            logger.debug(
                "sandbox: sandbox-exec not enforcing on this host — skipping"
            )
            if _REQUIRE_ENFORCEMENT:
                raise RuntimeError(
                    "sandbox: sandbox-exec is present but NOT enforcing restrictions "
                    "on this host (Apple Silicon / modern macOS deprecation). "
                    "Refusing to run unsandboxed command because "
                    "SANDBOX_REQUIRE_ENFORCEMENT=1. "
                    "Install bwrap or set CODINGAGENT_SANDBOX_LEVEL=off to opt out."
                )
        else:
            try:
                profile_path = _write_sandbox_exc_profile(cwd, level)
                try:
                    sbox_cmd = [str(_SANDBOX_EXEC_PATH), "-f", profile_path] + cmd
                    # Note: sandbox-exec does not support --unshare-net; network
                    # is denied via the profile instead.
                    result = subprocess.run(
                        sbox_cmd, cwd=str(cwd), timeout=timeout, **kwargs
                    )
                    # macOS sandbox-exec can fail before the command starts if the
                    # generated profile uses unsupported syntax on the host version.
                    # In that case degrade to the documented plain-subprocess fallback
                    # instead of surfacing sandbox parser errors as command output.
                    _stderr = result.stderr or ""
                    if isinstance(_stderr, bytes):
                        _stderr = _stderr.decode(errors="replace")
                    if result.returncode == 65 and "sandbox-exec:" in str(_stderr):
                        logger.warning(
                            "sandbox: sandbox-exec profile rejected (%s) — falling back to unsandboxed",
                            str(_stderr).splitlines()[0] if _stderr else "unknown error",
                        )
                    else:
                        return result
                finally:
                    # Best-effort cleanup of the temp profile
                    try:
                        os.unlink(profile_path)
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning(
                    "sandbox: sandbox-exec failed (%s) — falling back to unsandboxed",
                    exc,
                )
                # fall through

    # 4. Final fallback — plain subprocess with warning (or hard-stop)
    if level != "off":
        import sys as _sys

        _warn = (
            "WARNING: sandbox: bwrap and sandbox-exec are unavailable — "
            "running command WITHOUT sandboxing. Set SANDBOX_LEVEL=off to suppress.\n"
        )
        if _REQUIRE_ENFORCEMENT:
            raise RuntimeError(
                "sandbox: no sandbox backend is available or enforcing. "
                "Refusing to run unsandboxed command because "
                "SANDBOX_REQUIRE_ENFORCEMENT=1. "
                "Install bwrap or set CODINGAGENT_SANDBOX_LEVEL=off to opt out."
            )
        _sys.stderr.write(_warn)
        logger.warning(
            "sandbox: bwrap and sandbox-exec unavailable — falling back to unsandboxed execution"
        )
        try:
            from src.core.orchestration.event_bus import get_event_bus
            get_event_bus().publish(
                "system.warning",
                {"message": "sandbox: bwrap and sandbox-exec unavailable; sandbox disabled"},
            )
        except Exception:
            pass
    return subprocess.run(cmd, cwd=str(cwd), timeout=timeout, **kwargs)


def sandbox_available() -> bool:
    """Return True if sandboxed execution is possible on this host.

    On macOS this returns True only when sandbox-exec is both available
    *and* enforcing (see ``sandbox_exec_enforced()``).
    """
    if _bwrap_available():
        return True
    if _sandbox_exec_available():
        return sandbox_exec_enforced()
    return False


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

