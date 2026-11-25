"""Content-type tool output reduction, bounded raw-output handle store, and read-guard helpers.

TASK-30:
- ``ToolOutputStore``: bounded, thread-safe LRU store (max 32 entries, max 4 MiB total,
  max 512 KiB per entry) holding pre-redacted raw tool outputs addressable via
  deterministic sequential handles (``out_1``, ``out_2``, ...).
- Deterministic content-type reducers (``validation``, ``git``, ``diff``, ``search``,
  ``json``, ``generic``) triggered only when a string exceeds ``max_chars``.
- ``is_surgical_preview_edit_allowed()``: enforces the Context-Aware Read Guard
  invariant that a read-guard preview never satisfies read-before-write for blind
  full-file overwrite, while permitting surgical edits whose ``old_string`` appears
  verbatim in the previewed lines.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.core.security.redaction import redact_secrets

MAX_ENTRIES: int = 32
MAX_TOTAL_BYTES: int = 4 * 1024 * 1024  # 4 MiB
MAX_ENTRY_BYTES: int = 512 * 1024  # 512 KiB
DEFAULT_READ_LIMIT: int = 8_000
MAX_READ_LIMIT: int = 32_000

_VALIDATION_TOOLS: frozenset[str] = frozenset(
    {"run_tests", "run_js_tests", "run_linter", "lsp_diagnostics"}
)
_DIFF_TOOLS: frozenset[str] = frozenset({"git_diff", "get_git_diff", "diff_state"})
_GIT_TOOLS: frozenset[str] = frozenset({"git_status", "git_log"})
_SEARCH_TOOLS: frozenset[str] = frozenset(
    {"grep", "search_code", "find_files", "glob", "find_references", "list_files"}
)

_VALIDATION_CMD_RE = re.compile(
    r"\b(?:pytest|unittest|mypy|ruff|flake8|pylint|cargo\s+(?:test|check|clippy)|"
    r"jest|vitest|tsc|eslint|go\s+test|npm\s+test|pnpm\s+test|yarn\s+test)\b",
    re.IGNORECASE,
)
_VALIDATION_TEXT_RE = re.compile(
    r"(?:^=+\s*(?:FAILURES|ERRORS|short test summary info)\s*=+|"
    r"^FAILED\s+|Traceback \(most recent call last\):|"
    r"AssertionError:|error\[E\d+\]:|\b\d+\s+failed\b)",
    re.MULTILINE,
)
_VALIDATION_KEEP_LINE_RE = re.compile(
    r"(?:^\s*(?:FAILED|ERROR|E\s+|>\s+|File \"|Traceback|AssertionError|"
    r"=+\s*(?:FAILURES|ERRORS|short test summary|.*failed.*)=+|"
    r"error\[E\d+\]:|error:|warning:|.*:\d+(?::\d+)?:\s*(?:error|E\d+|F\d+|W\d+))|"
    r"\b(?:AssertionError|TypeError|ValueError|KeyError|AttributeError|ImportError|SyntaxError|NameError)\b)",
    re.IGNORECASE,
)
_PASSING_NOISE_RE = re.compile(
    r"^(?:\.+|PASSED\b|.*::.*\s+PASSED\b|\s*ok\s*$|collecting\s+\.\.\.)",
    re.IGNORECASE,
)
_SEARCH_MATCH_LINE_RE = re.compile(r"^([^:\n]+):(\d+):(.*)$")


@dataclass
class _StoredOutput:
    handle: str
    content: str
    byte_size: int
    tool_name: str
    field_name: str
    content_type: str


class ToolOutputStore:
    """Thread-safe bounded LRU store for pre-redacted raw tool outputs."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_ENTRIES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_entry_bytes: int = MAX_ENTRY_BYTES,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_total_bytes = max(1024, int(max_total_bytes))
        self.max_entry_bytes = max(512, min(int(max_entry_bytes), self.max_total_bytes))
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _StoredOutput]" = OrderedDict()
        self._total_bytes: int = 0
        self._counter: int = 0
        self._evicted_count: int = 0

    def store(
        self,
        content: str,
        *,
        tool_name: str = "",
        field_name: str = "",
        content_type: str = "generic",
    ) -> str:
        """Redact secrets, cap entry size, store under sequential handle, and evict LRU if needed."""
        redacted = redact_secrets(content)
        encoded = redacted.encode("utf-8", errors="replace")
        if len(encoded) > self.max_entry_bytes:
            suffix = "\n[raw output capped at entry byte limit]"
            keep_bytes = max(0, self.max_entry_bytes - len(suffix.encode("utf-8")))
            redacted = encoded[:keep_bytes].decode("utf-8", errors="ignore") + suffix
            encoded = redacted.encode("utf-8", errors="replace")
        byte_size = len(encoded)

        with self._lock:
            self._counter += 1
            handle = f"out_{self._counter}"
            while self._entries and (
                len(self._entries) >= self.max_entries
                or (self._total_bytes + byte_size) > self.max_total_bytes
            ):
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes = max(0, self._total_bytes - evicted.byte_size)
                self._evicted_count += 1

            self._entries[handle] = _StoredOutput(
                handle=handle,
                content=redacted,
                byte_size=byte_size,
                tool_name=tool_name,
                field_name=field_name,
                content_type=content_type,
            )
            self._total_bytes += byte_size
            return handle

    def read(
        self,
        handle: str,
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> Dict[str, Any]:
        """Read a character slice from a previously stored tool output handle."""
        if not isinstance(handle, str) or not handle.strip():
            return {
                "status": "error",
                "error": "A non-empty tool output handle (e.g. 'out_1') is required.",
                "recoverable": True,
            }
        key = handle.strip()
        start = max(0, int(offset))
        span = max(1, min(int(limit), MAX_READ_LIMIT))

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return {
                    "status": "error",
                    "error": (
                        f"Tool output handle '{key}' was not found or has been evicted "
                        f"(store retains up to {self.max_entries} outputs / "
                        f"{self.max_total_bytes // (1024 * 1024)} MiB)."
                    ),
                    "recoverable": True,
                }
            self._entries.move_to_end(key)
            total_chars = len(entry.content)
            chunk = entry.content[start : start + span]
            has_more = (start + len(chunk)) < total_chars
            return {
                "status": "ok",
                "handle": key,
                "tool_name": entry.tool_name,
                "content_type": entry.content_type,
                "offset": start,
                "limit": span,
                "total_chars": total_chars,
                "has_more": has_more,
                "content": chunk,
            }

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "total_bytes": self._total_bytes,
                "evicted_count": self._evicted_count,
            }


def get_or_create_output_store(orch: Any) -> Optional[ToolOutputStore]:
    """Return the per-session ``ToolOutputStore`` attached to *orch*, creating lazily."""
    if orch is None:
        return None
    store = getattr(orch, "_tool_output_store", None)
    if isinstance(store, ToolOutputStore):
        return store
    store = ToolOutputStore()
    try:
        setattr(orch, "_tool_output_store", store)
    except Exception:
        return store
    return store


def classify_tool_output(
    tool_name: str,
    args: Optional[Mapping[str, Any]],
    text: str,
) -> str:
    """Classify tool output into one of: validation, diff, git, search, json, generic."""
    name = (tool_name or "").strip().lower()
    cmd = ""
    if isinstance(args, Mapping):
        raw_cmd = args.get("command") or args.get("cmd") or ""
        if isinstance(raw_cmd, str):
            cmd = raw_cmd.strip()

    if name in _VALIDATION_TOOLS or (name in ("bash", "bash_readonly") and _VALIDATION_CMD_RE.search(cmd)):
        return "validation"
    if name in _DIFF_TOOLS:
        return "diff"
    if name in _GIT_TOOLS or (name in ("bash", "bash_readonly") and cmd.startswith("git ")):
        if "diff --git " in text or "\n@@ -" in text:
            return "diff"
        return "git"
    if name in _SEARCH_TOOLS:
        return "search"

    if "diff --git " in text or (text.startswith("--- ") and "\n+++ " in text and "\n@@ -" in text):
        return "diff"
    if _VALIDATION_TEXT_RE.search(text):
        return "validation"

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")) and stripped.rstrip().endswith(("}", "]")):
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            if len(stripped) > 256 and ('":' in stripped[:256] or stripped.startswith("[{")):
                return "json"

    return "generic"


def _head_tail_slice(text: str, budget: int, head_ratio: float = 0.40) -> Tuple[str, int]:
    """Slice *text* into head + tail within *budget* chars; return (body, omitted_chars)."""
    if len(text) <= budget:
        return text, 0
    safe_budget = max(80, budget)
    head_len = max(32, int(safe_budget * head_ratio))
    tail_len = max(32, safe_budget - head_len)
    if head_len + tail_len >= len(text):
        return text, 0
    omitted = len(text) - (head_len + tail_len)
    return text[:head_len] + "\n...\n" + text[-tail_len:], omitted


def _format_marker(omitted: int, content_type: str, handle: Optional[str]) -> str:
    if handle:
        return (
            f"\n[... {omitted} chars omitted ({content_type} reducer); "
            f'call read_tool_output(handle="{handle}", offset=0, limit={DEFAULT_READ_LIMIT}) for raw output ...]'
        )
    return f"\n[... {omitted} chars omitted ({content_type} reducer) ...]"


def _reduce_validation(text: str, budget: int) -> str:
    """Keep failure/error lines, assertion frames, and summary tail; collapse passing noise."""
    lines = text.splitlines()
    if not lines:
        body, _ = _head_tail_slice(text, budget)
        return body

    tail_lines = lines[-min(25, max(8, len(lines) // 8)) :]
    tail_start_idx = len(lines) - len(tail_lines)

    kept: List[str] = []
    collapsed_run = 0
    in_traceback = 0

    for idx, line in enumerate(lines[:tail_start_idx]):
        if _VALIDATION_KEEP_LINE_RE.search(line):
            if collapsed_run > 0:
                kept.append(f"[... {collapsed_run} passing/setup lines collapsed ...]")
                collapsed_run = 0
            kept.append(line)
            if "Traceback" in line or "FAILURES" in line or "ERRORS" in line:
                in_traceback = 12
            elif line.lstrip().startswith(("File \"", "> ", "E ")):
                in_traceback = max(in_traceback, 6)
        elif in_traceback > 0:
            kept.append(line)
            in_traceback -= 1
        elif _PASSING_NOISE_RE.match(line) or not line.strip():
            collapsed_run += 1
        else:
            collapsed_run += 1

    if collapsed_run > 0:
        kept.append(f"[... {collapsed_run} passing/setup lines collapsed ...]")

    tail_block = "\n".join(tail_lines)
    if not kept:
        body, _ = _head_tail_slice(text, budget, head_ratio=0.35)
        return body

    combined = "\n".join(kept) + "\n" + tail_block
    if len(combined) <= budget:
        return combined

    tail_budget = min(len(tail_block), max(200, int(budget * 0.30)))
    head_budget = max(120, budget - tail_budget - 10)
    head_part = "\n".join(kept)[:head_budget]
    tail_part = tail_block[-tail_budget:]
    return head_part + "\n...\n" + tail_part


def _reduce_git(text: str, budget: int) -> str:
    """Keep branch/status header, changed-file stat summary, and bounded previews."""
    lines = text.splitlines()
    header_prefixes = (
        "On branch",
        "HEAD detached",
        "Your branch",
        "Changes to be committed",
        "Changes not staged",
        "Untracked files",
        "commit ",
        "Author:",
        "Date:",
        "## ",
    )
    kept: List[str] = []
    other: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(header_prefixes) or " | " in line or stripped.startswith(("M ", "A ", "D ", "R ", "?? ")):
            kept.append(line)
        else:
            other.append(line)

    candidate = "\n".join(kept[:120])
    if other and len(candidate) < int(budget * 0.70):
        rem = budget - len(candidate) - 40
        preview = "\n".join(other)[: max(0, rem)]
        if preview:
            candidate = (candidate + "\n" + preview) if candidate else preview
    if len(candidate) > budget:
        candidate, _ = _head_tail_slice(candidate, budget, head_ratio=0.50)
    return candidate


def _reduce_diff(text: str, budget: int) -> str:
    """Keep per-file diff headers and bounded hunk previews; preserve tail."""
    lines = text.splitlines()
    kept: List[str] = []
    hunk_lines_in_file = 0
    omitted_in_file = 0
    max_lines_per_file = 40

    for line in lines:
        if line.startswith(("diff --git ", "--- ", "+++ ", "@@ ")):
            if omitted_in_file > 0:
                kept.append(f"[... {omitted_in_file} diff lines omitted in previous file ...]")
                omitted_in_file = 0
            if line.startswith("diff --git "):
                hunk_lines_in_file = 0
            kept.append(line)
        else:
            if hunk_lines_in_file < max_lines_per_file:
                kept.append(line)
                hunk_lines_in_file += 1
            else:
                omitted_in_file += 1

    if omitted_in_file > 0:
        kept.append(f"[... {omitted_in_file} diff lines omitted in last file ...]")

    candidate = "\n".join(kept)
    if len(candidate) <= budget:
        return candidate
    body, _ = _head_tail_slice(candidate, budget, head_ratio=0.40)
    return body


def _reduce_search(text: str, budget: int, max_per_file: int = 5) -> str:
    """Cap grep/search matches per file and append match/file totals."""
    lines = text.splitlines()
    per_file_counts: Dict[str, int] = {}
    kept: List[str] = []
    total_matches = 0

    for line in lines:
        m = _SEARCH_MATCH_LINE_RE.match(line)
        if m:
            fpath = m.group(1)
            total_matches += 1
            cnt = per_file_counts.get(fpath, 0) + 1
            per_file_counts[fpath] = cnt
            if cnt <= max_per_file:
                kept.append(line)
        else:
            kept.append(line)

    if total_matches > 0 and per_file_counts:
        kept.append(
            f"[search summary: {total_matches} total matches across "
            f"{len(per_file_counts)} files; showing up to {max_per_file} matches/file]"
        )

    candidate = "\n".join(kept)
    if len(candidate) <= budget:
        return candidate
    body, _ = _head_tail_slice(candidate, budget, head_ratio=0.40)
    return body


def _reduce_json(text: str, budget: int) -> str:
    body, _ = _head_tail_slice(text, budget, head_ratio=0.40)
    return body


def _reduce_generic(text: str, budget: int) -> str:
    body, _ = _head_tail_slice(text, budget, head_ratio=0.40)
    return body


def reduce_tool_output(
    text: str,
    *,
    tool_name: str = "",
    args: Optional[Mapping[str, Any]] = None,
    max_chars: int = DEFAULT_READ_LIMIT,
    store: Optional[ToolOutputStore] = None,
    field_name: str = "",
) -> Tuple[str, Optional[str], str]:
    """Reduce *text* deterministically when ``len(text) > max_chars``.

    Returns ``(reduced_text, handle_or_none, content_type)``.
    """
    ceiling = max(256, int(max_chars))
    if len(text) <= ceiling:
        return text, None, "inline"

    content_type = classify_tool_output(tool_name, args, text)
    handle = (
        store.store(
            text,
            tool_name=tool_name,
            field_name=field_name,
            content_type=content_type,
        )
        if store is not None
        else None
    )

    marker_reserve = 180
    body_budget = max(128, ceiling - marker_reserve)

    if content_type == "validation":
        reduced_body = _reduce_validation(text, body_budget)
    elif content_type == "git":
        reduced_body = _reduce_git(text, body_budget)
    elif content_type == "diff":
        reduced_body = _reduce_diff(text, body_budget)
    elif content_type == "search":
        reduced_body = _reduce_search(text, body_budget)
    elif content_type == "json":
        reduced_body = _reduce_json(text, body_budget)
    else:
        reduced_body = _reduce_generic(text, body_budget)

    omitted = max(0, len(text) - len(reduced_body))
    marker = _format_marker(omitted, content_type, handle)
    return reduced_body + marker, handle, content_type


def reduce_tool_result(
    result: Any,
    *,
    tool_name: str = "",
    args: Optional[Mapping[str, Any]] = None,
    max_chars: int = DEFAULT_READ_LIMIT,
    store: Optional[ToolOutputStore] = None,
    _depth: int = 0,
    _field_name: str = "",
) -> Any:
    """Recursively reduce oversized string fields in a tool result (max depth 6)."""
    if _depth > 6:
        return result
    if isinstance(result, str):
        if len(result) > max_chars:
            reduced, _, _ = reduce_tool_output(
                result,
                tool_name=tool_name,
                args=args,
                max_chars=max_chars,
                store=store,
                field_name=_field_name,
            )
            return reduced
        return result
    if isinstance(result, dict):
        return {
            k: reduce_tool_result(
                v,
                tool_name=tool_name,
                args=args,
                max_chars=max_chars,
                store=store,
                _depth=_depth + 1,
                _field_name=str(k),
            )
            for k, v in result.items()
        }
    if isinstance(result, list):
        return [
            reduce_tool_result(
                v,
                tool_name=tool_name,
                args=args,
                max_chars=max_chars,
                store=store,
                _depth=_depth + 1,
                _field_name=_field_name,
            )
            for v in result
        ]
    return result


def is_surgical_preview_edit_allowed(
    orch: Any,
    tool_name: str,
    args: Mapping[str, Any],
    resolved_path: str,
) -> bool:
    """Return True ONLY when a surgical edit targets text actually shown in a read-guard preview.

    Blind full-file overwrites (``write_file``) are NEVER satisfied by a read-guard preview.
    """
    if tool_name not in ("edit_file", "edit_file_atomic", "edit_code_block"):
        return False
    previews = getattr(orch, "_session_previewed_files", None)
    if not isinstance(previews, dict):
        return False
    preview_text = previews.get(resolved_path)
    if not isinstance(preview_text, str) or not preview_text:
        return False
    needle = (
        args.get("old_string")
        or args.get("old_text")
        or args.get("search")
        or args.get("target")
    )
    if isinstance(needle, str) and needle.strip() and needle in preview_text:
        return True
    return False
