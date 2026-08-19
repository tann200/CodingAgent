"""Runtime prompt enrichment helpers.

Two responsibilities:
1. ``load_project_instructions`` — discovers project-local AGENT.md / .agent/instructions.md
   files by walking upward from the working directory, deduplicates by content hash, and
   returns a single block suitable for appending to the system prompt.

2. ``build_git_context_block`` — runs three lightweight git commands and returns a fenced
   block containing branch/status, staged diff, and unstaged diff.  Output is capped so
   large repos do not flood the context window.

Both functions are safe to call in non-git, read-only, or test environments — all errors
are caught and result in an empty string.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# --- project instructions config ---
_INSTRUCTION_FILENAMES = ["AGENT.md", ".agent/instructions.md", "AGENT.local.md"]
_PER_FILE_CHAR_CAP = 4_000
_TOTAL_CHAR_CAP = 12_000

# --- git context config ---
_GIT_STATUS_CAP = 600  # bytes; short-form status is terse
_GIT_DIFF_CAP = 1_400  # bytes each for staged / unstaged
_GIT_TIMEOUT = 5  # seconds per subprocess call


def load_project_instructions(cwd: Path) -> str:
    """Walk from *cwd* upward collecting project-local instruction files.

    Searches each ancestor directory for any of::

        AGENT.md
        .agent/instructions.md
        AGENT.local.md

    Files are deduplicated by SHA-256 content hash.  Content is capped at
    ``_PER_FILE_CHAR_CAP`` chars per file and ``_TOTAL_CHAR_CAP`` chars total.

    Returns a non-empty string only when at least one instruction file is found;
    the string is wrapped in a ``<project_instructions>`` XML block ready for
    injection into the system prompt.
    """
    try:
        return _collect_instructions(cwd)
    except Exception as exc:
        logger.debug("load_project_instructions: error: %s", exc)
        return ""


def _collect_instructions(cwd: Path) -> str:
    seen_hashes: set = set()
    blocks: List[str] = []
    total = 0

    # Walk from cwd up to the filesystem root
    current = cwd.resolve()
    while True:
        for fname in _INSTRUCTION_FILENAMES:
            candidate = current / fname
            if not candidate.is_file():
                continue
            try:
                raw = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            digest = hashlib.sha256(raw.encode()).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            chunk = raw[:_PER_FILE_CHAR_CAP]
            if len(raw) > _PER_FILE_CHAR_CAP:
                chunk += (
                    f"\n... [{candidate.name} truncated at {_PER_FILE_CHAR_CAP} chars]"
                )
            total += len(chunk)
            blocks.append(f"<!-- {candidate} -->\n{chunk}")
            if total >= _TOTAL_CHAR_CAP:
                break
        if total >= _TOTAL_CHAR_CAP:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    if not blocks:
        return ""

    body = "\n\n".join(blocks)
    if total >= _TOTAL_CHAR_CAP:
        body += f"\n\n[Additional instruction files omitted — {_TOTAL_CHAR_CAP}-char cap reached]"
    return f"<project_instructions>\n{body}\n</project_instructions>"


def build_git_context_block(cwd: Path) -> str:
    """Return a fenced git-context block for injection into the system prompt.

    Runs three commands inside *cwd*:
    - ``git status --short --branch``
    - ``git diff --cached`` (staged changes)
    - ``git diff`` (unstaged changes)

    Each section is capped.  Returns an empty string when *cwd* is not inside a
    git repository or any subprocess call fails.
    """
    try:
        return _run_git_context(cwd)
    except Exception as exc:
        logger.debug("build_git_context_block: error: %s", exc)
        return ""


def _run_one(cmd: List[str], cwd: Path, cap: int) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    out = result.stdout
    if len(out) > cap:
        out = out[:cap] + f"\n... [truncated at {cap} chars]"
    return out.rstrip()


def _run_git_context(cwd: Path) -> str:
    # Quick check: is this a git repo?
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if probe.returncode != 0:
        return ""

    status = _run_one(["git", "status", "--short", "--branch"], cwd, _GIT_STATUS_CAP)
    staged = _run_one(["git", "diff", "--cached"], cwd, _GIT_DIFF_CAP)
    unstaged = _run_one(["git", "diff"], cwd, _GIT_DIFF_CAP)

    if not status and not staged and not unstaged:
        return ""

    parts = ["<git_context>"]
    if status:
        parts.append(f"## git status\n```\n{status}\n```")
    if staged:
        parts.append(f"## staged changes\n```diff\n{staged}\n```")
    if unstaged:
        parts.append(f"## unstaged changes\n```diff\n{unstaged}\n```")
    parts.append("</git_context>")
    return "\n\n".join(parts)


def build_runtime_context(cwd: Optional[Path] = None) -> str:
    """Convenience wrapper: return git context + project instructions as one block.

    Parameters
    ----------
    cwd:
        Working directory to inspect.  Defaults to ``Path.cwd()``.

    Returns
    -------
    str
        A string (possibly empty) ready to append to the static system prompt.
        The dynamic boundary marker is included so consumers can strip or locate it.
    """
    if cwd is None:
        cwd = Path.cwd()

    sections: List[str] = []

    git_block = build_git_context_block(cwd)
    if git_block:
        sections.append(git_block)

    instr_block = load_project_instructions(cwd)
    if instr_block:
        sections.append(instr_block)

    # Optional LSP symbol index (gated by config flag / env var)
    try:
        from src.core.indexing.lsp_context import get_lsp_context_block

        lsp_block = get_lsp_context_block(workdir=cwd)
        if lsp_block:
            sections.append(lsp_block)
    except Exception:
        pass

    # CP-10: Auto-inject live LSP diagnostics from the synchronous cache.
    # This surfaces type errors / lint warnings that the language server has
    # already computed (via pull or push) without starting any new async work.
    try:
        from src.core.indexing.lsp_context import get_lsp_diagnostics_block

        diag_block = get_lsp_diagnostics_block(workdir=cwd)
        if diag_block:
            sections.append(diag_block)
    except Exception:
        pass

    # UX-1: When LSP is enabled but no server is active, inject a notice so
    # the model (and indirectly the user) knows type-checking is inactive.
    try:
        from src.core.indexing.lsp_context import get_lsp_status_notice

        lsp_notice = get_lsp_status_notice(workdir=cwd)
        if lsp_notice:
            sections.append(lsp_notice)
    except Exception:
        pass

    if not sections:
        return ""

    marker = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
    return f"\n\n{marker}\n\n" + "\n\n".join(sections)
