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

# Explicit unsafe opt-out: by default, un-enforced host execution is denied (fail closed).
# Setting CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1 permits unconfined host execution fallback.
_ALLOW_UNENFORCED: bool = os.environ.get(
    "CODINGAGENT_SANDBOX_ALLOW_UNENFORCED",
    os.environ.get("SANDBOX_ALLOW_UNENFORCED", ""),
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
    configurations it silently accepts profiles but does not enforce them,
    or fails to apply them (sandbox_apply: Operation not permitted).
    We detect this by attempting a write inside an allowed probe dir (must succeed)
    and a write outside it (must be denied).

    Returns True when the sandbox appears to be enforced, False otherwise.
    Errors in the probe itself are treated conservatively as "not enforced".
    """
    if not _sandbox_exec_available():
        return False
    if platform.system() != "Darwin":
        return False
    try:
        import tempfile as _tf

        with _tf.TemporaryDirectory(prefix="codingagent-probe-") as probe_dir:
            probe_path = Path(probe_dir).resolve()
            target_inside = probe_path / "inside_ok.txt"
            target_outside = probe_path.parent / f"codingagent_probe_canary_{os.getpid()}.txt"

            escaped_probe = str(probe_path).replace("\\", "\\\\").replace('"', '\\"')
            profile = (
                "(version 1)\n"
                "(allow default)\n"
                f'(deny file-write* (require-not (subpath "{escaped_probe}")))\n'
            )
            _fd, profile_path = _tf.mkstemp(suffix=".sb", prefix="codingagent-probe-")
            try:
                with os.fdopen(_fd, "w") as f:
                    f.write(profile)

                # Probe A: Write inside probe_dir must succeed
                cmd_inside = [
                    str(_SANDBOX_EXEC_PATH),
                    "-f", profile_path,
                    "sh", "-c",
                    f'echo ok > "{target_inside}"',
                ]
                res_inside = subprocess.run(
                    cmd_inside, capture_output=True, text=True, timeout=5
                )
                if res_inside.returncode != 0 or not target_inside.exists():
                    logger.debug(
                        "sandbox-exec Probe A (inside write) failed: code %d, stderr: %s",
                        res_inside.returncode,
                        res_inside.stderr,
                    )
                    return False

                # Probe B: Write outside probe_dir must fail / be denied
                cmd_outside = [
                    str(_SANDBOX_EXEC_PATH),
                    "-f", profile_path,
                    "sh", "-c",
                    f'echo canary > "{target_outside}" 2>/dev/null',
                ]
                subprocess.run(
                    cmd_outside, capture_output=True, text=True, timeout=5
                )
                if target_outside.exists():
                    try:
                        target_outside.unlink()
                    except Exception:
                        pass
                    logger.debug("sandbox-exec Probe B failed: canary file was created outside sandbox")
                    return False

                return True
            finally:
                try:
                    os.unlink(profile_path)
                except Exception:
                    pass
                if target_outside.exists():
                    try:
                        target_outside.unlink()
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


def _sbpl_escape(p: str) -> str:
    return p.replace("\\", "\\\\").replace('"', '\\"')


def _build_sandbox_exc_profile(cwd: Path, level: str, private_tmp: Optional[Path] = None) -> str:
    """Generate a sandbox-exec profile string for the given *level*.

    The profile:
    - Allows read-only access to /usr, /bin, /sbin, /System, /Library
    - Denies all writes by default
    - Allows writable access to *cwd* and private temporary dir only
    - Denies network by default (no network-outbound rule)
    """
    cwd_str = _sbpl_escape(str(cwd.resolve()))
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "",
        ";; ---- writable working directory and dev nodes ----",
        "(allow file-write*",
        f'    (subpath "{cwd_str}")',
    ]
    if private_tmp is not None:
        lines.append(f'    (subpath "{_sbpl_escape(str(private_tmp.resolve()))}")')
    lines += [
        '    (literal "/dev/null")',
        '    (literal "/dev/zero")',
        '    (literal "/dev/tty")',
        '    (literal "/dev/dtracehelper")',
        ")",
        "",
        ";; ---- isolate /tmp read access ----",
        "(deny file-read*",
        '    (subpath "/tmp")',
        '    (subpath "/private/tmp")',
        ")",
        "(allow file-read*",
        f'    (subpath "{cwd_str}")',
    ]
    if private_tmp is not None:
        lines.append(f'    (subpath "{_sbpl_escape(str(private_tmp.resolve()))}")')
    lines.append(")")

    lines += [
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


def _write_sandbox_exc_profile(cwd: Path, level: str, private_tmp: Optional[Path] = None) -> str:
    """Write a temporary sandbox-exec profile and return its path.

    The file is created in a temporary directory that persists until the
    process exits (deleted by the OS on close in /tmp).
    """
    profile_content = _build_sandbox_exc_profile(cwd, level, private_tmp=private_tmp)
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
    for d in (
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/alternatives",
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/localtime",
    ):
        if Path(d).exists():
            args += ["--ro-bind", d, d]

    # Minimal proc and dev setup
    if Path("/proc").exists():
        args += ["--proc", "/proc"]
    if Path("/dev").exists():
        args += ["--dev", "/dev"]

    # Private tmpfs mount for /tmp (prevents exposing host temporary data/tokens)
    args += ["--tmpfs", "/tmp"]

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


def _run_subprocess_proc_group(
    cmd: List[str], cwd: Path | str, timeout: float, **kwargs
) -> subprocess.CompletedProcess:
    """Run command in a new process group so timeouts/cancellations kill all child processes."""
    if os.name == "posix" and "start_new_session" not in kwargs:
        kwargs["start_new_session"] = True

    try:
        from src.core.orchestration.tool_execution_pipeline import (
            register_current_tool_process,
            unregister_current_tool_process,
        )
    except Exception:
        register_current_tool_process = None
        unregister_current_tool_process = None

    proc = subprocess.Popen(cmd, cwd=str(cwd), **kwargs)

    if register_current_tool_process:
        try:
            register_current_tool_process(proc)
        except Exception:
            pass

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        retcode = proc.poll()
        return subprocess.CompletedProcess(proc.args, retcode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        raise exc
    finally:
        if unregister_current_tool_process:
            try:
                unregister_current_tool_process(proc)
            except Exception:
                pass


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
            if _enforcement_required():
                raise RuntimeError(
                    "sandbox: sandbox-exec is present but NOT enforcing restrictions "
                    "on this host (Apple Silicon / modern macOS deprecation). "
                    "Refusing to run unsandboxed command because "
                    "sandbox enforcement is required by default (fail-closed). "
                    "Install bwrap, set CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1, "
                    "or set CODINGAGENT_SANDBOX_LEVEL=off to opt out."
                )
        else:
            private_tmp = None
            try:
                import tempfile as _tf

                private_tmp = Path(_tf.mkdtemp(prefix="codingagent-sandbox-tmp-"))
                profile_path = _write_sandbox_exc_profile(cwd, level, private_tmp=private_tmp)
                try:
                    sbox_cmd = [str(_SANDBOX_EXEC_PATH), "-f", profile_path] + cmd
                    exec_kwargs = dict(kwargs)
                    run_env = dict(exec_kwargs.get("env") or os.environ)
                    run_env["TMPDIR"] = str(private_tmp)
                    exec_kwargs["env"] = run_env

                    result = subprocess.run(
                        sbox_cmd, cwd=str(cwd), timeout=timeout, **exec_kwargs
                    )
                    _stderr = result.stderr or ""
                    if isinstance(_stderr, bytes):
                        _stderr = _stderr.decode(errors="replace")
                    if result.returncode in (65, 71) and (
                        "sandbox-exec:" in str(_stderr)
                        or "sandbox_apply:" in str(_stderr)
                    ):
                        logger.warning(
                            "sandbox: sandbox-exec failed (%s)",
                            str(_stderr).splitlines()[0] if _stderr else f"exit code {result.returncode}",
                        )
                        if _enforcement_required():
                            raise RuntimeError(
                                f"sandbox: sandbox-exec failed to apply profile ({_stderr.strip()}). "
                                "Enforcement is required (fail-closed)."
                            )
                    else:
                        return result
                finally:
                    try:
                        os.unlink(profile_path)
                    except Exception:
                        pass
            except RuntimeError:
                raise
            except Exception as exc:
                logger.warning(
                    "sandbox: sandbox-exec failed (%s) — falling back to unsandboxed",
                    exc,
                )
                if _enforcement_required():
                    raise RuntimeError(f"sandbox: sandbox-exec failed: {exc}")
            finally:
                if private_tmp is not None:
                    try:
                        import shutil as _shutil

                        _shutil.rmtree(private_tmp, ignore_errors=True)
                    except Exception:
                        pass

    # 4. Final fallback — plain subprocess with warning (or hard-stop)
    if level != "off":
        if _enforcement_required():
            raise RuntimeError(
                "sandbox: no sandbox backend is available or enforcing. "
                "Refusing to run unsandboxed command because "
                "sandbox enforcement is required by default (fail-closed). "
                "Install bwrap, set CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1, "
                "or set CODINGAGENT_SANDBOX_LEVEL=off to opt out."
            )
        import sys as _sys

        _warn = (
            "WARNING: sandbox: bwrap and sandbox-exec are unavailable — "
            "running command WITHOUT sandboxing due to explicit opt-out. "
            "Set CODINGAGENT_SANDBOX_LEVEL=off to suppress.\n"
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


def _autonomous_mode() -> bool:
    """Return True when the agent is in autonomous (non-interactive) mode.

    Wrapped in try/except so a tools_config import problem can never disable
    sandbox enforcement (graceful-degradation, fail-safe direction).
    """
    try:
        from src.tools.tools_config import is_autonomous

        return bool(is_autonomous())
    except Exception:
        return False


def _allow_unenforced() -> bool:
    """Return True when the operator explicitly opted in to unconfined host execution."""
    return os.environ.get(
        "CODINGAGENT_SANDBOX_ALLOW_UNENFORCED",
        os.environ.get("SANDBOX_ALLOW_UNENFORCED", ""),
    ).strip().lower() in ("1", "true", "yes") or _ALLOW_UNENFORCED


def _enforcement_required() -> bool:
    """Return True when unsandboxed execution must be refused.

    By default, unavailable or non-enforcing sandboxing refuses to execute commands (fail-closed).
    Execution without enforcement is only permitted when the operator explicitly opts in via
    CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1 (or CODINGAGENT_SANDBOX_LEVEL=off).
    When SANDBOX_REQUIRE_ENFORCEMENT=1 or autonomous mode is active, enforcement
    is strictly required regardless of opt-outs.
    """
    if get_sandbox_level() == "off" and not (_REQUIRE_ENFORCEMENT or _autonomous_mode()):
        return False
    if _REQUIRE_ENFORCEMENT or _autonomous_mode():
        return True
    return not _allow_unenforced()


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

