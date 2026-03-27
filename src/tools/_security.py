"""Shell command security constants for the bash() tool.

Extracted from file_tools.bash() so they can be imported, tested, and
extended independently of the full file_tools dependency tree.

External projects can add entries to these collections before calling
``build_registry()`` to customise what the bash tool allows or blocks.

Example::

    from src.tools._security import SAFE_COMMANDS, DANGEROUS_PATTERNS
    SAFE_COMMANDS.add("my-read-only-cli")
    DANGEROUS_PATTERNS.append("drop table")   # domain-specific block
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tier 0 — Always-blocked shell constructs and destructive patterns
# Checked on whitespace-normalised, lower-cased command text so
# spacing tricks (e.g. "r m  -rf") cannot bypass the check.
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS: list[str] = [
    "&&",
    "||",
    ";",
    "|",
    ">",
    ">>",
    "<",
    "$(",
    "`",
    "rm -rf",
    "rm -r",
    "rm -f",
    "del ",
    "format ",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "git push",
]

# ---------------------------------------------------------------------------
# Tier 1 — Safe read-only / inspection commands (auto-allowed, no approval)
# ---------------------------------------------------------------------------

SAFE_COMMANDS: set[str] = {
    "ls",
    "cat",
    "grep",
    "find",
    "git",
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
    "python", "python3", "pytest", "py.test", "tox", "nox",
    "ruff", "mypy", "pyright", "uv", "poetry", "pdm", "hatch",
    # Node / npm
    "npm", "npx", "node", "yarn", "pnpm",
    # TypeScript
    "tsc",
    # JS/TS test runners
    "jest", "vitest", "mocha", "jasmine",
    # JS linters / formatters
    "eslint", "prettier", "biome",
    # Rust
    "cargo", "rustc",
    # Java
    "javac", "java", "jar", "mvn", "gradle",
    # Go
    "go", "gofmt", "golint", "staticcheck",
    # C/C++
    "gcc", "g++", "clang", "clang++", "make", "cmake", "ninja",
    # Ruby
    "bundle", "rake", "rspec", "ruby",
    # PHP
    "composer", "php",
    # Swift
    "swift", "swiftc",
    # .NET
    "dotnet",
}

# ---------------------------------------------------------------------------
# Tier 3 — Restricted commands (return requires_approval=True)
# ---------------------------------------------------------------------------

RESTRICTED_COMMANDS: set[str] = {
    "pip", "pip3", "pip install",
    "curl", "wget",
    "npm install", "npm i",
    "cargo install",
    "go install", "go get",
    "apt", "apt-get", "yum", "dnf", "brew",
    "sudo", "su",
    "chmod", "chown",
    "rm", "del",
}

# Tier-3 sub-commands that are actually safe (allowlist exceptions)
RESTRICTED_ALLOWED_SUBCOMMANDS: list[str] = [
    "npm test", "npm run", "npm start", "npm build", "npm lint",
    "cargo test", "cargo build", "cargo check",
    "go test", "go build", "go vet",
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
    "-x", "--extract", "-xf", "-xvf", "-xzf", "-xjf", "-xJf",
}

# TS-2 fix: Archive creation flags — tar -c / tar -r create or append to archives.
# SAFE_COMMANDS documents tar as "read-only / inspection" but these flags write.
# Block them to align implementation with documented semantics.
TAR_CREATE_FLAGS: set[str] = {
    "-c", "--create", "-r", "--append", "-u", "--update",
    "-cf", "-czf", "-cjf", "-cJf", "-cvf", "-cvzf",
}
