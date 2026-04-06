"""Ancestor instruction file discovery — CP-11.

Python port of claw-code's ``discover_instruction_files`` in ``prompt.rs``.

Walks from the current working directory up to the filesystem root, checking
each ancestor directory for any of the following instruction files:

    AGENTS.md
    AGENTS.local.md
    .agent/AGENTS.md
    .agent/instructions.md

Files are deduplicated using a stable content hash (normalised whitespace).
Per-file budget: 4,000 chars.  Total budget: 12,000 chars.

The result is a flat list of ``InstructionFile`` named-tuples, ready to be
rendered into the system prompt.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Character budgets — match claw-code's constants
MAX_INSTRUCTION_FILE_CHARS: int = 4_000
MAX_TOTAL_INSTRUCTION_CHARS: int = 12_000

# File names to probe in each ancestor directory (in order, within each dir).
# Mirrors: CLAW.md, CLAW.local.md, .claw/CLAW.md, .claw/instructions.md
_CANDIDATE_NAMES: tuple[tuple[str, ...], ...] = (
    ("AGENTS.md",),
    ("AGENTS.local.md",),
    (".agent", "AGENTS.md"),
    (".agent", "instructions.md"),
)


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------


@dataclass
class InstructionFile:
    """A discovered instruction file with its path and content."""

    path: Path
    content: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_content(content: str) -> str:
    """Collapse runs of blank lines and strip outer whitespace.

    Mirrors ``normalize_instruction_content`` in prompt.rs.
    """
    # Collapse 2+ blank lines to one
    normalized = re.sub(r"\n{3,}", "\n\n", content)
    return normalized.strip()


def _stable_content_hash(content: str) -> str:
    """SHA-256 of normalized content — used for deduplication.

    Mirrors ``stable_content_hash`` in prompt.rs (we use SHA-256 since
    Python's ``hash()`` is salted per-process; SHA-256 is deterministic).
    """
    return hashlib.sha256(content.encode()).hexdigest()


def _truncate_content(content: str, remaining_chars: int) -> str:
    """Truncate *content* to the lesser of per-file and *remaining_chars* limits.

    Mirrors ``truncate_instruction_content`` in prompt.rs.
    """
    hard_limit = min(MAX_INSTRUCTION_FILE_CHARS, remaining_chars)
    trimmed = content.strip()
    if len(trimmed) <= hard_limit:
        return trimmed
    return trimmed[:hard_limit] + "\n\n[truncated]"


def _describe_file(file: InstructionFile, all_files: list[InstructionFile]) -> str:
    """Produce a display label like ``AGENTS.md (scope: /home/user/project)``.

    Mirrors ``describe_instruction_file`` in prompt.rs.
    """
    name = file.path.name
    # Scope: the deepest ancestor directory that is a parent of this file
    parent_dirs = [f.path.parent for f in all_files if file.path != f.path]
    scope_dir = next(
        (
            d
            for d in sorted(parent_dirs, key=lambda p: len(p.parts), reverse=True)
            if file.path.is_relative_to(d)
        ),
        file.path.parent,
    )
    return f"{name} (scope: {scope_dir})"


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------


def discover_instruction_files(
    workdir: Optional[str | Path] = None,
) -> list[InstructionFile]:
    """Discover instruction files from *workdir* up to the filesystem root.

    Files are returned in ancestor-first order (root directory first,
    working directory last) — the same ordering as claw-code so that more
    specific (closer) instructions take precedence when rendered last.

    Mirrors ``discover_instruction_files`` in prompt.rs.
    """
    cwd = Path(workdir).resolve() if workdir else Path.cwd().resolve()

    # Build ancestor chain from root → cwd (claw-code reverses the cursor list)
    ancestors: list[Path] = []
    cursor: Optional[Path] = cwd
    while cursor is not None:
        ancestors.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    ancestors.reverse()  # root first

    files: list[InstructionFile] = []
    for directory in ancestors:
        for parts in _CANDIDATE_NAMES:
            candidate = directory.joinpath(*parts)
            try:
                content = candidate.read_text(encoding="utf-8")
                if content.strip():
                    files.append(InstructionFile(path=candidate, content=content))
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.debug(
                    "discover_instruction_files: could not read %s: %s", candidate, exc
                )

    return _dedupe(files)


def _dedupe(files: list[InstructionFile]) -> list[InstructionFile]:
    """Remove files whose normalised content is identical to a prior file.

    Mirrors ``dedupe_instruction_files`` in prompt.rs.
    """
    seen: set[str] = set()
    result: list[InstructionFile] = []
    for f in files:
        h = _stable_content_hash(_normalize_content(f.content))
        if h in seen:
            continue
        seen.add(h)
        result.append(f)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_instruction_files(files: list[InstructionFile]) -> str:
    """Render instruction files into a system-prompt section string.

    Mirrors ``render_instruction_files`` in prompt.rs.

    Returns an empty string when *files* is empty.
    """
    if not files:
        return ""

    sections = ["# Project instructions"]
    remaining = MAX_TOTAL_INSTRUCTION_CHARS

    for f in files:
        if remaining == 0:
            sections.append(
                "_Additional instruction content omitted after reaching the prompt budget._"
            )
            break
        raw = _truncate_content(f.content, remaining)
        consumed = min(len(raw), remaining)
        remaining -= consumed

        label = _describe_file(f, files)
        sections.append(f"## {label}")
        sections.append(raw)

    return "\n\n".join(sections)
