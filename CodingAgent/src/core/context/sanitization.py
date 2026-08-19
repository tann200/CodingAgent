from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def sanitize_prompt_text(text: str, *, get_context_dir_name: Callable[[], str]) -> str:
    """Sanitize text to reduce prompt-injection risk without stripping code fences."""
    if not text:
        return text

    lines = text.splitlines()
    cleaned_lines = []
    removed_any = False
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
