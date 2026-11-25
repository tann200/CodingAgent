"""Nonce-fenced untrusted content envelopes (TASK-31).

Wraps untrusted external or project-controlled text (web pages, MCP tool
results, project instruction files, project skills, compaction summaries) in
per-process random nonce tags and escapes forged closing tags (case,
whitespace, and newline variants) so untrusted payloads cannot break out of
their prompt container.
"""

from __future__ import annotations

import re
import secrets
from typing import Iterable, Mapping, Optional

PROCESS_NONCE: str = secrets.token_hex(16)

UNTRUSTED_TAGS: frozenset[str] = frozenset(
    {
        "web_content",
        "mcp_result",
        "project_instructions",
        "project_skill",
        "compaction_summary",
    }
)

_SAFE_TAG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_BASE_CLOSER_RE = re.compile(
    r"</(\s*(?:" + "|".join(sorted(UNTRUSTED_TAGS)) + r")\b[^>]*>)",
    re.IGNORECASE,
)


def _escape_attr_value(val: str) -> str:
    return (
        str(val)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def escape_envelope_closers(
    content: str,
    extra_tags: Iterable[str] = (),
) -> str:
    """Neutralize closing tags matching known untrusted envelopes (plus *extra_tags*).

    Handles case variants (``</WEB_CONTENT>``), inner whitespace/newlines
    (``</ web_content\\n>``), and forged nonce attributes
    (``</web_content nonce="...">``) by escaping ``</`` to ``<\\/``.
    """
    if not isinstance(content, str) or "</" not in content:
        return content

    extras = [t.strip().lower() for t in extra_tags if isinstance(t, str) and t.strip()]
    unknown_extras = [t for t in extras if t not in UNTRUSTED_TAGS]
    if not unknown_extras:
        return _BASE_CLOSER_RE.sub(r"<\\/\1", content)

    all_tags = sorted(UNTRUSTED_TAGS.union(unknown_extras))
    pattern = re.compile(
        r"</(\s*(?:" + "|".join(re.escape(t) for t in all_tags) + r")\b[^>]*>)",
        re.IGNORECASE,
    )
    return pattern.sub(r"<\\/\1", content)


def wrap_untrusted(
    tag: str,
    content: str,
    *,
    attrs: Optional[Mapping[str, str]] = None,
    nonce: Optional[str] = None,
) -> str:
    """Wrap *content* in a nonce-fenced untrusted envelope ``<{tag} nonce="...">``.

    Any forged closing tag inside *content* is escaped before wrapping.
    """
    clean_tag = (tag or "").strip().lower()
    if not _SAFE_TAG_RE.match(clean_tag):
        raise ValueError(f"Invalid untrusted envelope tag: {tag!r}")

    active_nonce = nonce if (isinstance(nonce, str) and nonce) else PROCESS_NONCE
    escaped = escape_envelope_closers(str(content), extra_tags=(clean_tag,))

    attr_parts = [f'nonce="{_escape_attr_value(active_nonce)}"']
    if attrs:
        for k, v in attrs.items():
            key = str(k).strip()
            if key and key != "nonce" and _SAFE_TAG_RE.match(key.lower()):
                attr_parts.append(f'{key}="{_escape_attr_value(v)}"')
    open_attrs = " ".join(attr_parts)
    return f"<{clean_tag} {open_attrs}>\n{escaped}\n</{clean_tag} nonce=\"{_escape_attr_value(active_nonce)}\">"
