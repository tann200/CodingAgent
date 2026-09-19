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


# ---------------------------------------------------------------------------
# MC-6: network-capable commands (per-tool network policy)
# ---------------------------------------------------------------------------

# Commands/invocations that inherently perform outbound network I/O.  The bash
# tools deny network by default (``network=False``), so these must never run
# with full host network when the sandbox cannot enforce the deny and
# enforcement is required (autonomous / SANDBOX_REQUIRE_ENFORCEMENT).
NETWORK_CAPABLE_COMMANDS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "pip",
        "pip3",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "brew",
        "rsync",
        "scp",
        "sftp",
        "nc",
        "telnet",
        "ssh",
    }
)

# Multi-token subcommands that perform outbound network I/O.
NETWORK_CAPABLE_SUBCOMMANDS: tuple[str, ...] = (
    "git clone",
    "git fetch",
    "git pull",
    "git push",
    "git ls-remote",
    "npm install",
    "npm i",
    "npm add",
    "npm publish",
    "yarn add",
    "pnpm add",
    "cargo install",
    "cargo add",
    "cargo search",
    "cargo publish",
    "go get",
    "go install",
    "go mod download",
)


def is_network_capable(command: str) -> bool:
    """Return True if *command* performs outbound network I/O.

    Uses exact first-token matching (preventing bypass via names like
    ``curl-tool``) and prefix matching for known multi-token subcommands
    (e.g. ``npm install``).  Local commands (``git status``, ``npm test``,
    ``ls``) are not network-capable.
    """
    import shlex as _shlex

    stripped = command.lstrip()
    try:
        tokens = _shlex.split(stripped)
    except ValueError:
        return False
    if not tokens:
        return False
    first = tokens[0].lower()
    if first in NETWORK_CAPABLE_COMMANDS:
        return True
    normalised = " ".join(tokens[:3]).lower()
    return any(normalised.startswith(sub) for sub in NETWORK_CAPABLE_SUBCOMMANDS)
