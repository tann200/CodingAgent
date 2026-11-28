"""Multi-pass fuzzy edit matcher and closest-match diagnostics (OPT-04 / OPT-06).

Extracted from ``src/tools/_edit_tools.py`` to keep edit matching logic cleanly
isolated and testable. All symbols are re-exported from ``_edit_tools.py``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Optional, Tuple

_LINE_NUM_PREFIX_RE = re.compile(r"^\s*\d+\s*\| ?")


@dataclass(frozen=True)
class _EditMatch:
    """Resolved match location and normalization metadata for atomic edits."""

    start: int
    end: int
    matched_text: str
    strategy: str
    indent_prefix_from: str = ""
    indent_prefix_to: str = ""
    strip_new_prefixes: bool = False


def _strip_line_number_prefixes(text: str) -> Optional[str]:
    """Strip ``read_file``-style ``   42 | `` prefixes when every non-empty line has one.

    Returns the cleaned text (preserving trailing newline parity), or ``None``
    if *text* does not look like a line-numbered excerpt.
    """
    lines = text.splitlines(keepends=True)
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return None
    if not all(_LINE_NUM_PREFIX_RE.match(ln) for ln in non_empty):
        return None
    cleaned_parts: list[str] = []
    for ln in lines:
        if ln.strip():
            cleaned_parts.append(_LINE_NUM_PREFIX_RE.sub("", ln, count=1))
        else:
            cleaned_parts.append(ln)
    cleaned = "".join(cleaned_parts)
    return cleaned if cleaned != text else None


def _common_indent_prefix(lines: list[str]) -> str:
    """Return the shortest leading-whitespace prefix shared by all non-empty lines."""
    indents = [
        ln[: len(ln) - len(ln.lstrip(" \t"))]
        for ln in lines
        if ln.strip()
    ]
    if not indents:
        return ""
    return min(indents, key=len)


def _dedent_lines(lines: list[str]) -> tuple[list[str], str]:
    """Strip the common leading whitespace prefix from *lines* and return ``(dedented, prefix)``."""
    prefix = _common_indent_prefix(lines)
    plen = len(prefix)
    dedented = [
        (ln[plen:] if ln.strip() and len(ln) >= plen else ln).rstrip()
        for ln in lines
    ]
    return dedented, prefix


def _slice_for_line_window(
    content: str,
    content_lines: list[str],
    line_offsets: list[int],
    start_line_idx: int,
    n: int,
    target_ends_with_nl: bool,
) -> tuple[int, int, str]:
    """Return ``(start_char, end_char, matched_text)`` preserving trailing-newline parity."""
    start_char = line_offsets[start_line_idx]
    end_char = line_offsets[start_line_idx + n]
    chunk = content[start_char:end_char]
    if not target_ends_with_nl:
        if chunk.endswith("\r\n"):
            end_char -= 2
            chunk = chunk[:-2]
        elif chunk.endswith("\n") or chunk.endswith("\r"):
            end_char -= 1
            chunk = chunk[:-1]
    return start_char, end_char, chunk


def _find_edit_match(
    content: str, target: str
) -> Tuple[Optional[_EditMatch], Optional[str]]:
    """Find *target* in *content* using strictly pass-ordered strategies.

    OPT-04: Each strategy scans the ENTIRE file before advancing to the next
    looser strategy, preventing an earlier loose match from shadowing a later
    tighter match and checking uniqueness across all normalized matches.

    Pass order:
      0. Exact substring match
      0b. ``read_file`` line-number prefix stripped exact match
      1. Trailing-whitespace-trimmed per-line match
      2. Collapsed internal-whitespace per-line match (tabs/spaces -> single space)
      3. Indentation-flexible (dedented) match (captures indent delta for re-indenting)

    Returns ``(match, None)`` on a unique match, ``(None, "ambiguous:<pass>:<count>")``
    when a pass finds multiple matches, or ``(None, None)`` when no pass matches.
    """
    if not target:
        return None, None

    # Pass 0: exact match
    exact_count = content.count(target)
    if exact_count > 1:
        return None, f"ambiguous:exact:{exact_count}"
    if exact_count == 1:
        pos = content.find(target)
        return (
            _EditMatch(
                start=pos,
                end=pos + len(target),
                matched_text=target,
                strategy="exact",
            ),
            None,
        )

    # Pass 0b: strip copied read_file line-number prefixes (e.g. "  42 | code")
    stripped_prefix_target = _strip_line_number_prefixes(target)
    effective_target = target
    strip_new_prefixes = False
    if stripped_prefix_target:
        pcount = content.count(stripped_prefix_target)
        if pcount > 1:
            return None, f"ambiguous:line_prefix_stripped:{pcount}"
        if pcount == 1:
            pos = content.find(stripped_prefix_target)
            return (
                _EditMatch(
                    start=pos,
                    end=pos + len(stripped_prefix_target),
                    matched_text=stripped_prefix_target,
                    strategy="line_prefix_stripped",
                    strip_new_prefixes=True,
                ),
                None,
            )
        effective_target = stripped_prefix_target
        strip_new_prefixes = True

    target_lines = effective_target.splitlines()
    n = len(target_lines)
    if n == 0:
        return None, None

    content_lines = content.splitlines(keepends=True)
    total = len(content_lines)
    if total < n:
        return None, None

    line_offsets: list[int] = [0]
    for ln in content_lines:
        line_offsets.append(line_offsets[-1] + len(ln))
    target_ends_with_nl = effective_target.endswith(("\n", "\r"))
    raw_lines = [ln.rstrip("\r\n") for ln in content_lines]

    # Pass 1: trailing whitespace trimmed across entire file
    target_rstripped = [ln.rstrip() for ln in target_lines]
    file_rstripped = [ln.rstrip() for ln in raw_lines]
    pass1_hits = [
        i
        for i in range(total - n + 1)
        if file_rstripped[i : i + n] == target_rstripped
    ]
    if len(pass1_hits) > 1:
        return None, f"ambiguous:trailing_whitespace:{len(pass1_hits)}"
    if len(pass1_hits) == 1:
        s, e, chunk = _slice_for_line_window(
            content, content_lines, line_offsets, pass1_hits[0], n, target_ends_with_nl
        )
        return (
            _EditMatch(
                start=s,
                end=e,
                matched_text=chunk,
                strategy="trailing_whitespace",
                strip_new_prefixes=strip_new_prefixes,
            ),
            None,
        )

    # Pass 2: collapsed internal whitespace (preserving leading indentation) across entire file
    def _norm_line(ln: str) -> str:
        stripped = ln.lstrip(" \t")
        leading = ln[: len(ln) - len(stripped)]
        return leading + re.sub(r"[ \t]+", " ", stripped).rstrip()

    target_norm = [_norm_line(ln) for ln in target_lines]
    file_norm = [_norm_line(ln) for ln in raw_lines]
    pass2_hits = [
        i
        for i in range(total - n + 1)
        if file_norm[i : i + n] == target_norm
    ]
    if len(pass2_hits) > 1:
        return None, f"ambiguous:normalized_whitespace:{len(pass2_hits)}"
    if len(pass2_hits) == 1:
        s, e, chunk = _slice_for_line_window(
            content, content_lines, line_offsets, pass2_hits[0], n, target_ends_with_nl
        )
        return (
            _EditMatch(
                start=s,
                end=e,
                matched_text=chunk,
                strategy="normalized_whitespace",
                strip_new_prefixes=strip_new_prefixes,
            ),
            None,
        )

    # Pass 3: indentation-flexible (dedented) match across entire file
    target_dedented, target_indent = _dedent_lines(target_lines)
    pass3_hits: list[tuple[int, str]] = []
    for i in range(total - n + 1):
        chunk_dedented, chunk_indent = _dedent_lines(raw_lines[i : i + n])
        if chunk_dedented == target_dedented:
            pass3_hits.append((i, chunk_indent))
    if len(pass3_hits) > 1:
        return None, f"ambiguous:indentation_flexible:{len(pass3_hits)}"
    if len(pass3_hits) == 1:
        idx, chunk_indent = pass3_hits[0]
        s, e, chunk = _slice_for_line_window(
            content, content_lines, line_offsets, idx, n, target_ends_with_nl
        )
        return (
            _EditMatch(
                start=s,
                end=e,
                matched_text=chunk,
                strategy="indentation_flexible",
                indent_prefix_from=target_indent,
                indent_prefix_to=chunk_indent,
                strip_new_prefixes=strip_new_prefixes,
            ),
            None,
        )

    return None, None


def _reindent_replacement(
    new_string: str, indent_from: str, indent_to: str
) -> str:
    """Adjust leading indentation of *new_string* from *indent_from* to *indent_to*."""
    if indent_from == indent_to or not new_string:
        return new_string
    out_lines: list[str] = []
    from_len = len(indent_from)
    for ln in new_string.splitlines(keepends=True):
        if not ln.strip():
            out_lines.append(ln)
            continue
        if from_len and ln.startswith(indent_from):
            body = ln[from_len:]
        elif from_len:
            ws_len = len(ln) - len(ln.lstrip(" \t"))
            body = ln[min(ws_len, from_len) :]
        else:
            body = ln
        out_lines.append(indent_to + body)
    return "".join(out_lines)


def _apply_edit_match(content: str, match: _EditMatch, new_string: str) -> str:
    """Apply *match* replacement with prefix cleanup and indentation preservation."""
    replacement = new_string
    if match.strip_new_prefixes:
        cleaned_new = _strip_line_number_prefixes(replacement)
        if cleaned_new is not None:
            replacement = cleaned_new
    if match.strategy == "indentation_flexible":
        replacement = _reindent_replacement(
            replacement, match.indent_prefix_from, match.indent_prefix_to
        )
    return content[: match.start] + replacement + content[match.end :]


def _find_closest_block_hint(
    content: str, target: str, *, min_ratio: float = 0.55
) -> str:
    """Return a compact line-numbered hint for the closest matching window in *content*."""
    cleaned_target = _strip_line_number_prefixes(target) or target
    target_lines = [ln.strip() for ln in cleaned_target.splitlines()]
    n = len(target_lines)
    if n == 0:
        return ""

    raw_lines = content.splitlines()
    total = len(raw_lines)
    if total == 0:
        return ""

    win = min(n, total)
    target_joined = "\n".join(target_lines)
    target_tokens = {
        tok for tok in re.findall(r"\w+", target_joined.lower()) if len(tok) > 1
    }

    candidate_starts: list[int]
    if total - win + 1 > 400 and target_tokens:
        scored: list[tuple[int, int]] = []
        line_token_sets = [
            {tok for tok in re.findall(r"\w+", ln.lower()) if len(tok) > 1}
            for ln in raw_lines
        ]
        for i in range(total - win + 1):
            overlap = 0
            for j in range(i, i + win):
                overlap += len(target_tokens & line_token_sets[j])
            if overlap > 0:
                scored.append((overlap, i))
        scored.sort(key=lambda item: -item[0])
        candidate_starts = [idx for _, idx in scored[:80]]
    else:
        candidate_starts = list(range(total - win + 1))

    best_ratio = 0.0
    best_start = -1
    for i in candidate_starts:
        window_joined = "\n".join(ln.strip() for ln in raw_lines[i : i + win])
        ratio = difflib.SequenceMatcher(None, target_joined, window_joined).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_start < 0 or best_ratio < min_ratio:
        return ""

    max_preview_lines = 12
    end_idx = min(total, best_start + win)
    preview_end = min(end_idx, best_start + max_preview_lines)
    snippet_lines = [
        f"{ln_no:4d} | {raw_lines[ln_no - 1]}"
        for ln_no in range(best_start + 1, preview_end + 1)
    ]
    if end_idx > preview_end:
        snippet_lines.append(f"... ({end_idx - preview_end} more lines)")
    snippet = "\n".join(snippet_lines)
    if len(snippet) > 650:
        snippet = snippet[:647] + "..."
    return (
        f"Closest match near lines {best_start + 1}-{end_idx} "
        f"(similarity {best_ratio:.0%}):\n{snippet}"
    )


def _fuzzy_find(content: str, target: str) -> Optional[str]:
    """Find *target* in *content* using strictly pass-ordered fuzzy strategies.

    Backwards-compatible wrapper over ``_find_edit_match`` returning the matched
    substring in *content*, or ``None`` if unmatched or ambiguous.
    """
    match, _err = _find_edit_match(content, target)
    return match.matched_text if match is not None else None
