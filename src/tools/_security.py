"""Shell command security constants for the bash() tool.

Extracted from file_tools.bash() so they can be imported, tested, and
extended independently of the full file_tools dependency tree.

External projects can add entries to these collections before calling
``build_registry()`` to customise what the bash tool allows or blocks.

Example::

    from src.tools._security import SAFE_COMMANDS, add_dangerous_pattern
    SAFE_COMMANDS.add("my-read-only-cli")
    add_dangerous_pattern("drop table")   # domain-specific block
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# P2-2: Strict allowlist mode — BASH_STRICT_ALLOWLIST=1
# When enabled, only commands in SAFE_COMMANDS are auto-allowed.
# TEST_COMPILE_COMMANDS (compilers, test runners) are also blocked, giving a
# read-only-inspection-only shell for maximum safety during autonomous runs.
# Set BASH_STRICT_ALLOWLIST=1 (or "true" / "yes") in the environment to enable.
# ---------------------------------------------------------------------------
_STRICT_ALLOWLIST_ENV = os.environ.get("BASH_STRICT_ALLOWLIST", "0").strip().lower()
BASH_STRICT_ALLOWLIST: bool = _STRICT_ALLOWLIST_ENV in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Tier 0 — Always-blocked shell constructs and destructive patterns
# Checked on whitespace-normalised, lower-cased command text so
# spacing tricks (e.g. "r m  -rf") cannot bypass the check.
# ---------------------------------------------------------------------------

# Immutable base — never modified at runtime; extend via add_dangerous_pattern().
_BASE_DANGEROUS_PATTERNS: tuple[str, ...] = (
    "&&",
    "||",
    ";",
    " | ",  # SEC-3: space-padded to avoid false positives on | inside quoted grep patterns
    ">",
    ">>",
    "<",
    "$(",
    "`",
    "rm -rf",
    "rm -r",
    "rm -f",
    # Absolute-path rm variants (bypass the basename check) - Unix
    "/bin/rm",
    "/usr/bin/rm",
    "\\rm",
    # Flag-interleaved rm variants
    "rm -v -r",
    "rm -v -f",
    "rm --recursive",
    "rm --force",
    # Windows dangerous commands
    "del ",
    "rmdir ",
    "format ",
    " attrib ",
    # Cross-platform shutdown
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "git push",
    # Windows PowerShell dangerous commands
    "remove-item",
    "clear-content",
    "stop-process",
    "stop-computer",
    # Linux-specific paths that could be dangerous if executed
    "etc/passwd",
    "etc/shadow",
    "/dev/sda",
    "/dev/nvme",
)

# Mutable extension list — populated by add_dangerous_pattern() at startup or
# by external callers that need domain-specific blocking.
_EXTRA_PATTERNS: list[str] = []


def add_dangerous_pattern(pattern: str) -> None:
    """Append *pattern* to the runtime extension list.

    Both ``_EXTRA_PATTERNS`` and the public ``DANGEROUS_PATTERNS`` list are
    updated so that additions are visible immediately to all callers.

    Parameters
    ----------
    pattern:
        A lower-case substring to block.  The check is performed on the
        whitespace-normalised, lower-cased command string, so the pattern
        should also be lower-case.
    """
    if pattern not in _EXTRA_PATTERNS:
        _EXTRA_PATTERNS.append(pattern)
        DANGEROUS_PATTERNS.append(pattern)


# ---------------------------------------------------------------------------
# Public name: DANGEROUS_PATTERNS
# Kept as a list for backwards-compatibility with callers that iterate or
# do ``pattern in DANGEROUS_PATTERNS`` checks.  The list is rebuilt from
# the immutable base + mutable extensions each time this module is imported;
# use add_dangerous_pattern() to extend it at runtime.
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS: list[str] = list(_BASE_DANGEROUS_PATTERNS) + _EXTRA_PATTERNS

# ---------------------------------------------------------------------------
# Tier 1 — Safe read-only / inspection commands (auto-allowed, no approval)
# ---------------------------------------------------------------------------

SAFE_COMMANDS: set[str] = {
    "ls",
    "cat",
    "grep",
    "find",
    # NOTE: 'git' is intentionally NOT in SAFE_COMMANDS — use GIT_SAFE_SUBCOMMANDS
    # allowlist instead so only read-only git operations pass automatically.
    "head",
    "tail",
    "wc",
    "pwd",
    "echo",
    "date",
    "which",
    "env",
    "tree",
    "sort",
    "uniq",
    "awk",
    "sed",
    "diff",
    "stat",
    "file",
    "du",
    "df",
    "id",
    "whoami",
    "hostname",
    # process / system info
    "ps",
    "pgrep",
    "lsof",
    "uname",
    "uptime",
    "free",
    "top",
    "htop",
    # binary / object-file inspection
    "nm",
    "objdump",
    "readelf",
    "ldd",
    "strings",
    # macOS-specific
    "sw_vers",
    "defaults",
    "system_profiler",
    "otool",
    "codesign",
    "xcode-select",
    "plutil",
    "pbpaste",
    # path / archive inspection
    "realpath",
    "basename",
    "dirname",
    "readlink",
    "tar",
    "zip",
    "unzip",
    "type",
    "md5sum",
    "sha256sum",
    "md5",
    "shasum",
    "xxd",
    "less",
    "more",
    "column",
    "cut",
    "tr",
    "xargs",
    "test",
    "[",
    "true",
    "false",
    # NOTE: 'touch' intentionally omitted — creates files, bypasses WorkspaceGuard
}

# ---------------------------------------------------------------------------
# Tier 2 — Test runners and compilers (auto-allowed; needed for verification)
# ---------------------------------------------------------------------------

TEST_COMPILE_COMMANDS: set[str] = {
    # Python
    "python",
    "python3",
    "pytest",
    "py.test",
    "tox",
    "nox",
    "ruff",
    "mypy",
    "pyright",
    "uv",
    "poetry",
    "pdm",
    "hatch",
    # Node / npm
    "npm",
    "npx",
    "node",
    "yarn",
    "pnpm",
    # TypeScript
    "tsc",
    # JS/TS test runners
    "jest",
    "vitest",
    "mocha",
    "jasmine",
    # JS linters / formatters
    "eslint",
    "prettier",
    "biome",
    # Rust
    "cargo",
    "rustc",
    # Java
    "javac",
    "java",
    "jar",
    "mvn",
    "gradle",
    # Go
    "go",
    "gofmt",
    "golint",
    "staticcheck",
    # C/C++
    "gcc",
    "g++",
    "clang",
    "clang++",
    "make",
    "cmake",
    "ninja",
    # Ruby
    "bundle",
    "rake",
    "rspec",
    "ruby",
    # PHP
    "composer",
    "php",
    # Swift
    "swift",
    "swiftc",
    # .NET
    "dotnet",
}

# ---------------------------------------------------------------------------
# Tier 3 — Restricted commands (return requires_approval=True)
# ---------------------------------------------------------------------------

RESTRICTED_COMMANDS: set[str] = {
    "pip",
    "pip3",
    "pip install",
    "curl",
    "wget",
    "npm install",
    "npm i",
    "cargo install",
    "go install",
    "go get",
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
    # Network / destructive git operations
    "git clone",
    "git push",
    "git fetch",
}

# Tier-3 sub-commands that are actually safe (allowlist exceptions)
RESTRICTED_ALLOWED_SUBCOMMANDS: list[str] = [
    "npm test",
    "npm run",
    "npm start",
    "npm build",
    "npm lint",
    "cargo test",
    "cargo build",
    "cargo check",
    "go test",
    "go build",
    "go vet",
]

# ---------------------------------------------------------------------------
# Interpreter inline-execution flags — always blocked
# Prevents: python3 -c "import os; os.system(...)"
# ---------------------------------------------------------------------------

CODE_EXEC_INTERPRETERS: set[str] = {"python", "python3", "node", "ruby", "php"}
CODE_EXEC_FLAGS: set[str] = {"-c", "-e", "-r", "--eval", "--execute"}

# ---------------------------------------------------------------------------
# Archive extract flags — only listing is allowed, not extraction
# ---------------------------------------------------------------------------

TAR_EXTRACT_FLAGS: set[str] = {
    "-x",
    "--extract",
    "-xf",
    "-xvf",
    "-xzf",
    "-xjf",
    "-xJf",
}

# TS-2 fix: Archive creation flags — tar -c / tar -r create or append to archives.
# SAFE_COMMANDS documents tar as "read-only / inspection" but these flags write.
# Block them to align implementation with documented semantics.
TAR_CREATE_FLAGS: set[str] = {
    "-c",
    "--create",
    "-r",
    "--append",
    "-u",
    "--update",
    "-cf",
    "-czf",
    "-cjf",
    "-cJf",
    "-cvf",
    "-cvzf",
}

# ---------------------------------------------------------------------------
# sed write flags — sed -i (in-place edit) modifies files; block in read-only
# bash mode.
# ---------------------------------------------------------------------------

SED_WRITE_FLAGS: set[str] = {
    "-i",
    "--in-place",
}

# ---------------------------------------------------------------------------
# Git subcommand allowlist — read-only operations auto-allowed without approval.
# Any other git subcommand (e.g. commit, push, rm, reset) is NOT auto-allowed.
# ---------------------------------------------------------------------------

GIT_SAFE_SUBCOMMANDS: set[str] = {
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
    "branch",
    "tag",
    "stash list",
    "remote -v",
    "remote",
    "config --list",
    "config --get",
    "help",
    "version",
}
