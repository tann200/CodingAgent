"""Bash execution tools — bash(), bash_readonly(), check_background_task().

Extracted from file_tools.py so the 480-line bash execution block has its own
module.  All public names are re-exported from src.tools.file_tools for
backward compatibility.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Any, Optional

# Import security constants
from src.tools._security import (
    DANGEROUS_PATTERNS,
    SAFE_COMMANDS,
    TEST_COMPILE_COMMANDS,
    RESTRICTED_COMMANDS,
    RESTRICTED_ALLOWED_SUBCOMMANDS,
    CODE_EXEC_INTERPRETERS,
    CODE_EXEC_FLAGS,
    TAR_EXTRACT_FLAGS,
    TAR_CREATE_FLAGS,
    GIT_SAFE_SUBCOMMANDS,
    SED_WRITE_FLAGS,
)
from src.tools._tool import tool

_logger = logging.getLogger(__name__)

# Sentinel for workdir default — resolved lazily to Path.cwd() at call time
# so the module can be imported without a fixed working directory.
_WORKDIR_DEFAULT = object()

# Output size caps — tests import these directly from file_tools; the re-export
# there reads from here so the values are authoritative in this module.
_BASH_STDOUT_MAX = 16_384
_BASH_STDERR_MAX = 6_000  # raised from 2 KB — Python tracebacks routinely exceed 2 KB
# Token-based caps: when the tokenizer is available, these token budgets take
# precedence so the LLM never receives unexpectedly large context from a single
# bash call.  Byte caps remain as a safety net when tiktoken is unavailable.
_BASH_STDOUT_MAX_TOKENS = 2_000  # ~8 KB of typical code at 1 tok ≈ 4 chars
_BASH_STDERR_MAX_TOKENS = 600  # enough for a full Python traceback


def _truncate_bash_output(stdout: str, stderr: str) -> tuple[str, str, bool, bool]:
    """Cap bash stdout/stderr and append a notice when truncated.

    Returns (stdout, stderr, stdout_was_truncated, stderr_was_truncated).

    Truncation uses token budgets when ``src.core.inference.tokenizer`` is
    available (tiktoken or the character-heuristic fallback).  Byte caps are
    kept as a fast safety net for when the tokenizer cannot be imported.
    """
    try:
        from src.core.inference.tokenizer import count_tokens

        def _token_truncate(text: str, max_tokens: int, label: str) -> tuple[str, bool]:
            if not text:
                return text, False
            tok = count_tokens(text)
            if tok <= max_tokens:
                return text, False
            # Binary-search the char boundary that brings tokens under budget.
            lo, hi = 0, len(text)
            while hi - lo > 64:
                mid = (lo + hi) // 2
                if count_tokens(text[:mid]) < max_tokens:
                    lo = mid
                else:
                    hi = mid
            cut = lo
            omitted_tokens = tok - count_tokens(text[:cut])
            return (
                text[:cut]
                + f"\n... [{label} truncated: ~{omitted_tokens} tokens omitted, reason: size_limit]",
                True,
            )

        stdout, stdout_cut = _token_truncate(stdout, _BASH_STDOUT_MAX_TOKENS, "output")
        stderr, stderr_cut = _token_truncate(stderr, _BASH_STDERR_MAX_TOKENS, "stderr")
        return stdout, stderr, stdout_cut, stderr_cut

    except ImportError:
        pass

    # Byte-based fallback
    stdout_cut = False
    if len(stdout) > _BASH_STDOUT_MAX:
        omitted = len(stdout) - _BASH_STDOUT_MAX
        stdout = (
            stdout[:_BASH_STDOUT_MAX]
            + f"\n... [output truncated: {omitted} chars omitted, reason: size_limit]"
        )
        stdout_cut = True
    stderr_cut = False
    if len(stderr) > _BASH_STDERR_MAX:
        omitted = len(stderr) - _BASH_STDERR_MAX
        stderr = (
            stderr[:_BASH_STDERR_MAX]
            + f"\n... [stderr truncated: {omitted} chars omitted, reason: size_limit]"
        )
        stderr_cut = True
    return stdout, stderr, stdout_cut, stderr_cut


def _check_shell_flags(cmd_parts: list, first_cmd: str) -> Optional[Dict[str, Any]]:
    """Check for disallowed archive/inplace-edit flags.

    Returns an error dict if a blocked flag is found, else None.
    Shared by both ``bash()`` and ``bash_readonly()`` to avoid duplication.
    """
    # Destructive-command patterns checked by this gate (distinct from the
    # module-level DANGEROUS_PATTERNS which checks shell metacharacters).
    _DESTRUCTIVE_CMD_PATTERNS = [
        (
            "rm",
            ["-rf", "-r", "-f", "--recursive", "--force"],
            "Use delete_file tool instead of rm -rf",
        ),
        ("dd", ["of=", "conv=notrunc"], "dd with output file is not allowed"),
        ("mkfs", [], "Filesystem creation is not allowed"),
        ("fdisk", [], "Partition editing is not allowed"),
        ("parted", [], "Partition editing is not allowed"),
        ("ssh", ["-i"], "Interactive SSH is not allowed; use ssh with key auth only"),
        ("chmod", ["777"], "World-writable permissions (777) are not allowed"),
        ("chown", [], "Ownership change is not allowed"),
        ("killall", [], "killall is not allowed; use kill with specific PID"),
        ("pkill", ["-f"], "Process killing by name is not allowed"),
        ("reboot", [], "System reboot is not allowed"),
        ("shutdown", [], "System shutdown is not allowed"),
        ("init", [], "Init control is not allowed"),
        ("halt", [], "System halt is not allowed"),
        ("poweroff", [], "Power off is not allowed"),
        ("mount", [], "Mount operations require approval"),
        ("umount", [], "Unmount operations require approval"),
        (":(){:|:&};:", None, "Fork bomb detected"),
        (
            "curl",
            ["-o", "--output"],
            "File download requires approval; use web_tools instead",
        ),
        (
            "wget",
            ["-O", "--output-document"],
            "File download requires approval; use web_tools instead",
        ),
    ]

    for cmd, flags, msg in _DESTRUCTIVE_CMD_PATTERNS:
        if first_cmd == cmd:
            if not flags:
                return {"status": "error", "error": msg}
            for part in cmd_parts[1:]:
                for flag in flags:
                    if part == flag or part.startswith(flag + "="):
                        return {"status": "error", "error": msg}

    if first_cmd == "sed":
        for _part in cmd_parts[1:]:
            if (
                _part == "-i"
                or _part == "--in-place"
                or _part.startswith("--in-place=")
                or (
                    _part.startswith("-")
                    and not _part.startswith("--")
                    and "i" in _part[1:]
                )
            ):
                return {
                    "status": "error",
                    "error": "sed -i (in-place edit) is not allowed. Use edit_file or edit_file_atomic instead.",
                }
    elif first_cmd == "tar":
        for part in cmd_parts[1:]:
            stripped = part.lstrip("-")
            if part in TAR_EXTRACT_FLAGS or (
                part.startswith("-") and not part.startswith("--") and "x" in stripped
            ):
                return {
                    "status": "error",
                    "error": "tar extract is not allowed. Use tar -t / --list to inspect archives.",
                }
            if part in TAR_CREATE_FLAGS or (
                part.startswith("-") and not part.startswith("--") and "c" in stripped
            ):
                return {
                    "status": "error",
                    "error": "tar archive creation is not allowed. SAFE_COMMANDS permits tar for inspection only.",
                }
    elif first_cmd == "unzip":
        if "-l" not in cmd_parts[1:]:
            return {
                "status": "error",
                "error": "unzip without -l (list) is not allowed. Use unzip -l to inspect archive contents.",
            }
    elif first_cmd == "env":
        if "-i" in cmd_parts[1:] or "--ignore-environment" in cmd_parts[1:]:
            return {
                "status": "error",
                "error": "env -i (clear environment) is not allowed.",
            }
    return None


@tool(side_effects=["execute"], tags=["coding"])
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
    import logging as _logging

    if description:
        _logging.getLogger(__name__).info(f"bash: {description} | cmd={command!r}")
    import subprocess
    import shlex
    import re as _re

    # Gate 1: Shell-operator / metacharacter block (DANGEROUS_PATTERNS).
    # Blocks &&, ||, ;, |, >, >>, <, $(, ` and destructive keywords on the
    # normalised (whitespace-collapsed, lowercased) command string so spacing
    # tricks like "r m  -rf" or "ls  |  grep" cannot bypass the check.
    _cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in _cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    # Gate 2: AST-level bash security analysis — catches advanced injection vectors
    # ($(...), backtick substitution, pipe-to-shell, fork bombs, disk-wipe ops) that
    # DANGEROUS_PATTERNS may miss (e.g. creative whitespace, multi-arg tricks).
    try:
        from src.tools.bash_security import analyze_bash_command, BashRiskLevel

        _risk_level, _risk_reasons = analyze_bash_command(command)
        if _risk_level == BashRiskLevel.BLOCKED:
            return {
                "status": "error",
                "error": f"Command blocked by security analysis: {'; '.join(_risk_reasons)}",
            }
    except ImportError:
        pass  # bash_security unavailable; Gate 1 above is still active

    try:
        cmd_parts = shlex.split(command)
    except ValueError as e:
        return {"status": "error", "error": f"Invalid command: {e}"}

    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    first_cmd = cmd_parts[0].lower()
    cmd_lower = _re.sub(r"\s+", " ", command).lower()

    # Gate 2: Restricted-command check (tier-3 candidates are blocked unless in the
    # RESTRICTED_ALLOWED_SUBCOMMANDS list, e.g. "npm test").
    for pattern in RESTRICTED_COMMANDS:
        if pattern in cmd_lower:
            allowed = any(
                cmd_lower.startswith(ok) for ok in RESTRICTED_ALLOWED_SUBCOMMANDS
            )
            if not allowed:
                return {
                    "status": "error",
                    "error": f"Command '{cmd_parts[0]}' requires user approval or sandboxed execution. "
                    f"Restricted commands include: pip, npm install, curl, wget, apt, sudo. "
                    f"Use safe alternatives or request user approval.",
                    "requires_approval": True,
                }

    # Gate 3: Block inline code-execution flags (python3 -c, node -e, ruby -e, php -r).
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked. "
                    "Run a script file instead (e.g. python3 script.py).",
                }

    # Gate 4: Archive / inplace-edit flag check (shared helper — also used by bash_readonly).
    _flag_err = _check_shell_flags(cmd_parts, first_cmd)
    if _flag_err is not None:
        return _flag_err

    # Gate 4b: Git subcommand allowlist — only read-only git operations are
    # auto-allowed.  Write operations (commit, push, reset, rm, …) require
    # explicit user approval or must go through the RESTRICTED_COMMANDS path.
    if first_cmd == "git":
        sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
        if sub not in GIT_SAFE_SUBCOMMANDS:
            return {
                "status": "error",
                "error": (
                    f"git subcommand '{sub}' is not in the read-only allowlist. "
                    f"Allowed: {sorted(GIT_SAFE_SUBCOMMANDS)}. "
                    "Write operations (commit, push, add, reset, …) require user approval."
                ),
                "requires_approval": True,
            }

    # Gate 5: Tier allowlist.
    if first_cmd in SAFE_COMMANDS:
        pass  # Auto-allowed
    elif first_cmd == "git":
        pass  # Already validated by Gate 4b (subcommand allowlist)
    elif first_cmd in TEST_COMPILE_COMMANDS:
        if first_cmd == "npm" and not any(
            x in cmd_lower for x in ["test", "run ", "start", "build", "lint"]
        ):
            return {
                "status": "error",
                "error": "npm: Only 'npm test', 'npm run', 'npm start', 'npm build', 'npm lint' are allowed. "
                "'npm install' requires user approval.",
                "requires_approval": True,
            }
    else:
        return {
            "status": "error",
            "error": f"Command '{cmd_parts[0]}' not allowed. Allowed: {sorted(SAFE_COMMANDS | TEST_COMPILE_COMMANDS)}",
        }

    # Tier-3 approval gate
    _approval_result = _check_tier3_approval(command)
    if _approval_result is not None:
        return _approval_result

    # Background execution: spawn without waiting, return PID as task ID.
    if run_in_background:
        try:
            proc = subprocess.Popen(
                cmd_parts,
                cwd=str(Path(workdir)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
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

    try:
        from src.tools.sandbox import run_sandboxed

        result = run_sandboxed(
            cmd_parts,
            cwd=Path(workdir),
            timeout=timeout_secs,
            capture_output=True,
            text=True,
        )
        stdout, stderr, _out_cut, _err_cut = _truncate_bash_output(
            result.stdout, result.stderr
        )
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
        return out
    except subprocess.TimeoutExpired as _te:
        _raw_stdout = _te.stdout or ""
        _raw_stderr = _te.stderr or ""
        if isinstance(_raw_stdout, bytes):
            _raw_stdout = _raw_stdout.decode(errors="replace")
        if isinstance(_raw_stderr, bytes):
            _raw_stderr = _raw_stderr.decode(errors="replace")
        _raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(_raw_stdout, _raw_stderr)
        return {
            "status": "ok",
            "command": command,
            "stdout": _raw_stdout,
            "stderr": _raw_stderr,
            "returncode": -1,
            "interrupted": True,
            "return_code_interpretation": "timeout",
            "no_output_expected": not _raw_stdout.strip() and not _raw_stderr.strip(),
        }
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


@tool(side_effects=["execute"], tags=["coding", "debug", "review", "planning"])
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
    import shlex
    import re as _re

    # Gate 1: Shell-operator / metacharacter block.
    _cmd_lower = _re.sub(r"\s+", " ", command).lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in _cmd_lower:
            return {
                "status": "error",
                "error": f"Command contains dangerous pattern '{pattern}'. No shell operators or destructive commands allowed.",
            }

    # Gate 2: AST-level bash security analysis.
    try:
        from src.tools.bash_security import analyze_bash_command, BashRiskLevel

        _risk_level, _risk_reasons = analyze_bash_command(command)
        if _risk_level == BashRiskLevel.BLOCKED:
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
    cmd_lower = _re.sub(r"\s+", " ", command).lower()

    # Gate 2: Restricted commands are never allowed in read-only mode.
    for pattern in RESTRICTED_COMMANDS:
        if pattern in cmd_lower:
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
        sub = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
        if sub not in GIT_SAFE_SUBCOMMANDS:
            return {
                "status": "error",
                "error": (
                    f"git subcommand '{sub}' is not allowed in read-only mode. "
                    f"Allowed: {sorted(GIT_SAFE_SUBCOMMANDS)}."
                ),
            }

    # Gate 3c: Block sed in-place edit flags (-i / --in-place) in read-only mode.
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

    # Gate 4: Block inline code-execution flags.
    if first_cmd in CODE_EXEC_INTERPRETERS:
        for part in cmd_parts[1:]:
            if part in CODE_EXEC_FLAGS:
                return {
                    "status": "error",
                    "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked.",
                }

    # Gate 5: Archive / inplace-edit flag check (shared helper).
    _flag_err = _check_shell_flags(cmd_parts, first_cmd)
    if _flag_err is not None:
        return _flag_err

    # Execute inside sandbox (network disabled) — prevents exfiltration even for
    # read-only commands. Falls back to plain subprocess when bwrap unavailable.
    import subprocess

    try:
        from src.tools.sandbox import run_sandboxed

        result = run_sandboxed(
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
        return out
    except subprocess.TimeoutExpired as _te:
        _raw_stdout = _te.stdout or ""
        _raw_stderr = _te.stderr or ""
        if isinstance(_raw_stdout, bytes):
            _raw_stdout = _raw_stdout.decode(errors="replace")
        if isinstance(_raw_stderr, bytes):
            _raw_stderr = _raw_stderr.decode(errors="replace")
        _raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(_raw_stdout, _raw_stderr)
        return {
            "status": "ok",
            "command": command,
            "stdout": _raw_stdout,
            "stderr": _raw_stderr,
            "returncode": -1,
            "interrupted": True,
            "return_code_interpretation": "timeout",
            "no_output_expected": not _raw_stdout.strip() and not _raw_stderr.strip(),
        }
    except FileNotFoundError:
        return {"status": "error", "error": f"Command not found: {cmd_parts[0]}"}
    except PermissionError:
        return {"status": "error", "error": f"Permission denied: {cmd_parts[0]}"}
    except OSError as e:
        return {"status": "error", "error": f"OS error: {e}"}


@tool(tags=["coding"])
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


def _check_tier3_approval(command: str) -> Optional[Dict[str, Any]]:
    """Check tier-3 approval gate.

    Returns approval result dict if command should be blocked, None if approved.
    """
    try:
        from src.tools._approval import is_tier3
        from src.tools.tools_config import is_autonomous

        # register_bash_gate and _bash_denied are defined in approval_gate.py.
        from src.core.orchestration.approval_gate import (
            register_bash_gate,
            _bash_denied,
        )
        from src.core.orchestration.event_bus import get_event_bus

        if is_tier3(command) and not is_autonomous():
            import uuid as _uuid_t3

            _tool_id = str(_uuid_t3.uuid4())[:8]
            _gate_ev = register_bash_gate(_tool_id)
            try:
                get_event_bus().publish(
                    "bash.approval_required",
                    {"tool_id": _tool_id, "command": command},
                )
            except Exception:
                pass

            _approved = _gate_ev.wait(timeout=120.0)
            if not _approved or _tool_id in _bash_denied:
                _bash_denied.discard(_tool_id)
                return {"status": "denied", "output": "Bash command denied by user."}
    except Exception as _gate_exc:
        # Gate setup failed — block for safety
        _logger.warning(
            "bash: tier-3 approval gate failed unexpectedly; blocking command for safety. "
            "Error: %s cmd=%r",
            _gate_exc,
            command,
        )
        return {
            "status": "error",
            "error": "Approval gate failure — command blocked for safety. Re-run to retry.",
        }

    return None
