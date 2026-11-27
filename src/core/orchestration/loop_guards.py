"""Deterministic 9B loop guards: ReadDedupCache, closest-tool hints, plan anchor, retry temp (TASK-34)."""

from __future__ import annotations

import difflib
import json
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from src.core.context.workspace_bootstrap import build_workspace_bootstrap_summary

PURE_READ_DEDUP_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "grep",
        "list_files",
        "find_files",
        "find_symbol",
        "glob",
    }
)

IDEMPOTENT_STATE_READ_TOOLS: frozenset[str] = frozenset({"manage_todo"})

INVALIDATING_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "multiedit",
        "apply_patch",
        "delete_file",
        "rename_file",
        "create_directory",
        "ast_rename",
        "bash",
        "run_bash",
        "execute_bash",
        "run_command",
        "execute_command",
        "run_tests",
        "git_commit",
        "git_restore",
        "git_stash",
        "revert_last_tool",
    }
)

_RETRY_TEMP_DELTAS: Tuple[float, ...] = (-0.15, +0.15, 0.0)


def _canonical_args_key(args: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(args or {}), sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        return str(args)


class ReadDedupCache:
    """Sliding-window deduplication cache for pure read-only tools (window=6).

    - Caches identical ``(canonical_tool, sorted_args)`` results for pure read tools
      so small models do not waste turns re-reading unchanged files/searches.
    - Completely invalidated on any mutating tool or ``bash`` call.
    - Separately deduplicates idempotent ``manage_todo(action="read")`` calls
      within the same turn.
    """

    def __init__(self, max_entries: int = 6) -> None:
        self._max_entries = max(1, int(max_entries))
        self._read_cache: OrderedDict[Tuple[str, str], Dict[str, Any]] = OrderedDict()
        self._turn_id: int = -1
        self._turn_state_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def begin_turn(self, turn_id: int) -> None:
        if turn_id != self._turn_id:
            self._turn_id = turn_id
            self._turn_state_cache.clear()

    def invalidate(self) -> None:
        """Clear all cached entries (called on any workspace mutation or bash call)."""
        self._read_cache.clear()
        self._turn_state_cache.clear()

    def is_invalidating(self, tool_name: str) -> bool:
        return tool_name in INVALIDATING_TOOLS

    def _is_same_turn_state_read(self, tool_name: str, args: Mapping[str, Any]) -> bool:
        if tool_name in IDEMPOTENT_STATE_READ_TOOLS:
            action = str((args or {}).get("action", "")).strip().lower()
            return action in ("read", "list", "get", "")
        return False

    def get(self, tool_name: str, args: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        key = (tool_name, _canonical_args_key(args))
        if tool_name in PURE_READ_DEDUP_TOOLS:
            hit = self._read_cache.get(key)
            if hit is not None:
                self._read_cache.move_to_end(key)
                return dict(hit)
            return None
        if self._is_same_turn_state_read(tool_name, args):
            hit = self._turn_state_cache.get(key)
            return dict(hit) if hit is not None else None
        return None

    def put(self, tool_name: str, args: Mapping[str, Any], result: Dict[str, Any]) -> None:
        if self.is_invalidating(tool_name):
            self.invalidate()
            return
        if not isinstance(result, dict):
            return
        if result.get("ok") is False or result.get("status") == "error":
            return
        key = (tool_name, _canonical_args_key(args))
        if tool_name in PURE_READ_DEDUP_TOOLS:
            self._read_cache[key] = dict(result)
            self._read_cache.move_to_end(key)
            while len(self._read_cache) > self._max_entries:
                self._read_cache.popitem(last=False)
        elif self._is_same_turn_state_read(tool_name, args):
            self._turn_state_cache[key] = dict(result)


def get_or_create_read_dedup_cache(orch: Any) -> ReadDedupCache:
    cache = getattr(orch, "_read_dedup_cache", None)
    if not isinstance(cache, ReadDedupCache):
        cache = ReadDedupCache(max_entries=6)
        if orch is not None:
            try:
                setattr(orch, "_read_dedup_cache", cache)
            except Exception:
                pass
    return cache


def suggest_closest_tool(unknown_name: str, available_tools: Iterable[str]) -> str:
    """Return closest registered tool name suggestion(s) via ``difflib.get_close_matches``."""
    if not unknown_name:
        return ""
    pool = sorted({str(t) for t in available_tools if t and "." not in str(t)})
    if not pool:
        return ""
    matches = difflib.get_close_matches(unknown_name, pool, n=3, cutoff=0.5)
    if not matches:
        return ""
    return f" Did you mean: {', '.join(matches)}?"


def format_unknown_tool_error(name: str, orch: Any = None) -> str:
    """Build an unknown-tool error message with closest-match recovery hint."""
    available: List[str] = []
    reg = getattr(orch, "tool_registry", None) if orch is not None else None
    if reg is not None:
        if hasattr(reg, "list") and callable(reg.list):
            available = list(reg.list())
        elif hasattr(reg, "tools") and isinstance(reg.tools, dict):
            available = list(reg.tools.keys())
    hint = suggest_closest_tool(name, available)
    return f"Tool '{name}' not found.{hint}"


def render_active_plan_anchor(state: Mapping[str, Any]) -> str:
    """Render a compact 4-6 line ``ACTIVE PLAN`` anchor when >= 2 plan/todo steps exist."""
    raw_plan = state.get("current_plan")
    steps: List[Tuple[str, bool]] = []
    if isinstance(raw_plan, list) and len(raw_plan) >= 2:
        for item in raw_plan:
            if isinstance(item, dict):
                desc = str(item.get("description") or item.get("action") or item.get("title") or "").strip()
                done = bool(item.get("completed") or item.get("status") == "completed")
                if desc:
                    steps.append((desc, done))
            elif isinstance(item, str) and item.strip():
                steps.append((item.strip(), False))
    if len(steps) < 2:
        return ""

    total = len(steps)
    cur_idx = int(state.get("current_step", 0) or 0)
    # Clamp into [0, total - 1]
    cur_idx = max(0, min(cur_idx, total - 1))
    lines = [f"ACTIVE PLAN (step {cur_idx + 1} of {total}):"]
    # Show up to 4 steps around current_step so anchor stays compact (4-6 lines total)
    start = max(0, min(cur_idx, total - 4))
    window = steps[start : start + 4]
    for offset, (desc, done) in enumerate(window):
        idx = start + offset
        marker = "[x]" if done else ("[>]" if idx == cur_idx else "[ ]")
        lines.append(f"  {idx + 1}. {marker} {desc[:100]}")
    if start + 4 < total:
        lines.append(f"  ... (+{total - (start + 4)} more step(s))")
    return "\n".join(lines)


def compute_retry_temperature(base_temp: float, retry_idx: int) -> float:
    """Apply adaptive retry temperature delta for verification fix attempts (retry 1, 2, 3)."""
    if retry_idx <= 0:
        return max(0.0, min(1.0, float(base_temp)))
    delta = _RETRY_TEMP_DELTAS[(retry_idx - 1) % len(_RETRY_TEMP_DELTAS)]
    return round(max(0.0, min(1.0, float(base_temp) + delta)), 4)


def cap_consecutive_invalid_turns(consecutive_count: int, limit: int = 2) -> bool:
    """Return True if consecutive empty/invalid-turn steers have hit the cap (default 2)."""
    return int(consecutive_count or 0) >= max(1, int(limit))


def compact_dedup_hit(
    tool_name: str,
    tool_args: Mapping[str, Any],
    cached: Dict[str, Any],
    *,
    max_repeat_chars: int = 400,
) -> Dict[str, Any]:
    """Return a deduplicated tool result, compacting large unchanged ``read_file`` repeats."""
    out = dict(cached)
    out["dedup_cache_hit"] = True
    if tool_name != "read_file":
        return out
    path_label = str(tool_args.get("path") or out.get("path") or "file")
    notice = (
        f"[Already read unchanged in this session ({path_label}); "
        "refer to the earlier read_file output in conversation history.]"
    )
    if isinstance(out.get("content"), str) and len(out["content"]) > max_repeat_chars:
        out["content"] = notice
    inner = out.get("result")
    if isinstance(inner, dict) and isinstance(inner.get("content"), str) and len(inner["content"]) > max_repeat_chars:
        out["result"] = {**inner, "content": notice}
    return out


def extract_finish_reason(response: Any) -> str:
    """Extract normalized ``finish_reason`` from an OpenAI/Anthropic/Gemini response dict."""
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            fr = choices[0].get("finish_reason") or choices[0].get("stop_reason")
            if isinstance(fr, str) and fr:
                return fr.strip().lower()
        fr_top = response.get("finish_reason") or response.get("stop_reason")
        if isinstance(fr_top, str) and fr_top:
            return fr_top.strip().lower()
    else:
        fr_attr = getattr(response, "finish_reason", None) or getattr(response, "stop_reason", None)
        if isinstance(fr_attr, str) and fr_attr:
            return fr_attr.strip().lower()
    return ""


def build_truncated_tool_call_feedback() -> str:
    """Return corrective guidance when completion hits max_tokens mid-tool-call."""
    return (
        "<system_reminder>\n"
        "Your previous response was truncated at the output token limit before completing "
        "the tool call. Do NOT repeat the same oversized write. Split large file edits into "
        "smaller chunks (e.g., write a minimal skeleton with write_file and apply focused "
        "edit_file_atomic patches).\n"
        "</system_reminder>"
    )


def detect_degenerate_repetition(text: str, *, min_repeats: int = 4, min_line_len: int = 12) -> bool:
    """Return True when model output degrades into repeated identical lines."""
    if not text or len(text) < min_repeats * min_line_len:
        return False
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= min_line_len]
    if len(lines) < min_repeats:
        return False
    run = 1
    for idx in range(1, len(lines)):
        if lines[idx] == lines[idx - 1]:
            run += 1
            if run >= min_repeats:
                return True
        else:
            run = 1
    return False


def build_degenerate_repetition_notice() -> str:
    """Return corrective steering when a turn degrades into repeated output lines."""
    return (
        "<system_reminder>\n"
        "Your previous response entered a repetitive output loop. Stop repeating text. "
        "Emit either one concrete tool call that makes progress or a concise 1–2 sentence factual summary.\n"
        "</system_reminder>"
    )


def build_incomplete_turn_nudge(
    err_code: str,
    error_msg: str,
    *,
    response: Any = None,
    content_text: str = "",
) -> str:
    """Build an explicit tool-action steering reminder when a model stops prematurely."""
    if extract_finish_reason(response) in ("length", "max_tokens"):
        return build_truncated_tool_call_feedback()
    if detect_degenerate_repetition(content_text):
        return build_degenerate_repetition_notice()
    if err_code in ("unverified_text_only_completion", "unverified_mutation_missing"):
        return (
            f"<system_reminder>\nIncomplete turn ({err_code}): {error_msg}\n"
            "Do NOT reply with prose or markdown code fences. Invoke the appropriate "
            "file tool now (e.g. write_file(path=..., content=...) to create a file or "
            "edit_file_atomic(path=..., old_string=..., new_string=...) to edit an existing file).\n"
            "</system_reminder>"
        )
    return (
        f"<system_reminder>\nIncomplete turn ({err_code}): {error_msg}\n"
        "Call the appropriate tool to complete the requested work before finishing.\n"
        "</system_reminder>"
    )


def augment_turn_messages(
    messages: List[Dict[str, Any]],
    state: Mapping[str, Any],
    turns_taken: int,
) -> List[Dict[str, Any]]:
    """Inject Turn-1 workspace bootstrap summary and active plan anchor non-destructively."""
    out = list(messages)
    additions: List[str] = []
    if turns_taken == 0:
        wd = state.get("working_dir")
        if wd:
            boot = build_workspace_bootstrap_summary(str(wd))
            if boot:
                additions.append(boot)
    anchor = render_active_plan_anchor(state)
    if anchor:
        additions.append(anchor)
    if not additions:
        return out
    block = "\n\n".join(additions)
    # Attach to the last user message if present, else append as a user guidance block
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user" and isinstance(out[i].get("content"), str):
            out[i] = {**out[i], "content": f"{block}\n\n{out[i]['content']}"}
            return out
    out.append({"role": "user", "content": block})
    return out
