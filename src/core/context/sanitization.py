from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_INVISIBLE_UNICODE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_SPOOFED_XML_TAGS_RE = re.compile(
    r"<(/?)(task|system|available_tools|repository_intelligence|identity|role|"
    r"active_skills|user_preferences|past_mistakes|session_summary|task_progress|"
    r"context|thinking_guidance|thinking_mode|output_format)(\b[^>]*)>",
    re.IGNORECASE,
)


def sanitize_prompt_text(text: str, *, get_context_dir_name: Callable[[], str]) -> str:
    """Sanitize text to reduce prompt-injection risk without stripping code fences."""
    if not text:
        return text

    # Strip zero-width and bidi override invisible Unicode characters used to hide instructions
    cleaned_unicode = _INVISIBLE_UNICODE_RE.sub("", text)
    removed_any = cleaned_unicode != text
    # Neutralize XML structural tag spoofing (e.g., </task>, </system>, </available_tools>)
    cleaned_tags = _SPOOFED_XML_TAGS_RE.sub(r"&lt;\1\2\3&gt;", cleaned_unicode)
    if cleaned_tags != cleaned_unicode:
        removed_any = True
    text = cleaned_tags

    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        lowered = line.strip().lower()
        if (
            "ignore all instructions" in lowered
            or "do not follow" in lowered
            or "disregard previous" in lowered
            or "forget all previous" in lowered
        ):
            removed_any = True
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    collapsed = []
    comment_block = []
    for line in text.splitlines():
        if line.strip().startswith("#") or line.strip().startswith("//"):
            comment_block.append(line)
            continue

        if len(comment_block) > 20:
            collapsed.extend(comment_block[:3])
            collapsed.append(f"[COMMENT BLOCK TRUNCATED - {len(comment_block)} lines]")
            collapsed.extend(comment_block[-3:])
            removed_any = True
        else:
            collapsed.extend(comment_block)
        comment_block = []
        collapsed.append(line)

    if comment_block:
        if len(comment_block) > 20:
            collapsed.extend(comment_block[:3])
            collapsed.append(f"[COMMENT BLOCK TRUNCATED - {len(comment_block)} lines]")
            collapsed.extend(comment_block[-3:])
            removed_any = True
        else:
            collapsed.extend(comment_block)

    sanitized = "\n".join(collapsed)

    if removed_any:
        try:
            ctx_dir = Path.cwd() / get_context_dir_name()
            if ctx_dir.exists():
                log_path = ctx_dir / "context_sanitization.log"
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write("SANITIZE: removed suspicious content\n")
        except Exception as exc:
            logger.debug("sanitization: failed to write sanitization log: %s", exc)

    return sanitized
