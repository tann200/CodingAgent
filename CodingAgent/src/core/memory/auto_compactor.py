"""Deterministic auto-compaction engine — CP-6.

Python port of claw-code's ``compact.rs``.  Unlike the LLM-based
``distiller.py``, this module produces a structured summary using only
character-count heuristics and text extraction — no LLM call required.

Design notes:
- Token estimation: ``len(text) // 4 + 1`` per content chunk (matches Rust).
- ``should_compact()`` examines only the *compactable portion* — the slice
  after any existing compacted-summary prefix — so repeated compactions merge
  cleanly rather than doubling the summary.
- ``compact_messages()`` is deterministic and synchronous; safe to call from
  any async context without worrying about event-loop nesting.
- Key file detection extends claw-code's ``[rs, ts, tsx, js, json, md]`` list
  with Python-centric extensions so Python projects benefit equally.
"""

# ruff: noqa: E501

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# String constants — must match across compact / detect / merge helpers
# ---------------------------------------------------------------------------

COMPACT_CONTINUATION_PREAMBLE: str = (
    "This session is being continued from a previous conversation that ran out of "
    "context. The summary below covers the earlier portion of the conversation.\n\n"
)
COMPACT_RECENT_MESSAGES_NOTE: str = "Recent messages are preserved verbatim."
COMPACT_DIRECT_RESUME_INSTRUCTION: str = (
    "Continue the conversation from where it left off without asking the user any "
    "further questions. Resume directly — do not acknowledge the summary, do not "
    "recap what was happening, and do not preface with continuation text."
)

# File extensions considered "interesting" for key-file extraction.
# Extends claw-code's {rs, ts, tsx, js, json, md} with Python / config types.
_INTERESTING_EXTENSIONS: frozenset[str] = frozenset(
    {
        "rs",
        "ts",
        "tsx",
        "js",
        "mjs",
        "cjs",
        "json",
        "jsonc",
        "md",
        "mdx",
        "py",
        "pyi",
        "yaml",
        "yml",
        "toml",
        "cfg",
        "ini",
        "sh",
        "bash",
        "tf",
        "hcl",
        "sql",
    }
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactConfig:
    """Mirrors ``CompactionConfig`` in compact.rs."""

    preserve_recent: int = 4
    """Number of most-recent messages to keep verbatim after compaction."""

    max_tokens: int = 10_000
    """Token threshold above which the compactable portion triggers compaction."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CompactResult:
    """Mirrors ``CompactionResult`` in compact.rs."""

    summary: str
    """Raw ``<summary>…</summary>`` block."""

    formatted_summary: str
    """Human-readable version (``<summary>`` tags stripped, ``Summary:`` prefix)."""

    compacted_messages: list[dict]
    """Replacement message list: [system-summary-msg] + preserved recent msgs."""

    removed_message_count: int
    """Number of messages that were replaced by the summary."""


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_message_tokens(message: dict) -> int:
    """Char-based token estimate for a single message dict.

    Matches the Rust formula: ``len / 4 + 1`` per content block.
    A Python message dict has a single ``"content"`` string so we treat that
    as one block.
    """
    content = message.get("content") or ""
    if isinstance(content, list):
        # OpenAI-style multipart content blocks
        total = 0
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                total += len(str(text)) // 4 + 1
            else:
                total += len(str(block)) // 4 + 1
        return total
    return len(str(content)) // 4 + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Sum token estimates across a list of message dicts."""
    return sum(estimate_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# Compactable-prefix detection
# ---------------------------------------------------------------------------


def _extract_existing_compacted_summary(message: dict) -> Optional[str]:
    """Return the raw summary text if *message* is an existing compact prefix.

    Mirrors ``extract_existing_compacted_summary`` in compact.rs.
    A compact prefix is a ``system`` role message whose content starts with
    ``COMPACT_CONTINUATION_PREAMBLE``.  The preamble, note, and resume
    instruction are stripped — only the bare ``<summary>…</summary>`` block
    (or plain text for older formats) is returned.
    """
    if message.get("role") != "system":
        return None
    content = message.get("content") or ""
    if isinstance(content, list):
        # Extract first text block from multipart content
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                content = block.get("text", "")
                break
        else:
            return None
    if not content.startswith(COMPACT_CONTINUATION_PREAMBLE):
        return None
    summary = content[len(COMPACT_CONTINUATION_PREAMBLE) :]
    # Strip "Recent messages are preserved verbatim." note
    note_sep = f"\n\n{COMPACT_RECENT_MESSAGES_NOTE}"
    if note_sep in summary:
        summary = summary.split(note_sep, 1)[0]
    # Strip direct resume instruction
    instr_sep = f"\n{COMPACT_DIRECT_RESUME_INSTRUCTION}"
    if instr_sep in summary:
        summary = summary.split(instr_sep, 1)[0]
    return summary.strip()


def _compacted_prefix_len(messages: list[dict]) -> int:
    """Return 1 if the first message is an existing compact summary, else 0."""
    if messages and _extract_existing_compacted_summary(messages[0]) is not None:
        return 1
    return 0


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


def should_compact(messages: list[dict], config: AutoCompactConfig) -> bool:
    """Return True when the compactable portion exceeds the token threshold.

    The *compactable portion* excludes any existing compact-summary prefix so
    that re-compaction only counts new messages rather than the already-
    summarised history.

    Mirrors ``should_compact`` in compact.rs.
    """
    start = _compacted_prefix_len(messages)
    compactable = messages[start:]
    if len(compactable) <= config.preserve_recent:
        return False
    total_tokens = estimate_messages_tokens(compactable)
    return total_tokens >= config.max_tokens


# ---------------------------------------------------------------------------
# Summary building helpers
# ---------------------------------------------------------------------------


def _truncate_summary(text: str, max_chars: int = 160) -> str:
    """Truncate *text* to *max_chars*, appending '…' if truncated."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _first_text_content(message: dict) -> Optional[str]:
    """Return the first non-empty text content string from a message dict."""
    content = message.get("content")
    if content is None:
        return None
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if text.strip():
                    return text.strip()
            elif isinstance(block, str) and block.strip():
                return block.strip()
    return None


_INTERESTING_EXT_RE = re.compile(
    r"\.(" + "|".join(re.escape(e) for e in _INTERESTING_EXTENSIONS) + r")\b",
    re.IGNORECASE,
)


def _extract_file_candidates(text: str) -> list[str]:
    """Extract path-like tokens that contain '/' and have an interesting extension.

    Mirrors ``extract_file_candidates`` in compact.rs.
    """
    candidates: list[str] = []
    for token in text.split():
        candidate = token.strip(",.;:)(\"'`")
        if "/" in candidate and _INTERESTING_EXT_RE.search(candidate):
            candidates.append(candidate)
    return candidates


def _collect_key_files(messages: list[dict]) -> list[str]:
    """Collect unique file paths referenced across all messages (up to 8).

    Mirrors ``collect_key_files`` in compact.rs.
    """
    seen: set[str] = set()
    files: list[str] = []
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text") or block.get("content") or "")
            content = " ".join(str(p) for p in parts)
        for f in _extract_file_candidates(str(content)):
            if f not in seen:
                seen.add(f)
                files.append(f)
        if len(files) >= 8:
            break
    return files[:8]


_PENDING_KEYWORDS = ("todo", "next", "pending", "follow up", "remaining")


def _infer_pending_work(messages: list[dict]) -> list[str]:
    """Find messages that mention pending/next/todo items (up to 3, reversed then re-reversed).

    Mirrors ``infer_pending_work`` in compact.rs.
    """
    results: list[str] = []
    for msg in reversed(messages):
        text = _first_text_content(msg)
        if text and any(kw in text.lower() for kw in _PENDING_KEYWORDS):
            results.append(_truncate_summary(text))
        if len(results) >= 3:
            break
    return list(reversed(results))


def _collect_recent_user_requests(messages: list[dict], limit: int = 3) -> list[str]:
    """Collect the last *limit* user-role text messages, oldest-first.

    Mirrors ``collect_recent_role_summaries(…, User, 3)`` in compact.rs.
    """
    results: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = _first_text_content(msg)
            if text:
                results.append(_truncate_summary(text))
        if len(results) >= limit:
            break
    return list(reversed(results))


def _summarize_messages(messages: list[dict]) -> str:
    """Build a structured ``<summary>…</summary>`` block for *messages*.

    Mirrors ``summarize_messages`` in compact.rs — seven sections:
    1. Scope (message counts)
    2. Tools mentioned
    3. Recent user requests
    4. Pending work
    5. Key files
    6. Current work
    7. Key timeline
    """
    user_count = sum(1 for m in messages if m.get("role") == "user")
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
    tool_count = sum(1 for m in messages if m.get("role") == "tool")

    # Collect tool names from JSON tool-call patterns in content
    tool_name_set: set[str] = set()
    _tool_name_re = re.compile(r'(?:name|tool_name|tool):\s*["\']?([A-Za-z_]\w*)["\']?')
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text") or b.get("content") or "")
                if isinstance(b, dict)
                else str(b)
                for b in content
            )
        for m in _tool_name_re.finditer(str(content)):
            tool_name_set.add(m.group(1))

    lines = [
        "<summary>",
        "Conversation summary:",
        f"- Scope: {len(messages)} earlier messages compacted "
        f"(user={user_count}, assistant={assistant_count}, tool={tool_count}).",
    ]

    if tool_name_set:
        sorted_tools = sorted(tool_name_set)
        lines.append(f"- Tools mentioned: {', '.join(sorted_tools)}.")

    recent_requests = _collect_recent_user_requests(messages)
    if recent_requests:
        lines.append("- Recent user requests:")
        lines.extend(f"  - {r}" for r in recent_requests)

    pending = _infer_pending_work(messages)
    if pending:
        lines.append("- Pending work:")
        lines.extend(f"  - {p}" for p in pending)

    key_files = _collect_key_files(messages)
    if key_files:
        lines.append(f"- Key files referenced: {', '.join(key_files)}.")

    # Current work: last non-empty text content from any message
    current_work: Optional[str] = None
    for msg in reversed(messages):
        text = _first_text_content(msg)
        if text:
            current_work = _truncate_summary(text, 200)
            break
    if current_work:
        lines.append(f"- Current work: {current_work}")

    # Key timeline: every message, content truncated to 160 chars
    lines.append("- Key timeline:")
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " | ".join(
                str(b.get("text") or b.get("content") or "")
                if isinstance(b, dict)
                else str(b)
                for b in content
            )
        lines.append(f"  - {role}: {_truncate_summary(str(content))}")

    lines.append("</summary>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------


def _strip_tag_block(content: str, tag: str) -> str:
    """Remove the first ``<tag>…</tag>`` block from *content*."""
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)
    if start_idx == -1 or end_idx == -1:
        return content
    return content[:start_idx] + content[end_idx + len(end_tag) :]


def _extract_tag_block(content: str, tag: str) -> Optional[str]:
    """Return the text inside the first ``<tag>…</tag>`` block, or None."""
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start_idx = content.find(start_tag)
    if start_idx == -1:
        return None
    start_idx += len(start_tag)
    end_idx = content.find(end_tag, start_idx)
    if end_idx == -1:
        return None
    return content[start_idx:end_idx]


def _collapse_blank_lines(text: str) -> str:
    """Collapse consecutive blank lines to a single blank line."""
    result_lines: list[str] = []
    last_blank = False
    for line in text.splitlines():
        is_blank = not line.strip()
        if is_blank and last_blank:
            continue
        result_lines.append(line)
        last_blank = is_blank
    return "\n".join(result_lines)


def format_compact_summary(summary: str) -> str:
    """Convert raw ``<summary>`` block to human-readable form.

    Mirrors ``format_compact_summary`` in compact.rs:
    1. Strip ``<analysis>`` block if present.
    2. Replace ``<summary>…</summary>`` with ``Summary:\\n{content}``.
    3. Collapse consecutive blank lines.
    """
    without_analysis = _strip_tag_block(summary, "analysis")
    inner = _extract_tag_block(without_analysis, "summary")
    if inner is not None:
        formatted = without_analysis.replace(
            f"<summary>{inner}</summary>",
            f"Summary:\n{inner.strip()}",
        )
    else:
        formatted = without_analysis
    return _collapse_blank_lines(formatted).strip()


# ---------------------------------------------------------------------------
# Summary merging
# ---------------------------------------------------------------------------


def _extract_summary_highlights(summary: str) -> list[str]:
    """Extract non-timeline bullet lines from a formatted summary."""
    lines: list[str] = []
    in_timeline = False
    for line in format_compact_summary(summary).splitlines():
        trimmed = line.rstrip()
        if not trimmed or trimmed in ("Summary:", "Conversation summary:"):
            continue
        if trimmed == "- Key timeline:":
            in_timeline = True
            continue
        if in_timeline:
            continue
        lines.append(trimmed)
    return lines


def _extract_summary_timeline(summary: str) -> list[str]:
    """Extract timeline lines from a formatted summary."""
    lines: list[str] = []
    in_timeline = False
    for line in format_compact_summary(summary).splitlines():
        trimmed = line.rstrip()
        if trimmed == "- Key timeline:":
            in_timeline = True
            continue
        if not in_timeline:
            continue
        if not trimmed:
            break
        lines.append(trimmed)
    return lines


def _merge_compact_summaries(existing_summary: Optional[str], new_summary: str) -> str:
    """Merge an existing compact summary with a newly produced one.

    Mirrors ``merge_compact_summaries`` in compact.rs.
    """
    if existing_summary is None:
        return new_summary

    previous_highlights = _extract_summary_highlights(existing_summary)
    new_formatted = format_compact_summary(new_summary)
    new_highlights = _extract_summary_highlights(new_formatted)
    new_timeline = _extract_summary_timeline(new_formatted)

    lines = ["<summary>", "Conversation summary:"]

    if previous_highlights:
        lines.append("- Previously compacted context:")
        lines.extend(f"  {h}" for h in previous_highlights)

    if new_highlights:
        lines.append("- Newly compacted context:")
        lines.extend(f"  {h}" for h in new_highlights)

    if new_timeline:
        lines.append("- Key timeline:")
        lines.extend(f"  {t}" for t in new_timeline)

    lines.append("</summary>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Continuation message assembly
# ---------------------------------------------------------------------------


def get_compact_continuation_message(
    summary: str,
    *,
    suppress_follow_up_questions: bool = True,
    recent_messages_preserved: bool = True,
) -> str:
    """Build the full system-message text for the compact prefix.

    Mirrors ``get_compact_continuation_message`` in compact.rs.
    """
    base = COMPACT_CONTINUATION_PREAMBLE + format_compact_summary(summary)
    if recent_messages_preserved:
        base += f"\n\n{COMPACT_RECENT_MESSAGES_NOTE}"
    if suppress_follow_up_questions:
        base += f"\n{COMPACT_DIRECT_RESUME_INSTRUCTION}"
    return base


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def compact_messages(
    messages: list[dict],
    config: Optional[AutoCompactConfig] = None,
) -> CompactResult:
    """Produce a deterministic compaction of *messages*.

    If ``should_compact()`` returns False the original messages are returned
    unchanged (``removed_message_count=0``).

    Mirrors ``compact_session`` in compact.rs.
    """
    if config is None:
        config = AutoCompactConfig()

    if not should_compact(messages, config):
        return CompactResult(
            summary="",
            formatted_summary="",
            compacted_messages=list(messages),
            removed_message_count=0,
        )

    existing_summary: Optional[str] = None
    prefix_len = _compacted_prefix_len(messages)
    if prefix_len > 0:
        existing_summary = _extract_existing_compacted_summary(messages[0])

    keep_from = max(0, len(messages) - config.preserve_recent)
    removed = messages[prefix_len:keep_from]
    preserved = messages[keep_from:]

    new_raw_summary = _summarize_messages(removed)
    merged_summary = _merge_compact_summaries(existing_summary, new_raw_summary)
    formatted = format_compact_summary(merged_summary)

    continuation_text = get_compact_continuation_message(
        merged_summary,
        suppress_follow_up_questions=True,
        recent_messages_preserved=bool(preserved),
    )

    compacted: list[dict] = [{"role": "system", "content": continuation_text}]
    compacted.extend(preserved)

    logger.info(
        "auto_compactor: compacted %d messages → %d "
        "(1 summary + %d preserved); removed=%d",
        len(messages),
        len(compacted),
        len(preserved),
        len(removed),
    )

    return CompactResult(
        summary=merged_summary,
        formatted_summary=formatted,
        compacted_messages=compacted,
        removed_message_count=len(removed),
    )
