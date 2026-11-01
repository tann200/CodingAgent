"""Wave-based tool dispatch, native tool-call history builders, and bash mutation tracking.

Extracted to keep ``frontier_loop_node.py`` modular and well within the node
line-count budget while enabling:
- Bounded parallel execution of contiguous read-only tool waves
- Canonical structured ``assistant.tool_calls`` + ``role="tool"`` history (1x output)
- Signed ``thinking_blocks`` / ``reasoning_content`` preservation across turns
- Workspace file mutation detection across ``bash`` invocations
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src.core.orchestration.graph.completion_contract import _MODIFYING_TOOL_NAMES
from src.core.orchestration.graph.nodes.frontier_toolcalls import _normalize_tool_call
from src.core.orchestration.graph.policy import compute_action_digest
from src.tools._tool import PermissionKind

_PARALLEL_SAFE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_file_chunk",
        "read_file_bytes",
        "batched_file_read",
        "files_read",
        "fs.read",
        "list_files",
        "glob",
        "grep",
        "find_symbol",
        "search_code",
        "git_status",
        "git_diff",
        "get_git_diff",
        "git_log",
        "lsp_diagnostics",
        "lsp_references",
        "lsp_definition",
        "check_background_task",
    }
)

_NEVER_PARALLEL_TOOLS: frozenset[str] = frozenset(
    set(_MODIFYING_TOOL_NAMES)
    | {
        "ask_user",
        "delegate_task",
        "manage_todo",
        "load_skill",
        "run_tests",
        "run_linter",
        "run_js_tests",
        "run_ts_check",
    }
)

_IGNORED_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".codingAgent",
        ".codingagent",
        ".agent-context",
        "target",
        "dist",
        "build",
    }
)
_MAX_SCAN_FILES = 2500


def is_parallel_safe_readonly_tool(tool_name: str, orchestrator: Any = None) -> bool:
    """Return True when *tool_name* is pure read-only and safe to run in a parallel wave."""
    if not tool_name or tool_name in _NEVER_PARALLEL_TOOLS:
        return False
    if tool_name in _PARALLEL_SAFE_READ_TOOLS:
        return True
    reg = getattr(orchestrator, "tool_registry", None) if orchestrator else None
    if reg is not None and hasattr(reg, "get_permission_kind") and callable(reg.get_permission_kind):
        try:
            return reg.get_permission_kind(tool_name) == PermissionKind.READ_FILE
        except Exception:
            return False
    return False


def normalize_tool_calls_with_ids(
    tool_calls: List[Any],
    turn_index: int,
) -> List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]]:
    """Normalize tool calls into ``(call_id, tool_name, tool_args, openai_tc_dict)``."""
    normalized: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
    for idx, tc in enumerate(tool_calls):
        tool_name, tool_args = _normalize_tool_call(tc)
        if not tool_name:
            continue
        raw_id = ""
        if isinstance(tc, dict):
            raw_id = str(tc.get("id") or tc.get("tool_call_id") or "").strip()
        else:
            raw_id = str(getattr(tc, "id", "") or "").strip()
        call_id = raw_id or f"call_{turn_index}_{idx + 1}"
        openai_tc = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(tool_args, ensure_ascii=False, default=str),
            },
        }
        normalized.append((call_id, tool_name, tool_args, openai_tc))
    return normalized


def partition_tool_call_waves(
    normalized_calls: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]],
    orchestrator: Any,
    max_parallel_reads: int = 1,
) -> List[Tuple[bool, List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]]]]:
    """Partition normalized tool calls into ordered execution waves.

    Contiguous parallel-safe read-only calls are grouped into waves bounded by
    ``max_parallel_reads`` (when ``> 1``). Mutating, shell, and interactive
    calls always form single-element serial barrier waves.
    """
    waves: List[Tuple[bool, List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]]]] = []
    read_buffer: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = []
    eff_limit = max(1, int(max_parallel_reads or 1))

    def _flush_reads() -> None:
        nonlocal read_buffer
        if not read_buffer:
            return
        if eff_limit <= 1 or len(read_buffer) == 1:
            for item in read_buffer:
                waves.append((False, [item]))
        else:
            for start in range(0, len(read_buffer), eff_limit):
                chunk = read_buffer[start : start + eff_limit]
                waves.append((len(chunk) > 1, chunk))
        read_buffer = []

    for item in normalized_calls:
        _, tool_name, _, _ = item
        if eff_limit > 1 and is_parallel_safe_readonly_tool(tool_name, orchestrator):
            read_buffer.append(item)
        else:
            _flush_reads()
            waves.append((False, [item]))

    _flush_reads()
    return waves


def extract_response_thinking(
    response: Any,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Extract ``(thinking_blocks, reasoning_content)`` from an adapter response."""
    thinking_blocks: Optional[List[Dict[str, Any]]] = None
    reasoning_content: Optional[str] = None

    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict):
                tb = msg.get("thinking_blocks")
                if isinstance(tb, list) and tb:
                    thinking_blocks = tb
                rc = msg.get("reasoning_content")
                if isinstance(rc, str) and rc:
                    reasoning_content = rc
        if thinking_blocks is None:
            top_tb = response.get("thinking_blocks")
            if isinstance(top_tb, list) and top_tb:
                thinking_blocks = top_tb
        if reasoning_content is None:
            top_rc = response.get("reasoning_content")
            if isinstance(top_rc, str) and top_rc:
                reasoning_content = top_rc
    else:
        tb = getattr(response, "thinking_blocks", None)
        if isinstance(tb, list) and tb:
            thinking_blocks = tb
        rc = getattr(response, "reasoning_content", None)
        if isinstance(rc, str) and rc:
            reasoning_content = rc

    return thinking_blocks, reasoning_content


def build_native_assistant_message(
    raw_content: str,
    openai_tool_calls: List[Dict[str, Any]],
    response: Any = None,
) -> Dict[str, Any]:
    """Construct a structured OpenAI-protocol assistant message without duplicating JSON in content."""
    msg: Dict[str, Any] = {
        "role": "assistant",
        "content": raw_content or "",
    }
    if openai_tool_calls:
        msg["tool_calls"] = openai_tool_calls
    thinking_blocks, reasoning_content = extract_response_thinking(response)
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    return msg


def build_native_tool_message(
    tool_call_id: str,
    tool_name: str,
    sanitized_result: Dict[str, Any],
    truncate_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Construct a single ``role='tool'`` result message with truncated payload."""
    truncated = truncate_fn(sanitized_result)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": json.dumps(truncated, ensure_ascii=False, default=str),
    }


def compute_tool_call_signature(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Compute a deterministic signature for duplicate-failure loop detection."""
    return f"{tool_name}:{compute_action_digest(tool_name, tool_args)}"


def is_tool_result_failure(result: Any) -> bool:
    """Return True when a tool result dictionary represents an execution error."""
    if not isinstance(result, dict):
        return True
    if result.get("ok") is False or result.get("status") in ("error", "failed"):
        return True
    inner = result.get("result")
    if isinstance(inner, dict):
        if inner.get("ok") is False or inner.get("status") in ("error", "failed"):
            return True
        rc = inner.get("returncode")
        if rc is not None and rc != 0:
            return True
    return False


def capture_workspace_file_state(workdir: str) -> Dict[str, Tuple[int, int]]:
    """Capture bounded ``{resolved_path: (mtime_ns, size)}`` snapshot before ``bash``."""
    if not workdir:
        return {}
    root = Path(workdir)
    if not root.is_dir():
        return {}
    snap: Dict[str, Tuple[int, int]] = {}
    scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_SCAN_DIRS]
            for fname in filenames:
                scanned += 1
                if scanned > _MAX_SCAN_FILES:
                    return snap
                fpath = Path(dirpath) / fname
                try:
                    st = fpath.stat()
                    snap[str(fpath.resolve())] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
    except OSError:
        return snap
    return snap


def detect_workspace_mutations(
    workdir: str,
    before: Dict[str, Tuple[int, int]],
) -> List[str]:
    """Return resolved paths created or modified in *workdir* relative to *before*."""
    if not workdir or not before and not Path(workdir).is_dir():
        return []
    after = capture_workspace_file_state(workdir)
    changed: List[str] = []
    for path_str, meta in after.items():
        if before.get(path_str) != meta:
            changed.append(path_str)
    return changed


async def execute_raw_tool(
    orchestrator: Any,
    action: Dict[str, Any],
    model_tier: str,
    run_async: bool,
    log: Any = None,
) -> Any:
    """Execute one tool action through ``orchestrator.execute_tool``."""
    import asyncio

    if not orchestrator:
        return {"ok": False, "error": "orchestrator unavailable"}
    try:
        if run_async or model_tier in ("large", "frontier", "medium"):
            from src.core.orchestration.event_bus import run_with_correlation

            loop = asyncio.get_running_loop()
            return await run_with_correlation(loop, None, orchestrator.execute_tool, action)
        return orchestrator.execute_tool(action)
    except Exception as exc:
        if log is not None:
            log.warning("frontier_loop_node: tool '%s' raised: %s", action.get("name"), exc)
        return {"ok": False, "error": str(exc)}


def record_bash_mutations(orchestrator: Any, workdir: str, before: Dict[str, Tuple[int, int]]) -> None:
    """Detect files created/modified during a ``bash`` call and record on orchestrator."""
    if not workdir or orchestrator is None or not hasattr(orchestrator, "_session_modified_files"):
        return
    changed_paths = detect_workspace_mutations(workdir, before)
    if not changed_paths:
        return
    lock = getattr(orchestrator, "_state_lock", None)
    if lock is not None and hasattr(lock, "__enter__"):
        with lock:
            orchestrator._session_modified_files.update(changed_paths)
    else:
        orchestrator._session_modified_files.update(changed_paths)


def build_read_then_write_result(
    *,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    tool_name: str,
    path_arg: str | None,
    working_dir: str,
    truncate_tool_output: Callable[[dict], dict],
    tool_last_used: Mapping[str, Any],
    files_read: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Format single-copy read-then-write result for legacy tool calling mode."""
    actual_result = result.get("result", {})
    if (
        tool_name not in ("read_file", "fs.read")
        or not path_arg
        or not isinstance(actual_result, dict)
    ):
        return None

    status = actual_result.get("status")
    if status is None:
        if actual_result.get("items") is not None or "content" in actual_result:
            status = "ok"
    if status != "ok":
        return None

    resolved = str((Path(working_dir) / path_arg).resolve())
    task = (state.get("task") or "").lower()
    modification_keywords = (
        "add ",
        "prepend",
        "append",
        "edit ",
        "modify",
        "update ",
        "change ",
        "replace ",
        "insert ",
        "delete ",
        "remove ",
        "top of ",
        "beginning of ",
        "after ",
        "before ",
        "on top of ",
        "inside ",
        "contents of ",
        "fix ",
        "create ",
        "write ",
        "implement",
        "correct",
        "repair",
        "patch",
    )
    task_implies_write = any(keyword in task for keyword in modification_keywords)

    if not task_implies_write:
        return {
            "verified_reads": [resolved],
            "files_read": {**dict(files_read), resolved: True},
        }

    enhanced_context = (
        f"Context: You just read '{path_arg}'. "
        "Apply minimal targeted edits with edit_file_atomic (or write_file for new files)."
    )
    new_messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "tool_execution_result": truncate_tool_output(dict(result)),
                    "orchestration_hint": "write_required",
                    "file_path": path_arg,
                    "enhanced_context": enhanced_context,
                }
            ),
        }
    ]
    return {
        "last_result": result,
        "last_tool_name": tool_name,
        "verified_reads": [resolved],
        "history": new_messages,
        "next_action": None,
        "tool_call_count": int(state.get("tool_call_count") or 0) + 1,
        "tool_last_used": dict(tool_last_used),
        "files_read": {**dict(files_read), resolved: True},
    }


def build_tool_history_messages(
    *,
    result: Mapping[str, Any],
    truncate_tool_output: Callable[[dict], dict],
) -> list[dict[str, str]]:
    """Format single-copy legacy tool output message."""
    return [
        {
            "role": "user",
            "content": json.dumps(
                {"tool_execution_result": truncate_tool_output(dict(result))}
            ),
        }
    ]


def append_tool_result_to_history(
    *,
    history: List[Dict[str, Any]],
    use_native_tools: bool,
    call_id: str,
    tool_name: str,
    tool_args: Dict[str, Any],
    tool_result: Dict[str, Any],
    state: Any,
    tool_call_count: int,
    truncate_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> None:
    """Append either a native ``role='tool'`` turn or single-copy legacy tool result."""
    path_arg = None
    if isinstance(tool_args, dict):
        raw_path = tool_args.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            path_arg = raw_path

    files_read = dict(state.get("files_read") or {})
    tool_last_used = dict(state.get("tool_last_used") or {})
    if path_arg and tool_name in ("read_file", "fs.read", "read_file_chunk", "files_read"):
        try:
            resolved_path = str((Path(str(state.get("working_dir") or ".")) / path_arg).resolve())
            files_read[resolved_path] = True
            tool_last_used[f"{tool_name}:{path_arg}"] = tool_call_count
        except Exception:
            pass

    if use_native_tools:
        history.append(build_native_tool_message(call_id, tool_name, tool_result, truncate_fn))
        return

    read_then_write_result = build_read_then_write_result(
        state={**state, "tool_call_count": tool_call_count - 1},
        result=tool_result,
        tool_name=tool_name,
        path_arg=path_arg,
        working_dir=str(state.get("working_dir") or "."),
        truncate_tool_output=truncate_fn,
        tool_last_used=tool_last_used,
        files_read=files_read,
    )
    if read_then_write_result and read_then_write_result.get("history"):
        history.extend(list(read_then_write_result.get("history") or []))
    else:
        history.extend(
            build_tool_history_messages(result=tool_result, truncate_tool_output=truncate_fn)
        )
