"""Bash execution tools — bash(), bash_readonly(), check_background_task().

Safety validation and policy gates live in ``src/tools/_bash_safety.py`` and are
re-exported here for backward compatibility with callers and tests.
"""

from __future__ import annotations

import logging
import os
import re as _re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

from src.core.orchestration.approval_gate import (
    discard_bash_denied,  # noqa: F401
    is_bash_denied,  # noqa: F401
    register_bash_gate,  # noqa: F401
)
from src.core.orchestration.event_bus import get_event_bus as _get_event_bus
from src.core.security.redaction import redact_secrets
from src.tools import sandbox as _sandbox
from src.tools._approval import (
    is_network_capable as _is_network_capable,  # noqa: F401
    is_tier3 as _is_tier3,  # noqa: F401
)
from src.tools.tools_config import is_autonomous as _is_autonomous  # noqa: F401
from src.tools._bash_safety import (
    _BASH_STDERR_MAX,  # noqa: F401
    _BASH_STDERR_MAX_TOKENS,  # noqa: F401
    _BASH_STDOUT_MAX,  # noqa: F401
    _BASH_STDOUT_MAX_TOKENS,  # noqa: F401
    _COMMAND_DENYLIST,  # noqa: F401
    _DANGEROUS_GIT_FLAGS,  # noqa: F401
    _DESTRUCTIVE_CMD_PATTERNS,  # noqa: F401
    _MUTATING_BRANCH_FLAGS,  # noqa: F401
    _MUTATING_TAG_FLAGS,  # noqa: F401
    _build_background_sandbox,
    _check_command_denylist,  # noqa: F401
    _check_git_command_safety,  # noqa: F401
    _check_network_policy,  # noqa: F401
    _check_secret_path_args,  # noqa: F401
    _check_shell_flags,  # noqa: F401
    _check_tier3_approval,  # noqa: F401
    _is_code_exec_flag,  # noqa: F401
    _is_os_sandbox_enforced,
    _matches_restricted_command,  # noqa: F401
    _normalize_command,
    _truncate_bash_output,
    _validate_bash_segment,
)
from src.tools._security import (
    BASH_STRICT_ALLOWLIST,  # noqa: F401
    DANGEROUS_PATTERNS,
    GIT_SAFE_SUBCOMMANDS,
    SAFE_COMMANDS,  # noqa: F401
    SED_WRITE_FLAGS,
)
from src.tools._tool import PermissionKind, tool
from src.tools.bash_security import (
    BashRiskLevel,
    SAFE_SANDBOX_OPERATORS,
    analyze_bash_command,
    split_shell_segments,
)

_logger = logging.getLogger(__name__)

# Sentinel for workdir default — resolved lazily to Path.cwd() at call time
# so the module can be imported without a fixed working directory.
_WORKDIR_DEFAULT = object()

_SPAWNED_BG_PIDS: Set[int] = set()
_SPAWNED_BG_PROCS: Dict[int, tuple[Any, Optional[str]]] = {}


@tool(side_effects=["execute"], tags=["coding"], permission_kind=PermissionKind.EXECUTE_BASH)
def bash(
    command: str,
    workdir: object = _WORKDIR_DEFAULT,
    description: str = "",
    timeout_secs: float = 60.0,
    run_in_background: bool = False,
) -> Dict[str, Any]:
    """Execute a shell command and return its output.

    Args:
        command: The shell command to run.
        workdir: Working directory for the command.
        description: Brief description of the command's intent (advisory; logged for auditability).
        timeout_secs: Maximum seconds to wait for the command (default 60). Ignored when run_in_background=True.
        run_in_background: If True, spawn the process without waiting and return a background_task_id (PID).
    """
    if workdir is _WORKDIR_DEFAULT:
        workdir = Path.cwd()
    workdir = Path(workdir)  # type: ignore[arg-type]

    if description:
        _logger.info("bash: %s | cmd=%r", description, command)

    # Pre-Gate: Unicode normalization — NFKC normalises homoglyphs and strips
    # control/format characters (null bytes, zero-width spaces, etc.).
    command = _normalize_command(command)

    sandbox_enforced = _is_os_sandbox_enforced()

    # Gate 1: Strict naive operator blocking when OS sandbox is NOT enforced
    # (unconfined host fallback). When an OS sandbox IS actively enforced,
    # quote-aware AST segmentation + per-segment validation below takes over.
    if not sandbox_enforced:
        _cmd_lower = _re.sub(r"\s+", " ", command).lower()
        _cmd_lower = _cmd_lower.replace("'", "").replace('"', "")
        for pattern in DANGEROUS_PATTERNS:
            if pattern in _cmd_lower:
                return {
                    "status": "error",
                    "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
                }

    # Gate 2: AST-level bash security analysis (always active).
    try:
        _risk_level, _risk_reasons = analyze_bash_command(
            command, working_dir=str(workdir)
        )
        if _risk_level == BashRiskLevel.BLOCKED:
            return {
                "status": "error",
                "error": f"Command blocked by security analysis: {'; '.join(_risk_reasons)}",
            }
        if _risk_level == BashRiskLevel.DANGEROUS:
            return {
                "status": "error",
                "error": (
                    f"Command blocked as dangerous (risk: {'; '.join(_risk_reasons)}). "
                    "Not allowed — requires operator approval."
                ),
                "requires_approval": True,
            }
    except ImportError:
        pass

    if sandbox_enforced:
        try:
            segments, operators = split_shell_segments(command)
        except ValueError as e:
            return {"status": "error", "error": f"Invalid command: {e}"}

        bad_ops = [op for op in operators if op not in SAFE_SANDBOX_OPERATORS]
        if bad_ops:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{bad_ops[0]}'. Redirections and background operators are not allowed.",
            }
        if not segments:
            return {"status": "error", "error": "Empty command"}
        if operators and len(segments) < len(operators) + 1:
            return {"status": "error", "error": "Invalid command: empty pipeline segment"}
        if operators and run_in_background:
            return {
                "status": "error",
                "error": "Shell pipelines/chains are not supported with run_in_background=True.",
            }

        for seg_parts in segments:
            seg_str = " ".join(shlex.quote(tok) for tok in seg_parts)
            seg_err = _validate_bash_segment(
                seg_parts, seg_str, Path(workdir), sandbox_enforced=True
            )
            if seg_err is not None:
                return seg_err

        if not operators:
            cmd_parts = segments[0]
        else:
            from src.tools.bash_security import reconstruct_shell_command

            reconstructed_cmd = reconstruct_shell_command(segments, operators)
            if Path("/bin/bash").is_file():
                from src.core.orchestration.graph.validation_authority import wrap_with_pipefail

                cmd_parts = ["/bin/bash", "-c", wrap_with_pipefail(reconstructed_cmd, "bash")]
            else:
                cmd_parts = ["/bin/sh", "-c", reconstructed_cmd]
    else:
        try:
            cmd_parts = shlex.split(command)
        except ValueError as e:
            return {"status": "error", "error": f"Invalid command: {e}"}

        if not cmd_parts:
            return {"status": "error", "error": "Empty command"}

        seg_err = _validate_bash_segment(
            cmd_parts, command, Path(workdir), sandbox_enforced=False
        )
        if seg_err is not None:
            return seg_err

    # Background execution: spawn without waiting, return PID as task ID.
    # TW-2: the background path must not silently escape sandbox enforcement —
    # wrap the spawn when a backend is available, refuse when the sandbox is
    # required but cannot be applied, and warn (interactive) when unsandboxed.
    if run_in_background:
        _bg_wrapped = _build_background_sandbox(cmd_parts, Path(workdir))
        if _bg_wrapped:
            if len(_bg_wrapped) >= len(cmd_parts) and _bg_wrapped[-len(cmd_parts) :] == cmd_parts:
                _bg_cmd = _bg_wrapped
            else:
                _bg_cmd = _bg_wrapped + cmd_parts
        else:
            _bg_cmd = cmd_parts
        _sb_profile_path: Optional[str] = None
        if _bg_wrapped and len(_bg_wrapped) >= 3 and _bg_wrapped[1] == "-f":
            _sb_profile_path = str(_bg_wrapped[2])
        if _bg_wrapped is None:
            try:
                _enforce = bool(_sandbox._enforcement_required())
            except Exception:
                _enforce = True
            if _enforce:
                return {
                    "status": "error",
                    "error": (
                        "Background execution refused: sandbox enforcement is "
                        "required (autonomous mode or SANDBOX_REQUIRE_ENFORCEMENT) "
                        "but no enforcing sandbox backend is available to run the "
                        "background process inside. Run the command in the "
                        "foreground instead (run_in_background=False)."
                    ),
                }
            try:
                _get_event_bus().publish(
                    "system.warning",
                    {
                        "message": (
                            "bash: background process running unsandboxed — no "
                            "enforcing sandbox backend available"
                        ),
                    },
                )
            except Exception:
                pass
        try:
            _bg_env = _sandbox._scrub_sandbox_env(os.environ)
            proc = subprocess.Popen(
                _bg_cmd,
                cwd=str(Path(workdir)),
                env=_bg_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            _SPAWNED_BG_PIDS.add(proc.pid)
            _SPAWNED_BG_PROCS[proc.pid] = (proc, _sb_profile_path)
            return {
                "status": "ok",
                "command": command,
                "background_task_id": str(proc.pid),
                "no_output_expected": True,
                "interrupted": False,
            }
        except FileNotFoundError:
            return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
        except OSError as e:
            return {"status": "error", "error": f"OS error: {e}"}

    if cmd_parts and cmd_parts[0] == "python" and shutil.which("python") is None:
        cmd_parts = [sys.executable] + list(cmd_parts[1:])

    try:
        result = _sandbox.run_sandboxed(
            cmd_parts,
            cwd=Path(workdir),
            timeout=timeout_secs,
            network=False,
            capture_output=True,
            text=True,
        )
        stdout, stderr, _out_cut, _err_cut = _truncate_bash_output(
            result.stdout, result.stderr
        )
        stdout = redact_secrets(stdout)
        stderr = redact_secrets(stderr)
        _rci = f"exit_code:{result.returncode}" if result.returncode != 0 else None
        out: Dict[str, Any] = {
            "status": "ok",
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "interrupted": False,
            "no_output_expected": not stdout.strip() and not stderr.strip(),
        }
        if _rci is not None:
            out["return_code_interpretation"] = _rci
        if _out_cut:
            out["stdout_truncated"] = True
        if _err_cut:
            out["stderr_truncated"] = True
        try:
            from src.core.orchestration.graph.validation_authority import (
                classify_shell_validation,
                is_validation_command,
            )

            if is_validation_command(command):
                out["validation_authority"] = classify_shell_validation(command)["authority"]
        except Exception:
            pass
        return out
    except subprocess.TimeoutExpired as _te:
        _raw_stdout = _te.stdout or ""
        _raw_stderr = _te.stderr or ""
        if isinstance(_raw_stdout, bytes):
            _raw_stdout = _raw_stdout.decode(errors="replace")
        if isinstance(_raw_stderr, bytes):
            _raw_stderr = _raw_stderr.decode(errors="replace")
        _raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(_raw_stdout, _raw_stderr)
        _raw_stdout = redact_secrets(_raw_stdout)
        _raw_stderr = redact_secrets(_raw_stderr)
        return {
            "status": "error",
            "command": command,
            "stdout": _raw_stdout,
            "stderr": _raw_stderr,
            "returncode": -1,
            "interrupted": True,
            "return_code_interpretation": "timeout",
            "error": "Command timed out",
            "no_output_expected": not _raw_stdout.strip() and not _raw_stderr.strip(),
        }
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except RuntimeError as _rte:
        return {"status": "error", "error": f"Sandbox execution denied: {_rte}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


@tool(
    side_effects=["execute"],
    tags=["coding", "debug", "review", "planning"],
    permission_kind=PermissionKind.EXECUTE_BASH,
)
def bash_readonly(
    command: str,
    workdir: object = _WORKDIR_DEFAULT,
    timeout_secs: float = 60.0,
) -> Dict[str, Any]:
    """Execute a read-only shell command (ls, grep, git status, cat, etc.).

    Only SAFE_COMMANDS (tier 1) are allowed. No test runners, no compilers,
    no file-writing operations. Prefer this over bash() for inspection tasks.

    Args:
        command: The shell command to run.
        workdir: Working directory for the command.
        timeout_secs: Maximum seconds to wait (default 60).
    """
    if workdir is _WORKDIR_DEFAULT:
        workdir = Path.cwd()
    workdir = Path(workdir)  # type: ignore[arg-type]

    # Pre-Gate: Unicode normalization — same as bash().
    command = _normalize_command(command)

    # Gate 1: Shell-operator / metacharacter block.
    _cmd_lower = _re.sub(r"\s+", " ", command).lower()
    _cmd_lower = _cmd_lower.replace("'", "").replace('"', "")
    for pattern in DANGEROUS_PATTERNS:
        if pattern in _cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    # Gate 2: AST-level bash security analysis.
    try:
        _risk_level, _risk_reasons = analyze_bash_command(
            command, working_dir=str(workdir)
        )
        if _risk_level in (BashRiskLevel.BLOCKED, BashRiskLevel.DANGEROUS):
            return {
                "status": "error",
                "error": f"Command blocked by security analysis: {'; '.join(_risk_reasons)}",
            }
    except ImportError:
        pass

    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"status": "error", "error": f"Invalid command: {e}"}

    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    first_cmd = cmd_parts[0].lower()

    # Gate 2b: P2-T3 explicit command denylist
    _deny_err = _check_command_denylist(command)
    if _deny_err is not None:
        return {"status": "error", "error": _deny_err}

    # Gate 2: Restricted commands are never allowed in read-only mode.
    if _matches_restricted_command(cmd_parts, command):
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' is not allowed in read-only mode.",
            "requires_approval": True,
        }

    # Gate 3: Only SAFE_COMMANDS (tier 1) — no test runners or compilers.
    # Git is handled separately via the subcommand allowlist (Gate 3b).
    if first_cmd != "git" and first_cmd not in SAFE_COMMANDS:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed in read-only mode. Allowed: {sorted(SAFE_COMMANDS)}",
        }

    # Gate 3b: Git subcommand allowlist (read-only mode is more restrictive).
    if first_cmd == "git":
        is_safe, msg = _check_git_command_safety(cmd_parts)
        if not is_safe:
            return {
                "status": "error",
                "error": (
                    f"{msg} "
                    f"Allowed: {sorted(GIT_SAFE_SUBCOMMANDS)}."
                ),
            }

    # Gate 3c: Block sed in-place edit flags and find -exec/-delete in read-only mode.
    if first_cmd == "sed":
        for token in cmd_parts[1:]:
            if token in SED_WRITE_FLAGS or token.startswith("-i"):
                return {
                    "status": "error",
                    "error": (
                        f"sed flag '{token}' performs in-place file modification "
                        "and is not allowed in read-only mode."
                    ),
                }
    elif first_cmd == "find":
        for token in cmd_parts[1:]:
            if token in ("-exec", "-execdir", "-ok", "-okdir", "-delete"):
                return {
                    "status": "error",
                    "error": (
                        f"find flag '{token}' is not allowed in read-only mode."
                    ),
                }

    # Gate 4: Block inline code-execution flags.
    for part in cmd_parts[1:]:
        if _is_code_exec_flag(first_cmd, part):
            return {
                "status": "error",
                "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked.",
            }

    # Gate 5: Archive / inplace-edit flag check (shared helper).
    _flag_err = _check_shell_flags(cmd_parts, first_cmd)
    if _flag_err is not None:
        return _flag_err

    # Gate 6: Block access to secret/protected files in arguments
    _secret_err = _check_secret_path_args(cmd_parts, workdir)
    if _secret_err is not None:
        return _secret_err

    # Execute inside sandbox (network disabled) — prevents exfiltration even for
    # read-only commands. Falls back to plain subprocess when bwrap unavailable.

    if cmd_parts and cmd_parts[0] == "python" and shutil.which("python") is None:
        cmd_parts = [sys.executable] + list(cmd_parts[1:])

    try:
        result = _sandbox.run_sandboxed(
            cmd_parts,
            cwd=Path(workdir),
            timeout=timeout_secs,
            network=False,
            capture_output=True,
            text=True,
        )
        stdout, stderr, _out_cut, _err_cut = _truncate_bash_output(
            result.stdout, result.stderr
        )
        stdout = redact_secrets(stdout)
        stderr = redact_secrets(stderr)
        _rci = f"exit_code:{result.returncode}" if result.returncode != 0 else None
        out: Dict[str, Any] = {
            "status": "ok",
            "command": command,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "interrupted": False,
            "no_output_expected": not stdout.strip() and not stderr.strip(),
        }
        if _rci is not None:
            out["return_code_interpretation"] = _rci
        if _out_cut:
            out["stdout_truncated"] = True
        if _err_cut:
            out["stderr_truncated"] = True
        try:
            from src.core.orchestration.graph.validation_authority import (
                classify_shell_validation,
                is_validation_command,
            )

            if is_validation_command(command):
                out["validation_authority"] = classify_shell_validation(command)["authority"]
        except Exception:
            pass
        return out
    except subprocess.TimeoutExpired as _te:
        _raw_stdout = _te.stdout or ""
        _raw_stderr = _te.stderr or ""
        if isinstance(_raw_stdout, bytes):
            _raw_stdout = _raw_stdout.decode(errors="replace")
        if isinstance(_raw_stderr, bytes):
            _raw_stderr = _raw_stderr.decode(errors="replace")
        _raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(_raw_stdout, _raw_stderr)
        _raw_stdout = redact_secrets(_raw_stdout)
        _raw_stderr = redact_secrets(_raw_stderr)
        return {
            "status": "error",
            "command": command,
            "stdout": _raw_stdout,
            "stderr": _raw_stderr,
            "returncode": -1,
            "interrupted": True,
            "return_code_interpretation": "timeout",
            "error": "Command timed out",
            "no_output_expected": not _raw_stdout.strip() and not _raw_stderr.strip(),
        }
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except RuntimeError as _rte:
        return {"status": "error", "error": f"Sandbox execution denied: {_rte}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


def has_active_background_tasks() -> bool:
    """Return True when at least one background bash subprocess handle is tracked."""
    return bool(_SPAWNED_BG_PROCS)


@tool(tags=["coding"], permission_kind=PermissionKind.NONE)
def check_background_task(
    task_id: str, workdir: object = _WORKDIR_DEFAULT
) -> Dict[str, Any]:  # type: ignore[assignment]
    """Poll the status of a background process started with bash(run_in_background=True).

    Args:
        task_id: The background_task_id (PID) returned by bash(run_in_background=True).
        workdir: Unused; kept for API consistency.

    Returns a dict with:
        running (bool): True if the process is still alive.
        pid (int): The process ID.
        exit_code (int | None): Exit code if the process has finished, else None.
    """
    import os

    try:
        pid = int(task_id)
    except (ValueError, TypeError):
        return {
            "status": "error",
            "error": f"Invalid task_id: {task_id!r} — expected a PID string.",
        }

    if pid not in _SPAWNED_BG_PIDS and pid not in _SPAWNED_BG_PROCS:
        return {
            "status": "error",
            "error": f"Unknown or untracked background task ID: {task_id}. Only processes started with bash(run_in_background=True) in this session can be checked.",
        }

    tracked = _SPAWNED_BG_PROCS.get(pid)
    if tracked is not None:
        proc, sb_path = tracked
        rc = proc.poll()
        if rc is None:
            return {"status": "ok", "pid": pid, "running": True, "exit_code": None}
        # Reap completed process & clean up temporary .sb Seatbelt profile if any
        _SPAWNED_BG_PROCS.pop(pid, None)
        _SPAWNED_BG_PIDS.discard(pid)
        if sb_path:
            try:
                Path(sb_path).unlink(missing_ok=True)
            except Exception:
                pass
        return {"status": "ok", "pid": pid, "running": False, "exit_code": rc}

    try:
        # os.kill(pid, 0) succeeds if the process exists; raises OSError if not.
        os.kill(pid, 0)
        return {"status": "ok", "pid": pid, "running": True, "exit_code": None}
    except ProcessLookupError:
        return {"status": "ok", "pid": pid, "running": False, "exit_code": None}
    except PermissionError:
        # Process exists but we don't own it — it's running.
        return {"status": "ok", "pid": pid, "running": True, "exit_code": None}
    except OSError:
        return {"status": "ok", "pid": pid, "running": False, "exit_code": None}

