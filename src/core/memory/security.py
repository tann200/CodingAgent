"""Memory security: injection and exfiltration pattern scanning.

This module provides security checks for content that gets injected into
the system prompt via memory systems. It prevents prompt injection attacks
and data exfiltration attempts.
"""

# ruff: noqa: E501

import re
from typing import Optional

# Threat patterns that indicate prompt injection or exfiltration attempts
_MEMORY_THREAT_PATTERNS = [
    # Prompt injection patterns (case-insensitive, SPECIFIC matches only)
    (r"ignore\s+all\s+previous\s+instructions", "prompt_injection"),
    (r"ignore\s+(all\s+)?my\s+instructions", "prompt_injection"),
    (r"disregard\s+(all|your)\s+.*(instructions|rules)", "disregard_rules"),
    (r"forget\s+(all|your|the)\s+.*(instructions|rules)", "forget_instructions"),
    (r"you\s+are\s+now\s+(a|an)\s*\w*\s*(assistant|AI|bot|agent)", "role_hijack"),
    (
        r"you\s+must\s+now\s+(be\s+)?(a|an)\s*\w*\s*(assistant|AI|bot|agent)",
        "role_hijack",
    ),
    (r"do\s+not\s+(tell|inform)\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"act\s+as\s+(if|though)\s+you\s+(have|don.?t\s+have)", "bypass_restrictions"),
    (r"new\s+instructions", "new_instructions"),
    (r"override\s+(your\s+)?(instructions|system|prompt)", "override_prompt"),
    (r"ignore\s+all\s+rules", "ignore_rules"),
    (r"just\s+(do|follow)\s+what\s+i\s+say", "ignore_rules"),
    # Exfiltration patterns - curl/wget with secrets
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"requests?\.get\s*\([^)]*\$\w*(KEY|TOKEN|SECRET|PASSWORD|API)", "exfil_python"),
    (r"fetch\s*\([^)]*\$\w*(KEY|TOKEN|SECRET|PASSWORD|API)", "exfil_fetch"),
    # File-based exfiltration
    (r"cat\s+.*(\.env|credentials|\.netrc|\.pgpass)", "read_secrets"),
    (r"open\s*\([^)]*(\.env|credentials|secrets?)", "read_secrets_python"),
    # SSH/backdoor patterns
    (r"authorized_keys", "ssh_backdoor"),
    (r"\.ssh/", "ssh_access"),
    # Command execution with secrets
    (r"exec\s*\(.*\$", "exec_injection"),
    (r"eval\s*\(.*\$", "eval_injection"),
]

# Invisible Unicode characters that could be used for injection
_INVISIBLE_CHARS = {
    "\u200b",  # Zero-width space
    "\u200c",  # Zero-width non-joiner
    "\u200d",  # Zero-width joiner
    "\u2060",  # Word joiner
    "\ufeff",  # Byte order mark
    "\u202a",  # Left-to-right embedding
    "\u202b",  # Right-to-left embedding
    "\u202c",  # Pop directional formatting
    "\u202d",  # Left-to-right override
    "\u202e",  # Right-to-left override
}


def scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfiltration patterns.

    This function checks for:
    1. Invisible Unicode characters that could hide malicious content
    2. Prompt injection patterns that try to override instructions
    3. Exfiltration patterns that try to leak secrets via curl/wget/etc.

    Args:
        content: The content to scan

    Returns:
        Error string if blocked, None if content is safe
    """
    if not content:
        return None

    # Check for invisible Unicode characters
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X} (possible injection)"

    # Check for threat patterns
    for pattern, pattern_id in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pattern_id}'. Memory entries are injected into the system prompt and must not contain injection or exfiltration payloads."

    return None


def strip_invisible_chars(content: str) -> str:
    """Remove invisible Unicode characters from content.

    Args:
        content: The content to clean

    Returns:
        Content with invisible characters removed
    """
    for char in _INVISIBLE_CHARS:
        content = content.replace(char, "")
    return content


def is_memory_content_safe(content: str) -> bool:
    """Check if memory content is safe (no threats detected).

    Args:
        content: The content to check

    Returns:
        True if safe, False otherwise
    """
    return scan_memory_content(content) is None
