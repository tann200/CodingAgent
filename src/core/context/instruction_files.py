"""Ancestor instruction file discovery — CP-11.

Python port of claw-code's ``discover_instruction_files`` in ``prompt.rs``.

Walks from the current working directory up to the filesystem root, checking
each ancestor directory for any of the following instruction files:

    AGENTS.md
    AGENTS.local.md
    .agent/AGENTS.md
    .agent/instructions.md
    CLAUDE.md
    CLAUDE.local.md
    .claude/CLAUDE.md
    .claw/CLAUDE.md
    .claw/instructions.md

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
MAX_ARCHITECTURE_FILE_CHARS: int = 3_000
MAX_TOTAL_INSTRUCTION_CHARS: int = 12_000

# File names to probe in each ancestor directory (in order, within each dir).
# TASK-1: extended to cover Claude Code / claw-code CLAUDE.md convention in
# addition to the original AGENTS.md convention.  SHA-256 dedup handles the
# case where both variants exist with identical content.
_CANDIDATE_NAMES: tuple[tuple[str, ...], ...] = (
    # CodingAgent convention
    ("AGENTS.md",),
    ("AGENT.md",),
    ("AGENTS.local.md",),
    ("AGENT.local.md",),
    (".codingAgent", "AGENTS.md"),
    (".codingAgent", "AGENT.md"),
    (".codingAgent", "instructions.md"),
    (".agent", "AGENTS.md"),
    (".agent", "AGENT.md"),
    (".agent", "instructions.md"),
    # Claude Code / claw-code convention
    ("CLAUDE.md",),
    ("CLAUDE.local.md",),
    (".claude", "CLAUDE.md"),
    (".claw", "CLAUDE.md"),
    (".claw", "instructions.md"),
)

# Root-only architecture doc discovered in workspace root (capped at 3,000 chars)
_ROOT_ONLY_CANDIDATES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("ARCHITECTURE.md",), MAX_ARCHITECTURE_FILE_CHARS),
)

# Subtree instruction files discovered lazily when touching files beneath workdir
_NESTED_CANDIDATE_NAMES: tuple[tuple[str, ...], ...] = (
    ("AGENTS.md",),
    ("AGENTS.local.md",),
    ("CLAUDE.md",),
    ("CLAUDE.local.md",),
)


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------


@dataclass
class InstructionFile:
    """A discovered instruction file with its path and content."""

    path: Path
    content: str
    max_chars: Optional[int] = None


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


def _truncate_content(
    content: str,
    remaining_chars: int,
    max_file_chars: int = MAX_INSTRUCTION_FILE_CHARS,
) -> str:
    """Truncate *content* to the lesser of per-file and *remaining_chars* limits.

    Mirrors ``truncate_instruction_content`` in prompt.rs.
    """
    hard_limit = min(max_file_chars, remaining_chars)
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

    ancestors: list[Path] = []
    cursor: Optional[Path] = cwd
    while cursor is not None:
        ancestors.append(cursor)
        if (cursor / ".git").exists():
            break
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
                    try:
                        from src.core.security.redaction import redact_secrets
                        content = redact_secrets(content)
                    except Exception:
                        pass
                    files.append(InstructionFile(path=candidate, content=content))
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.debug(
                    "discover_instruction_files: could not read %s: %s", candidate, exc
                )

    # TASK-32: Root-only ARCHITECTURE.md discovery (capped at 3,000 chars)
    for parts, cap in _ROOT_ONLY_CANDIDATES:
        candidate = cwd.joinpath(*parts)
        try:
            content = candidate.read_text(encoding="utf-8")
            if content.strip():
                try:
                    from src.core.security.redaction import redact_secrets
                    content = redact_secrets(content)
                except Exception:
                    pass
                files.append(
                    InstructionFile(path=candidate, content=content, max_chars=cap)
                )
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.debug(
                "discover_instruction_files: could not read %s: %s", candidate, exc
            )

    return _dedupe(files)


def discover_nested_instruction_files(
    touched_path: str | Path,
    workdir: str | Path,
    seen_signatures: Optional[set[str]] = None,
) -> tuple[list[InstructionFile], str]:
    """Lazily discover nested ``AGENTS.md`` / ``CLAUDE.md`` files beneath *workdir*.

    Walks intermediate directories strictly between *workdir* and the directory
    containing *touched_path* (shallowest first). Each candidate file is hashed
    with SHA-256 (``"<resolved_path>:<sha256>"``) so already-seen files are
    skipped unless modified on disk.

    Returns ``(new_files, rendered_envelope)`` where *rendered_envelope* is
    wrapped in a nonce-fenced ``<project_instructions>`` envelope (or ``""``).
    """
    try:
        wd = Path(workdir).resolve()
        tp = Path(touched_path).resolve()
    except OSError:
        return [], ""

    if not tp.is_relative_to(wd):
        return [], ""

    target_dir = tp if tp.is_dir() else tp.parent
    if target_dir == wd:
        return [], ""

    rel_parts = target_dir.relative_to(wd).parts
    intermediate_dirs: list[Path] = []
    cur = wd
    for part in rel_parts:
        cur = cur / part
        intermediate_dirs.append(cur)

    collected: list[InstructionFile] = []
    for directory in intermediate_dirs:
        for parts in _NESTED_CANDIDATE_NAMES:
            candidate = directory.joinpath(*parts)
            try:
                if not candidate.is_file():
                    continue
                resolved_cand = candidate.resolve()
                if not resolved_cand.is_relative_to(wd):
                    continue
                content = resolved_cand.read_text(encoding="utf-8")
                if not content.strip():
                    continue
                try:
                    from src.core.security.redaction import redact_secrets

                    content = redact_secrets(content)
                except Exception:
                    pass
                sig = f"{resolved_cand}:{_stable_content_hash(_normalize_content(content))}"
                if seen_signatures is not None:
                    if sig in seen_signatures:
                        continue
                    seen_signatures.add(sig)
                collected.append(InstructionFile(path=resolved_cand, content=content))
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.debug(
                    "discover_nested_instruction_files: could not read %s: %s",
                    candidate,
                    exc,
                )

    deduped = _dedupe(collected)
    if not deduped:
        return [], ""
    return deduped, render_instruction_files(deduped)


def maybe_collect_nested_instructions(
    touched_path: str | Path,
    workdir: str | Path,
) -> str:
    """Collect unseen nested subtree instructions for the active orchestrator session."""
    try:
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR

        orch = _PARENT_ORCHESTRATOR_VAR.get(None)
    except Exception:
        orch = None
    if orch is None:
        return ""
    seen = getattr(orch, "_seen_nested_instruction_sigs", None)
    if not isinstance(seen, set):
        seen = set()
        setattr(orch, "_seen_nested_instruction_sigs", seen)
    _, rendered = discover_nested_instruction_files(
        touched_path, workdir, seen_signatures=seen
    )
    return rendered


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
        raw = _truncate_content(
            f.content,
            remaining,
            max_file_chars=f.max_chars or MAX_INSTRUCTION_FILE_CHARS,
        )
        consumed = min(len(raw), remaining)
        remaining -= consumed

        label = _describe_file(f, files)
        sections.append(f"## {label}")
        sections.append(raw)

    from src.core.security.untrusted_envelopes import wrap_untrusted

    return wrap_untrusted("project_instructions", "\n\n".join(sections))
