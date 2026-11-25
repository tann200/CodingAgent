"""Universal Secret Detection and Redaction.

Detects and redacts credentials, API keys, private keys, authentication tokens,
passwords, and database connection strings before they reach prompts, traces,
logs, session stores, events, or client responses.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional, Set, Union

REDACTED_PLACEHOLDER = "[REDACTED]"

# Specific known provider token patterns
_PROVIDER_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI & generic sk- tokens (sk-..., sk-proj-..., sk-ant-...)
    (re.compile(r"\bsk-[a-zA-Z0-9]{20,}\b"), "openai_api_key"),
    (re.compile(r"\bsk-proj-[a-zA-Z0-9_\-]{30,}\b"), "openai_proj_api_key"),
    (re.compile(r"\bsk-ant-[a-zA-Z0-9_\-]{30,}\b"), "anthropic_api_key"),
    # Google API Key
    (re.compile(r"\bAIza[0-9A-Za-z_-]{32,45}\b"), "google_api_key"),
    # GitHub Personal Access Tokens and other GitHub tokens
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}\b"), "github_token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "github_pat"),
    # AWS Access Key ID
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "aws_access_key"),
    # Slack tokens
    (re.compile(r"\bxox[baprs]-[0-9a-zA-Z]{10,48}\b"), "slack_token"),
    # Stripe keys
    (re.compile(r"\b(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}\b"), "stripe_key"),
    # Bearer tokens in headers or strings
    (re.compile(r"(?i)(\bBearer\s+)[a-zA-Z0-9_\-\.]{20,}\b"), "bearer_token"),
]

# Multi-line Private Keys (RSA, DSA, EC, OPENSSH, PGP)
_PRIVATE_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
        ),
        "private_key",
    ),
    (
        re.compile(
            r"-----BEGIN OPENSSH PRIVATE KEY-----[\s\S]*?-----END OPENSSH PRIVATE KEY-----"
        ),
        "openssh_private_key",
    ),
    (
        re.compile(
            r"-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]*?-----END PGP PRIVATE KEY BLOCK-----"
        ),
        "pgp_private_key",
    ),
]

# Database / Protocol URI with embedded credentials (e.g. postgres://user:password@host)
# Bounded scheme prefix length {0,31} prevents catastrophic backtracking (ReDoS) on large payloads.
_URI_PASSWORD_PATTERN = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^:\s@]*)?:([^@\s]+)@([^\s/]+)"
)

# Key-Value assignments in environment files, config files, JSON, YAML
# Matches: API_KEY="xyz", password: 'xyz', token = xyz
_KV_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|secret|password|passwd|pwd|token|auth[_-]?token|access[_-]?token|access[_-]?key|client[_-]?secret|private[_-]?key|signing[_-]?key|encryption[_-]?key)\b"
    r"(\s*[:=]\s*)"
    r"(['\"]?)([^\s'\"]{8,})\3"
)

# Sensitive dictionary/mapping key names whose string values should always be redacted
SENSITIVE_KEY_NAMES: Set[str] = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "private_key",
    "refresh_token",
    "id_token",
    "token",
    "authorization",
    "proxy_authorization",
    "x_api_key",
}

# Protected secret directory names
SECRET_DIR_NAMES: Set[str] = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
    ".claude",
    ".codex",
}

# Explicit doc/public allowlist that must stay readable
_SAFE_DOC_VARIANTS: Set[str] = {
    ".env.example",
    ".env.sample",
    ".env.template",
}

# Protected secret file names
SECRET_FILE_NAMES: Set[str] = {
    ".env",
    ".netrc",
    ".pgpass",
    ".pypirc",
    ".npmrc",
    ".my.cnf",
    ".git-credentials",
    ".bash_history",
    ".zsh_history",
    ".psql_history",
    ".python_history",
    ".node_repl_history",
    "fish_history",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "authorized_keys",
    "known_hosts",
    "credentials.json",
    "service-account.json",
    "client_secret.json",
    "token.json",
    "logins.json",
    "key4.db",
    "cert9.db",
}

# Protected secret file extensions
SECRET_EXTENSIONS: Set[str] = {
    ".pem",
    ".key",
    ".pkcs12",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
}

# Browser profile directory markers paired with credential stores
_BROWSER_PROFILE_MARKERS: Set[str] = {
    "google/chrome",
    "chromium",
    "firefox/profiles",
    "arc/user data",
    "brave-browser",
    "microsoft edge",
}
_BROWSER_SECRET_FILES: Set[str] = {
    "cookies",
    "login data",
    "web data",
    "local state",
    "logins.json",
    "key4.db",
    "cert9.db",
}

# Negated glob patterns appended after model globs in grep/ripgrep searches
SECRET_SEARCH_EXCLUDE_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".env.development",
    ".env.secret",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    ".netrc",
    ".pgpass",
    ".pypirc",
    ".npmrc",
    ".my.cnf",
    ".git-credentials",
    ".bash_history",
    ".zsh_history",
    ".psql_history",
    ".python_history",
    ".node_repl_history",
    "fish_history",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "*-service-account*.json",
    "firebase-adminsdk-*.json",
    "client_secret*.json",
)

SECRET_SEARCH_EXCLUDE_DIRS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
    ".claude",
    ".codex",
    ".git",
)


def is_git_internals_path(
    path: Union[str, Path, None],
    *,
    for_write: bool = False,
) -> bool:
    """Return True if *path* targets protected git internal files/directories.

    - ``for_write=True``: blocks ALL mutations under ``.git/**`` (or ``.git`` file).
    - ``for_write=False``: blocks reads of ``.git/config``, ``.git/credentials``,
      ``.git/hooks/**``, ``.git/modules/**/config``, and ``.git-credentials``.
    """
    if not path:
        return False
    p = Path(path)
    parts_low = [part.lower() for part in p.parts]
    name_low = p.name.lower()

    if name_low == ".git-credentials":
        return True

    if ".git" not in parts_low:
        return False

    git_idx = parts_low.index(".git")
    subparts = parts_low[git_idx + 1 :]
    if for_write:
        return True
    if not subparts:
        return False
    first = subparts[0]
    if first in ("config", "credentials", "hooks"):
        return True
    if first == "modules" and subparts[-1] == "config":
        return True
    if first == "worktrees" and subparts[-1] in ("config", "credentials"):
        return True
    return False


def is_secret_path(path: Union[str, Path, None]) -> bool:
    """Determine whether a given path points to a secret-bearing file or credential."""
    if not path:
        return False
    path_obj = Path(path)
    name = path_obj.name.lower()
    norm_str = str(path_obj).replace("\\", "/").lower()

    # 0. Explicit doc-variant & public-key allowlist (never secret unless inside a secret dir for non-pub)
    if name in _SAFE_DOC_VARIANTS:
        return False
    if name.endswith(".pub") and not any(
        d in {part.lower() for part in path_obj.parts}
        for d in (".aws", ".gnupg", ".kube", ".docker", ".azure")
    ):
        return False

    # 1. Exact secret filenames
    if name in SECRET_FILE_NAMES or path_obj.name in SECRET_FILE_NAMES:
        return True

    # 2. .env files (.env, .env.local, .env.production, etc., except allowlisted doc variants)
    if name.startswith(".env") and name not in _SAFE_DOC_VARIANTS:
        return True

    # 3. Secret extensions (.pem, .key, etc.)
    if path_obj.suffix.lower() in SECRET_EXTENSIONS:
        return True

    # 4. Check directory components (.ssh/, .aws/, .claude/, .codex/, etc.)
    parts = {part.lower() for part in path_obj.parts}
    if any(secret_dir in parts for secret_dir in SECRET_DIR_NAMES):
        return True

    # 5. Browser credential stores
    if name in _BROWSER_SECRET_FILES and any(m in norm_str for m in _BROWSER_PROFILE_MARKERS):
        return True

    # 6. Sensitive token/credential/key patterns
    # Extension-less id_* private keys (e.g. id_rsa, id_ed25519, id_work)
    if name.startswith("id_") and "." not in name:
        return True
    if (
        name.startswith("id_rsa")
        or name.startswith("id_ed25519")
        or "client_secret" in name
        or "service_account" in name
        or "service-account" in name
        or name.startswith("firebase-adminsdk-")
    ):
        return True

    return False


def detect_secrets(text: str) -> List[str]:
    """Return a list of detected secret types in text."""
    if not text or not isinstance(text, str):
        return []
    findings: List[str] = []
    for pattern, label in _PRIVATE_KEY_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    if "@" in text and ":" in text and _URI_PASSWORD_PATTERN.search(text):
        findings.append("uri_password")
    for pattern, label in _PROVIDER_SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    if _KV_SECRET_PATTERN.search(text):
        findings.append("kv_secret")
    return findings


def contains_secrets(text: str) -> bool:
    """Return True if any secret pattern is found in text."""
    return bool(detect_secrets(text))


def redact_secrets(text: str, replacement: str = REDACTED_PLACEHOLDER) -> str:
    """Scrub sensitive credentials, API keys, private keys, and passwords from text."""
    if not text or not isinstance(text, str):
        return text

    redacted = text

    # 1. Private keys (run before single-line regexes)
    for pattern, _ in _PRIVATE_KEY_PATTERNS:
        redacted = pattern.sub(replacement, redacted)

    # 2. URI passwords: preserve scheme, user, and host, redact password
    if "@" in redacted and ":" in redacted:
        redacted = _URI_PASSWORD_PATTERN.sub(
            lambda m: f"{m.group(1) or ''}:{replacement}@{m.group(3)}", redacted
        )

    # 3. Specific provider tokens
    for pattern, _ in _PROVIDER_SECRET_PATTERNS:
        if pattern.groups > 0:
            redacted = pattern.sub(rf"\1{replacement}", redacted)
        else:
            redacted = pattern.sub(replacement, redacted)

    # 4. Key-value assignments (API_KEY=..., password: ...)
    redacted = _KV_SECRET_PATTERN.sub(rf"\1\2\3{replacement}\3", redacted)

    return redacted


def redact_data(
    data: Any,
    replacement: str = REDACTED_PLACEHOLDER,
    visited: Optional[Set[int]] = None,
) -> Any:
    """Recursively scrub secrets from arbitrarily nested data structures.

    Handles dicts, lists, tuples, sets, and strings safely, preventing infinite
    loops with cyclic references.
    """
    if data is None or isinstance(data, (int, float, bool)):
        return data

    if isinstance(data, str):
        return redact_secrets(data, replacement=replacement)

    if visited is None:
        visited = set()

    obj_id = id(data)
    if obj_id in visited:
        return data

    if isinstance(data, dict):
        visited.add(obj_id)
        result_dict: dict[Any, Any] = {}
        for k, v in data.items():
            k_redacted = (
                redact_secrets(k, replacement=replacement)
                if isinstance(k, str)
                else k
            )
            k_norm = (
                str(k).lower().replace("-", "_").strip()
                if isinstance(k, str)
                else ""
            )
            if k_norm in SENSITIVE_KEY_NAMES and isinstance(v, str) and v.strip():
                result_dict[k_redacted] = replacement
            else:
                result_dict[k_redacted] = redact_data(
                    v, replacement=replacement, visited=visited
                )
        return result_dict

    if isinstance(data, list):
        visited.add(obj_id)
        return [
            redact_data(item, replacement=replacement, visited=visited)
            for item in data
        ]

    if isinstance(data, tuple):
        visited.add(obj_id)
        return tuple(
            redact_data(item, replacement=replacement, visited=visited)
            for item in data
        )

    if isinstance(data, set):
        visited.add(obj_id)
        return {
            redact_data(item, replacement=replacement, visited=visited)
            for item in data
        }

    return data
