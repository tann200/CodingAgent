"""execution_plan.py — Plan step computation and payload builders.

Extracted from execution_helpers.py (P3-4) for improved modularity.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


def compute_execution_post_tool_updates(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    tool_name: str,
    result: Mapping[str, Any],
    modifying_tools: Sequence[str],
) -> Dict[str, Any]:
    """Compute the post-tool bookkeeping updates merged into the final payload."""
    tool_call_count = int(state.get("tool_call_count") or 0) + 1
    no_plan_fail_update = compute_no_plan_fail_update(state=state, result=result)
    plan_approval_consumed = compute_plan_approval_consumed(
        state=state,
        tool_name=tool_name,
        result=result,
        modifying_tools=modifying_tools,
    )
    affected_files_update = compute_affected_files_update(
        tool_name=tool_name,
        result=result,
        state=state,
        orchestrator=orchestrator,
    )
    plan_exit_update = compute_plan_exit_update(orchestrator=orchestrator)
    return {
        "tool_call_count": tool_call_count,
        "no_plan_fail_update": no_plan_fail_update,
        "plan_approval_consumed": plan_approval_consumed,
        "affected_files_update": affected_files_update,
        "plan_exit_update": plan_exit_update,
    }


def compute_plan_step_updates(
    *,
    result: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    original_task: Optional[str],
    execution_waves: Optional[Sequence[Any]],
    current_wave: int,
    step_retry_counts: Mapping[str, Any],
    max_step_retries: int = 3,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    plan_advance: Dict[str, Any] = {}
    wave_advance: Dict[str, Any] = {}

    if not current_plan or current_step >= len(current_plan):
        return plan_advance, wave_advance

    _ok_flag = result.get("ok")
    execution_ok = (_ok_flag is True) or (_ok_flag is None and result.get("status") == "ok")
    if not execution_ok:
        return plan_advance, wave_advance

    updated_plan = [dict(step) for step in current_plan]
    updated_plan[current_step]["completed"] = True
    next_step = current_step + 1

    if execution_waves and current_wave < len(execution_waves):
        wave_step_ids = execution_waves[current_wave]
        step_id_str = str(current_step)
        if step_id_str in wave_step_ids or current_step in wave_step_ids:
            all_in_wave_complete = True
            for wave_step in wave_step_ids:
                wave_idx = (
                    int(wave_step.split("_")[-1])
                    if isinstance(wave_step, str) and wave_step.startswith("step_")
                    else wave_step
                )
                if isinstance(wave_idx, str):
                    try:
                        wave_idx = int(wave_idx.replace("step_", ""))
                    except (ValueError, AttributeError):
                        wave_idx = wave_step
                if isinstance(wave_idx, int) and wave_idx < len(updated_plan):
                    step_done = updated_plan[wave_idx].get("completed")
                    step_retries = int(step_retry_counts.get(str(wave_idx), 0))
                    step_retry_exhausted = step_retries >= max_step_retries
                    if not step_done and not step_retry_exhausted:
                        all_in_wave_complete = False
                        break

            if all_in_wave_complete:
                wave_advance = {"current_wave": current_wave + 1}

    if next_step < len(updated_plan):
        plan_advance = {
            "current_step": next_step,
            "current_plan": updated_plan,
            "task": updated_plan[next_step].get("description", ""),
        }
    else:
        plan_advance = {
            "current_step": next_step,
            "current_plan": updated_plan,
            "task": original_task or "Task complete",
        }

    return plan_advance, wave_advance


def compute_no_plan_fail_update(
    *, state: Mapping[str, Any], result: Mapping[str, Any]
) -> Dict[str, Any]:
    if state.get("current_plan"):
        return {}

    _ok_flag = result.get("ok")
    execution_ok = (_ok_flag is True) or (_ok_flag is None and result.get("status") == "ok")
    is_format_error = "format_error" in str(result.get("error", ""))
    if execution_ok:
        return {"no_plan_fail_count": 0}
    if not is_format_error:
        return {"no_plan_fail_count": int(state.get("no_plan_fail_count") or 0) + 1}
    return {}


def compute_plan_approval_consumed(
    *,
    state: Mapping[str, Any],
    tool_name: str,
    result: Mapping[str, Any],
    modifying_tools: Sequence[str],
) -> Dict[str, Any]:
    if (
        state.get("plan_mode_approved")
        and tool_name in modifying_tools
        and ((result.get("ok") is True) or (result.get("ok") is None and result.get("status") == "ok"))
    ):
        return {"plan_mode_approved": False}
    return {}


def compute_affected_files_update(
    *,
    tool_name: str,
    result: Mapping[str, Any],
    state: Mapping[str, Any],
    orchestrator: Any,
) -> Dict[str, Any]:
    affected_files_update: Dict[str, Any] = {}
    if (
        tool_name == "ask_user"
        and result.get("status") == "ok"
        and state.get("affected_files") is not None
    ):
        approval_kw = re.compile(
            r"\b(yes\s+all|approve\s+all|expand\s+scope|allow\s+all|unrestrict)\b",
            re.IGNORECASE,
        )
        file_pat = re.compile(
            r"\b([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|yaml|yml|json|md|txt|toml|cfg|ini|sh|bash|html|css|scss|sql))\b"
        )
        answer_text = str(result.get("answer") or "")
        if approval_kw.search(answer_text):
            affected_files_update = {"affected_files": []}
        else:
            new_paths = []
            seen_paths = set(state.get("affected_files") or [])
            for match in file_pat.finditer(answer_text):
                path = match.group(1)
                if path not in seen_paths and not path.startswith(".."):
                    seen_paths.add(path)
                    new_paths.append(path)
            if new_paths:
                expanded = list(state.get("affected_files") or []) + new_paths
                affected_files_update = {"affected_files": expanded}
                if orchestrator:
                    orchestrator._affected_files = expanded

    return affected_files_update


def build_read_then_write_result(
    *,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
    tool_name: str,
    path_arg: Optional[str],
    working_dir: str,
    truncate_tool_output: Callable[[dict], dict],
    tool_last_used: Mapping[str, Any],
    files_read: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
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
        f"Task: {state.get('task')}\n"
        f"Context: You just read the file '{path_arg}'.\n"
        f"File contents:\n{actual_result.get('content', '')}\n"
        f"Today's date: {date.today().isoformat()}\n"
        f"Use write_file tool to write the updated content based on the task above."
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
) -> List[Dict[str, str]]:
    return [
        {
            "role": "user",
            "content": json.dumps(
                {"tool_execution_result": truncate_tool_output(dict(result))}
            ),
        }
    ]


def compute_replan_trigger(*, result: Mapping[str, Any]) -> Dict[str, Any]:
    if result.get("requires_split") is True:
        error_msg = result.get(
            "error", "Patch exceeded 200 lines. Split into multiple targeted functions."
        )
        return {
            "replan_required": error_msg,
            "action_failed": True,
            "next_action": None,
        }
    return {}


def compute_plan_progress_payload(
    *,
    state: Mapping[str, Any],
    current_plan: Optional[List[Dict[str, Any]]],
    current_step: int,
    execution_ok: bool,
) -> Dict[str, Any]:
    if not current_plan or current_step >= len(current_plan):
        return {}
    step_desc = current_plan[current_step].get("description", "Unknown step")
    progress_payload = {
        "sessionUpdate": "plan_progress",
        "planId": f"plan_{state.get('session_id', 'default')}",
        "currentStep": current_step + 1,
        "totalSteps": len(current_plan),
        "stepDescription": step_desc,
        "status": "completed" if execution_ok else "in_progress",
    }
    return {"plan_progress": progress_payload}


def compute_plan_exit_update(*, orchestrator: Any) -> Dict[str, Any]:
    if not orchestrator:
        return {}
    committed = getattr(orchestrator, "_committed_plan_steps", None)
    if committed:
        setattr(orchestrator, "_committed_plan_steps", None)
        setattr(orchestrator, "_plan_mode_approved", True)
        return {
            "current_plan": committed,
            "plan_mode_approved": True,
        }
    return {}


def build_execution_return_payload(
    *,
    result: Mapping[str, Any],
    tool_name: str,
    verified_reads: List[str],
    history: List[Dict[str, str]],
    tool_call_count: int,
    tool_last_used: Mapping[str, Any],
    files_read: Mapping[str, Any],
    recent_tool_calls: List[Any],
    plan_advance: Mapping[str, Any],
    wave_advance: Mapping[str, Any],
    replan_triggered: Mapping[str, Any],
    plan_progress_event: Mapping[str, Any],
    plan_approval_consumed: Mapping[str, Any],
    no_plan_fail_update: Mapping[str, Any],
    affected_files_update: Mapping[str, Any],
    plan_exit_update: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "last_result": result,
        "last_tool_name": tool_name,
        "verified_reads": verified_reads,
        "history": history,
        "next_action": None,
        "tool_call_count": tool_call_count,
        "tool_last_used": dict(tool_last_used),
        "files_read": dict(files_read),
        "recent_tool_calls": recent_tool_calls,
        **dict(plan_advance),
        **dict(wave_advance),
        **dict(replan_triggered),
        **dict(plan_progress_event),
        **dict(plan_approval_consumed),
        **dict(no_plan_fail_update),
        **dict(affected_files_update),
        **dict(plan_exit_update),
    }
