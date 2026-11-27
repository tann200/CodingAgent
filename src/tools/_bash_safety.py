"""Bash command safety, validation, policy, and sandbox helpers.

Extracted from ``src/tools/_bash_exec.py`` (TASK-43). All helpers are re-exported
from ``src.tools._bash_exec`` for backward compatibility.
"""

from __future__ import annotations

import logging
import os
import re as _re
import shlex
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.orchestration.approval_gate import (
    discard_bash_denied,
    is_bash_denied,
    register_bash_gate,
)
from src.core.orchestration.event_bus import get_event_bus as _get_event_bus
from src.core.security.redaction import is_secret_path
from src.tools import sandbox as _sandbox
from src.tools._approval import (
    is_network_capable as _is_network_capable,
    is_tier3 as _is_tier3,
)
from src.tools._security import (
    BASH_STRICT_ALLOWLIST,
    CODE_EXEC_FLAGS,
    CODE_EXEC_INTERPRETERS,
    GIT_SAFE_SUBCOMMANDS,
    RESTRICTED_ALLOWED_SUBCOMMANDS,
    RESTRICTED_COMMANDS,
    SAFE_COMMANDS,
    TAR_CREATE_FLAGS,
    TAR_EXTRACT_FLAGS,
    TEST_COMPILE_COMMANDS,
)
from src.tools.tools_config import is_autonomous as _is_autonomous

_logger = logging.getLogger(__name__)


def _bx_attr(name: str, default: Any) -> Any:
    """Resolve *name* from ``src.tools._bash_exec`` when loaded so test patches apply."""
    mod = sys.modules.get("src.tools._bash_exec")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _resolve_is_os_sandbox_enforced() -> bool:
    """Delegate through ``_bash_exec._is_os_sandbox_enforced`` so test patches apply."""
    fn = _bx_attr("_is_os_sandbox_enforced", _is_os_sandbox_enforced)
    if callable(fn) and fn is not _is_os_sandbox_enforced:
        return bool(fn())
    return _is_os_sandbox_enforced()


def _normalize_command(cmd: str) -> str:
    """Apply NFKC Unicode normalization and strip control characters.

    Prevents homoglyph bypasses (e.g. Cyrillic 'с' for ASCII 'c') and null-byte
    injection.  Normal whitespace (space, tab, newline) is preserved.
    """
    normalized = unicodedata.normalize("NFKC", cmd)
    return "".join(
        ch
        for ch in normalized
        if unicodedata.category(ch) not in ("Cc", "Cf") or ch in " \t\n\r"
    )

# Output size caps — tests import these directly from file_tools; the re-export
# there reads from here so the values are authoritative in this module.
_BASH_STDOUT_MAX = 16_384
_BASH_STDERR_MAX = 6_000  # raised from 2 KB — Python tracebacks routinely exceed 2 KB
# Token-based caps: when the tokenizer is available, these token budgets take
# precedence so the LLM never receives unexpectedly large context from a single
# bash call.  Byte caps remain as a safety net when tiktoken is unavailable.
_BASH_STDOUT_MAX_TOKENS = 2_000  # ~8 KB of typical code at 1 tok ≈ 4 chars
_BASH_STDERR_MAX_TOKENS = 600  # enough for a full Python traceback

# P2-T3: explicit denylist of commands that are unsafe regardless of flags.
# These supplement _DESTRUCTIVE_CMD_PATTERNS which checks flag-level restrictions.
# Operators can extend this list via agent_config.yaml bash_tool.command_denylist_extras.
_COMMAND_DENYLIST: frozenset = frozenset(
    {
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ftp",
        "sftp",
        "scp",
        "rsync",  # network exfiltration vectors (no-flag block)
        "mkfs",
        "fdisk",
        "parted",  # disk destruction
        "shutdown",
        "reboot",
        "halt",
        "poweroff",  # system control
        "crontab",  # persistence
    }
)


def _check_command_denylist(cmd: str) -> Optional[str]:
    """Return an error string if the first token of *cmd* is in _COMMAND_DENYLIST.

    Strips leading path components so ``/usr/bin/nc`` is treated as ``nc``.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None  # malformed shell — let sandbox handle it
    if not tokens:
        return None
    binary = Path(tokens[0]).name  # /usr/bin/curl → curl
    if binary in _COMMAND_DENYLIST:
        return (
            f"Command '{binary}' is blocked by the CodingAgent tool policy. "
            "If this command is required, add it to the command_denylist_extras "
            "allowlist_override in agent_config.yaml."
        )
    return None


_DANGEROUS_GIT_FLAGS: frozenset[str] = frozenset(
    {
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--upload-pack",
        "--receive-pack",
    }
)

_MUTATING_BRANCH_FLAGS: frozenset[str] = frozenset(
    {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--edit-description"}
)
_MUTATING_TAG_FLAGS: frozenset[str] = frozenset(
    {"-d", "-a", "-s", "-u", "-f", "--delete", "--force"}
)


def _is_code_exec_flag(interpreter: str, part: str) -> bool:
    """Return True if part is an inline code execution flag for interpreter."""
    base = os.path.basename(interpreter).lower()
    is_interp = (
        base in CODE_EXEC_INTERPRETERS
        or base.startswith(("python", "node", "ruby", "php"))
    )
    if not is_interp:
        return False
    if part == "-":
        return True
    if part.startswith(("--eval=", "--execute=")):
        return True
    if part in CODE_EXEC_FLAGS:
        return True
    if part.startswith("-") and not part.startswith("--"):
        # Match combined short flags like -Bc, -Ic, -e"code", -c"code", etc.
        return any(ch in ("c", "e", "r") for ch in part[1:])
    return False


def _check_git_command_safety(cmd_parts: list[str]) -> tuple[bool, str]:
    """Inspect git command parts.

    Returns (is_safe, error_or_subcommand_description).
    If is_safe is False:
      - If the error mentions 'Dangerous git flag', it's a hard block.
      - Otherwise, it requires approval (bash) or is blocked (bash_readonly).
    """
    if not cmd_parts or cmd_parts[0].lower() != "git":
        return True, ""

    # Check for dangerous git flags anywhere in the command
    for part in cmd_parts[1:]:
        p_lower = part.lower()
        if p_lower == "-c" or p_lower.startswith("-c="):
            return False, "Dangerous git flag '-c' is blocked."
        for d in (
            "--config-env",
            "--exec-path",
            "--git-dir",
            "--work-tree",
            "--upload-pack",
            "--receive-pack",
        ):
            if p_lower == d or p_lower.startswith(d + "="):
                return False, f"Dangerous git flag '{part}' is blocked."

    # Find the subcommand (first non-option argument)
    subcommand = None
    sub_index = -1
    i = 1
    while i < len(cmd_parts):
        token = cmd_parts[i]
        if token.startswith("-"):
            if token == "-C" and i + 1 < len(cmd_parts):
                i += 1
            i += 1
            continue
        subcommand = token.lower()
        sub_index = i
        break

    if not subcommand:
        # e.g., 'git' or 'git --version'
        if any(p.lower() in ("--version", "-v", "--help", "-h") for p in cmd_parts[1:]):
            return True, "version"
        return False, "git with no subcommand requires approval."

    rest_tokens = cmd_parts[sub_index + 1 :]

    # Pure read-only subcommands that accept any read arguments
    if subcommand in {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "shortlog",
        "describe",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "rev-list",
        "help",
        "version",
    }:
        return True, subcommand

    # branch: safe only when listing branches
    if subcommand == "branch":
        if any(p in _MUTATING_BRANCH_FLAGS for p in rest_tokens):
            return False, "git branch with mutation flag is not allowed without approval."
        non_flag_args = [p for p in rest_tokens if not p.startswith("-")]
        if non_flag_args:
            return False, f"git branch '{non_flag_args[0]}' creates or modifies branches."
        return True, "branch"

    # tag: safe only when listing
    if subcommand == "tag":
        if any(p in _MUTATING_TAG_FLAGS for p in rest_tokens):
            return False, "git tag with mutation flag is not allowed without approval."
        non_flag_args = [p for p in rest_tokens if not p.startswith("-")]
        if non_flag_args:
            return False, f"git tag '{non_flag_args[0]}' creates or modifies tags."
        return True, "tag"

    # stash: safe only if 'stash list'
    if subcommand == "stash":
        if rest_tokens and rest_tokens[0].lower() == "list":
            return True, "stash list"
        return False, "git stash operation is not in the read-only allowlist."

    # config: safe only with --list, -l, or --get
    if subcommand == "config":
        first_rest = rest_tokens[0].lower() if rest_tokens else ""
        if first_rest in ("--list", "-l", "--get"):
            if not any(p.lower() == "--global" for p in rest_tokens):
                return True, f"config {first_rest}"
        return False, "git config operation is not in the read-only allowlist."

    # remote: safe with 'remote' or 'remote -v'
    if subcommand == "remote":
        if not rest_tokens or (len(rest_tokens) == 1 and rest_tokens[0].lower() in ("-v", "--verbose")):
            return True, "remote"
        return False, f"git remote operation '{rest_tokens[0]}' is not in the read-only allowlist."

    return False, f"git subcommand '{subcommand}' is not in the read-only allowlist."


# Destructive-command patterns checked by _check_shell_flags()
# (distinct from DANGEROUS_PATTERNS which checks shell metacharacters)
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
    (":(){ :|:&};:", None, "Fork bomb detected"),
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


def _check_shell_flags(cmd_parts: list, first_cmd: str) -> Optional[Dict[str, Any]]:
    """Check for disallowed archive/inplace-edit flags.

    Returns an error dict if a blocked flag is found, else None.
    Shared by both ``bash()`` and ``bash_readonly()`` to avoid duplication.
    """
    # Destructive-command patterns checked by this gate (distinct from the
    # module-level DANGEROUS_PATTERNS which checks shell metacharacters).

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
        # Block env VAR=val cmd — environment variable injection can bypass
        # security controls via LD_PRELOAD, PYTHONPATH, PATH, etc.
        sub_idx = None
        for idx, _part in enumerate(cmd_parts[1:], 1):
            if _part.startswith("-"):
                continue  # skip env flags (e.g., -u, --unset)
            if "=" in _part:
                return {
                    "status": "error",
                    "error": (
                        f"env with variable assignment '{_part}' is not allowed. "
                        "Environment variable injection (LD_PRELOAD, PYTHONPATH, PATH, etc.) "
                        "can bypass security controls."
                    ),
                }
            sub_idx = idx
            break  # first non-flag, non-assignment token is the sub-command
        if sub_idx is not None:
            _SHELL_NAMES = {"sh", "bash", "zsh", "ksh", "fish", "dash", "csh", "tcsh"}
            sub_parts = cmd_parts[sub_idx:]
            inner_cmd = Path(sub_parts[0]).name.lower()
            if inner_cmd in _SHELL_NAMES:
                return {
                    "status": "error",
                    "error": f"env with shell '{inner_cmd}' is not allowed.",
                }
            if inner_cmd in _COMMAND_DENYLIST:
                return {
                    "status": "error",
                    "error": f"env with '{inner_cmd}' is not allowed (command denylist).",
                }
            for _tok in sub_parts[1:]:
                if _is_code_exec_flag(inner_cmd, _tok):
                    return {
                        "status": "error",
                        "error": (
                            f"env with '{inner_cmd} {_tok}' is not allowed: "
                            "inline code execution flags are blocked."
                        ),
                    }
            nested_err = _check_shell_flags(sub_parts, inner_cmd)
            if nested_err is not None:
                return nested_err
    elif first_cmd == "xargs":
        # Block xargs invoking shells, denylisted binaries, or code-execution
        # interpreters (including versioned/path-qualified binaries like
        # /usr/bin/python3 or python3.11) with inline code flags (-c/-Bc/-e/-r).
        _SHELL_NAMES = {"sh", "bash", "zsh", "ksh", "fish", "dash", "csh", "tcsh"}
        for _i, _part in enumerate(cmd_parts[1:], 1):
            if _part.startswith("-"):
                continue
            _bin = Path(_part).name.lower()
            if _bin in _SHELL_NAMES:
                return {
                    "status": "error",
                    "error": f"xargs with shell '{_bin}' is not allowed.",
                }
            if _bin in _COMMAND_DENYLIST:
                return {
                    "status": "error",
                    "error": f"xargs with '{_bin}' is not allowed (command denylist).",
                }
            _remaining = cmd_parts[_i + 1 :]
            for _f in _remaining:
                if _is_code_exec_flag(_part, _f):
                    return {
                        "status": "error",
                        "error": (
                            f"xargs with '{_part}' and inline execution flag '{_f}' is not allowed: "
                            "xargs can be used to invoke code-execution interpreters and "
                            "bypass the inline-code guard."
                        ),
                    }
            nested_err = _check_shell_flags(cmd_parts[_i:], _bin)
            if nested_err is not None:
                return nested_err
            break
    elif first_cmd == "find":
        # Block find -exec / -execdir / -ok with shell or code-exec interpreters.
        _FIND_EXEC_FLAGS = {"-exec", "-execdir", "-ok"}
        _SHELL_NAMES = {"sh", "bash", "zsh", "ksh", "fish", "dash", "csh", "tcsh"}
        for _i, _part in enumerate(cmd_parts[1:], 1):
            if _part in _FIND_EXEC_FLAGS:
                # Collect the exec'd command (tokens until \; or +)
                _exec_args = []
                for _follow in cmd_parts[_i + 1 :]:
                    if _follow in (";", "+", r"\;", "\\;"):
                        break
                    _exec_args.append(_follow)
                if not _exec_args:
                    continue
                _exec_binary = Path(_exec_args[0]).name.lower()
                if _exec_binary in _SHELL_NAMES:
                    return {
                        "status": "error",
                        "error": (
                            f"find {_part} with shell '{_exec_binary}' is not allowed: "
                            "find -exec sh/bash/etc can execute arbitrary commands."
                        ),
                    }
                if _exec_binary in CODE_EXEC_INTERPRETERS:
                    _exec_flags = _exec_args[1:]
                    if any(_f in CODE_EXEC_FLAGS for _f in _exec_flags):
                        return {
                            "status": "error",
                            "error": (
                                f"find {_part} with '{_exec_binary}' and inline execution flags "
                                "is not allowed: find -exec can bypass the inline-code guard."
                            ),
                        }
                if _exec_binary in _COMMAND_DENYLIST:
                    return {
                        "status": "error",
                        "error": (
                            f"find {_part} with '{_exec_binary}' is not allowed: "
                            f"'{_exec_binary}' is in the command denylist."
                        ),
                    }
    return None


def _check_secret_path_args(
    cmd_parts: list, workdir: Path
) -> Optional[Dict[str, Any]]:
    """Block access to secret/protected files referenced in command arguments."""
    for token in cmd_parts[1:]:
        candidates: list[str] = []
        if not token.startswith("-"):
            candidates.append(token)
        elif "=" in token:
            candidates.append(token.split("=", 1)[1])
        for cand in candidates:
            if not cand:
                continue
            try:
                candidate = Path(cand)
                candidate_resolved = (
                    (workdir / candidate).resolve()
                    if not candidate.is_absolute()
                    else candidate.resolve()
                )
                if is_secret_path(candidate) or is_secret_path(candidate_resolved):
                    return {
                        "status": "error",
                        "error": f"Access to secret/protected path is blocked: {cand}",
                    }
            except Exception:
                if is_secret_path(cand):
                    return {
                        "status": "error",
                        "error": f"Access to secret/protected path is blocked: {cand}",
                    }
    return None


def _truncate_bash_output(
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> tuple[str, str, bool, bool]:
    stdout_text = (
        stdout.decode(errors="replace") if isinstance(stdout, bytes) else (stdout or "")
    )
    stderr_text = (
        stderr.decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    )

    stdout_cut = len(stdout_text) > _BASH_STDOUT_MAX
    stderr_cut = len(stderr_text) > _BASH_STDERR_MAX

    if stdout_cut:
        stdout_text = stdout_text[:_BASH_STDOUT_MAX] + "\n... [stdout truncated]"
    if stderr_cut:
        stderr_text = stderr_text[:_BASH_STDERR_MAX] + "\n... [stderr truncated]"

    return stdout_text, stderr_text, stdout_cut, stderr_cut


def _matches_restricted_command(cmd_parts: list[str], command: str) -> bool:
    """Return True when the command itself is a restricted command.

    Matching against the full command string causes false positives when a file
    path or argument merely contains a restricted word. Restriction checks should
    key off the executable and its immediate subcommand shape instead.
    """
    if not cmd_parts:
        return False

    first = cmd_parts[0].lower()
    second = cmd_parts[1].lower() if len(cmd_parts) > 1 else ""
    prefixes = {first}
    if second:
        prefixes.add(f"{first} {second}")

    return any(prefix in RESTRICTED_COMMANDS for prefix in prefixes)


def _is_os_sandbox_enforced() -> bool:
    """Return True when real OS sandboxing (bwrap or enforcing SBPL) will wrap execution.

    Returns False whenever the operator opted into unconfined execution
    (``CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1`` or ``sandbox_level='off'``)
    or when no enforcing OS backend is available on the host.
    """
    try:
        if _sandbox._allow_unenforced():
            return False
        if _sandbox.get_sandbox_level() == "off":
            return False
        return bool(_sandbox.sandbox_available())
    except Exception:
        return False


def _validate_bash_segment(
    cmd_parts: List[str],
    seg_str: str,
    workdir: Path,
    *,
    sandbox_enforced: bool,
) -> Optional[Dict[str, Any]]:
    """Validate one parsed command segment through Gates 2b–6 + network policy."""
    if not cmd_parts:
        return {"status": "error", "error": "Empty command"}

    first_cmd = cmd_parts[0].lower()
    cmd_lower = _re.sub(r"\s+", " ", seg_str).lower()

    # Gate 2a: Git subcommand allowlist (run before generic restricted-command check
    # so mutating git subcommands like `git push` return specific git-subcommand errors).
    if first_cmd == "git":
        is_safe, msg = _check_git_command_safety(cmd_parts)
        if not is_safe:
            if "Dangerous git flag" in msg:
                return {"status": "error", "error": msg}
            return {
                "status": "error",
                "error": (
                    f"{msg} "
                    f"Allowed: {sorted(GIT_SAFE_SUBCOMMANDS)}. "
                    "Write operations (commit, push, add, reset, …) are not allowed without user approval."
                ),
                "requires_approval": True,
            }

    # Gate 2b: Restricted-command check
    if _matches_restricted_command(cmd_parts, seg_str):
        allowed = any(cmd_lower.startswith(ok) for ok in RESTRICTED_ALLOWED_SUBCOMMANDS)
        if not allowed:
            return {
                "status": "error",
                "error": f"Command '{cmd_parts[0]}' requires user approval or sandboxed execution. "
                f"Restricted commands include: pip, npm install, curl, wget, apt, sudo. "
                f"Use safe alternatives or request user approval.",
                "requires_approval": True,
            }

    # Gate 3: Block inline code-execution flags (python3 -c, node -e, ruby -e, php -r).
    # TASK-23: Inside an actively enforced OS sandbox (network=False, workspace-confined,
    # non-strict mode), allow direct `python`/`python3 -c "<snippet>"` while still
    # blocking combined obfuscated flags (-Bc, -Ic, -), other interpreters (node -e,
    # ruby -e, php -r), and all env/xargs/find -exec wrappers (Gate 4 below).
    _allow_sandboxed_python_c = (
        sandbox_enforced
        and not BASH_STRICT_ALLOWLIST
        and os.path.basename(first_cmd) in ("python", "python3")
    )
    for part in cmd_parts[1:]:
        if _is_code_exec_flag(first_cmd, part):
            if _allow_sandboxed_python_c and part == "-c":
                continue
            return {
                "status": "error",
                "error": f"Command '{first_cmd} {part}' is not allowed: inline code execution flags are blocked. "
                "Run a script file instead (e.g. python3 script.py).",
            }

    # Gate 4: Archive / inplace-edit / env / xargs / find -exec check
    _flag_err = _check_shell_flags(cmd_parts, first_cmd)
    if _flag_err is not None:
        return _flag_err

    # Gate 4c: Explicit command denylist (nc, telnet, mkfs, crontab, ...)
    _deny_err = _check_command_denylist(seg_str)
    if _deny_err is not None:
        return {"status": "error", "error": _deny_err}

    # Gate 5: Tier allowlist
    if first_cmd in SAFE_COMMANDS:
        pass
    elif first_cmd == "git":
        pass
    elif BASH_STRICT_ALLOWLIST:
        return {
            "status": "error",
            "error": (
                f"Command '{cmd_parts[0]}' is not allowed in strict allowlist mode "
                f"(BASH_STRICT_ALLOWLIST=1). Only read-only inspection commands are "
                f"permitted: {sorted(SAFE_COMMANDS)}."
            ),
        }
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
    _approval_result = _bx_attr("_check_tier3_approval", _check_tier3_approval)(seg_str)
    if _approval_result is not None:
        return _approval_result

    # Gate 6: Block access to secret/protected files in arguments
    _secret_err = _check_secret_path_args(cmd_parts, workdir)
    if _secret_err is not None:
        return _secret_err

    # MC-6: per-tool network policy
    _net_policy_err = _bx_attr("_check_network_policy", _check_network_policy)(
        cmd_parts, first_cmd, seg_str
    )
    if _net_policy_err is not None:
        return _net_policy_err

    return None



def _check_tier3_approval(command: str) -> Optional[Dict[str, Any]]:
    """Check tier-3 approval gate.

    Returns approval result dict if command should be blocked, None if approved.
    """
    # HS-1: autonomous mode only suppresses the tier-3 prompt when the
    # operator explicitly allowlisted "bash" (or opted into "*").
    _bash_auto_approved = False
    try:
        from src.tools.tools_config import autonomous_approval_allowed

        _bash_auto_approved = autonomous_approval_allowed("bash")
    except Exception:
        _bash_auto_approved = False

    is_autonomous_fn = _bx_attr("_is_autonomous", _is_autonomous)
    if _is_tier3(command) and not (is_autonomous_fn() and _bash_auto_approved):
        _tool_id = str(uuid.uuid4())[:8]
        _gate_ev = _bx_attr("register_bash_gate", register_bash_gate)(_tool_id)
        try:
            _bx_attr("_get_event_bus", _get_event_bus)().publish(
                "bash.approval_required",
                {"tool_id": _tool_id, "command": command},
            )
        except Exception:
            _bx_attr("discard_bash_denied", discard_bash_denied)(_tool_id)
            _logger.warning("Failed to publish bash.approval_required event", exc_info=True)
            return {
                "status": "error",
                "error": "Bash command was denied (approval gate not available)",
                "tool_id": _tool_id,
            }

        _approved = _gate_ev.wait(timeout=120.0)
        if not _approved or _bx_attr("is_bash_denied", is_bash_denied)(_tool_id):
            _bx_attr("discard_bash_denied", discard_bash_denied)(_tool_id)
            return {
                "status": "error",
                "error": "Bash command was denied by approval gate",
                "tool_id": _tool_id,
            }
    return None


def _check_network_policy(
    cmd_parts: List[str], first_cmd: str, command: str
) -> Optional[Dict[str, Any]]:
    """MC-6: enforce the per-tool network-deny policy.

    ``bash()`` denies outbound network (``network=False``).  For commands that
    are inherently network-capable (curl, wget, pip, git remote ops, …) that
    deny is only meaningful when the sandbox backend actually enforces it —
    bwrap via ``--unshare-net``, sandbox-exec via the generated profile.  With
    sandbox level ``"off"`` (or no enforcing backend → unsandboxed fallback)
    ``run_sandboxed`` silently ignores ``network=False`` and the command would
    run with full host network.

    When the deny cannot be enforced:
      * enforcement required (autonomous mode or SANDBOX_REQUIRE_ENFORCEMENT)
        → the command is refused (fail-closed);
      * interactive mode → the command proceeds with a ``system.warning``
        event (mirrors the documented unsandboxed-fallback behaviour).

    Returns an error dict when the command must be refused, ``None`` otherwise.
    """
    if not _bx_attr("_is_network_capable", _is_network_capable)(command):
        return None
    try:
        _enforcing = (
            _sandbox.get_sandbox_level() != "off" and _sandbox.sandbox_available()
        )
    except Exception:
        _enforcing = False
    if _enforcing:
        return None
    try:
        _refuse = bool(_sandbox._enforcement_required())
    except Exception:
        _refuse = True
    if _refuse:
        return {
            "status": "error",
            "error": (
                f"Command '{first_cmd}' performs outbound network access, which the "
                "bash tool denies by default. The sandbox cannot enforce the network "
                "deny in the current configuration (sandbox level 'off' or no "
                "enforcing backend), and sandbox/network enforcement is required "
                "(SANDBOX_REQUIRE_ENFORCEMENT or autonomous mode). Refusing."
            ),
        }
    _logger.warning(
        "bash: network-capable command %r running unsandboxed — network-deny "
        "policy cannot be enforced on this host",
        command,
    )
    try:
        _bx_attr("_get_event_bus", _get_event_bus)().publish(
            "system.warning",
            {
                "message": (
                    "bash: network-deny policy not enforced for command: " + command
                )
            },
        )
    except Exception:
        pass
    return None


def _build_background_sandbox(
    cmd_parts: List[str], workdir: Path
) -> Optional[List[str]]:
    """TW-2: build a sandboxed command prefix for a background spawn.

    ``run_in_background`` spawns via ``subprocess.Popen`` and would otherwise
    bypass the sandbox entirely (no filesystem confinement, no network deny,
    no output capping).  When bwrap (or an enforcing sandbox-exec) is present
    the background command is wrapped so the same isolation applies as in the
    foreground path.

    Returns the full sandboxed argv (prefix + ``cmd_parts``) when a backend is
    available, or ``None`` when no enforcing backend exists — the caller may
    only fall back to an unsandboxed background spawn when enforcement is NOT
    required.
    """
    try:
        level = _sandbox.get_sandbox_level()
        if level == "off":
            return None
        if _sandbox._bwrap_available():
            return _sandbox._build_bwrap_args(workdir, level) + ["--"] + cmd_parts
        if _sandbox._sandbox_exec_available() and _sandbox.sandbox_exec_enforced():
            profile = _sandbox._write_sandbox_exc_profile(workdir, level)
            return [str(_sandbox._SANDBOX_EXEC_PATH), "-f", profile] + cmd_parts
    except Exception:
        _logger.warning(
            "sandbox: failed to build background sandbox wrapper", exc_info=True
        )
    return None
