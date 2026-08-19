"""
_approval.py — shared constants for bash/tool tier-3 approval gates.

Imported by ``bash()`` in ``file_tools.py`` and ``execute_tool()`` in
``orchestrator.py`` so the gate logic stays DRY across both call sites.
"""

from __future__ import annotations

# Commands whose first token requires explicit user approval (TUI-03).
# Uses exact first-token matching to prevent bypass via names like "curl-tool".
TIER3_COMMANDS: frozenset[str] = frozenset(
    {
        "pip",
        "pip3",
        "curl",
        "wget",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "brew",
        "sudo",
        "su",
        "chmod",
        "chown",
        "rm",
        "del",
    }
)

# Multi-token subcommands that also require approval.
TIER3_SUBCOMMANDS: tuple[str, ...] = (
    "npm install",
    "npm i",
    "cargo install",
    "go install",
    "go get",
    "git clone",
    "git push",
    "git fetch",
)

# Keep TIER3_PREFIXES as an alias for backwards compatibility with any code
# that imports it directly.
TIER3_PREFIXES: tuple[str, ...] = (
    tuple(t + " " for t in TIER3_COMMANDS) + TIER3_SUBCOMMANDS
)


def is_tier3(command: str) -> bool:
    """Return True if *command* requires tier-3 user approval.

    Uses exact first-token matching for single-word commands (preventing
    bypass via names like ``curl-tool``) and prefix matching for known
    multi-token subcommands (e.g. ``npm install``).
    """
    import shlex as _shlex

    stripped = command.lstrip()
    try:
        tokens = _shlex.split(stripped)
    except ValueError:
        # Malformed command — treat as requiring approval to be safe.
        return True
    if not tokens:
        return False
    first = tokens[0].lower()
    if first in TIER3_COMMANDS:
        return True
    # Multi-token checks: "npm install", "go get", "git push", etc.
    normalised = " ".join(tokens[:3]).lower()
    return any(normalised.startswith(sub) for sub in TIER3_SUBCOMMANDS)
