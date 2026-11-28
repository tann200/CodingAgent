"""Response-building helpers for the inference loop."""

from __future__ import annotations

import asyncio
import contextvars as _cv
import json
import re
import threading as _threading
from typing import Any, Dict

from src.core.logger import logger as guilogger
from src.core.inference.provider_utils import (
    resolve_provider_and_model as _resolve_provider_and_model,
)
from src.core.orchestration.tool_result_formatter import (
    format_tool_result as _format_tool_result,
)
from src.core.orchestration.work_summary import _generate_work_summary


def _build_assistant_message(
    assistant_msgs: list,
    tool_results: list,
    last_tool_name: str | None,
) -> str:
    """Build the assistant message from tool results or raw assistant text."""
    if not assistant_msgs:
        return ""

    last_assistant = assistant_msgs[-1]
    _last_stripped = re.sub(
        r"<think>.*?</think>", "", last_assistant, flags=re.DOTALL
    ).strip()

    _is_tool_call_msg = (
        not last_assistant
        or _last_stripped.startswith("name:")
        or _last_stripped.startswith("```yaml")
        or _last_stripped.startswith("```\nname:")
        or (_last_stripped.startswith("```") and "name:" in _last_stripped)
    )

    if tool_results and _is_tool_call_msg:
        assistant_message = ""
        for i, result in enumerate(tool_results):
            tool_name = None
            if i == len(tool_results) - 1 and last_tool_name:
                tool_name = last_tool_name
            formatted = _format_tool_result(result, tool_name)
            if formatted:
                if assistant_message and not assistant_message.endswith("\n"):
                    assistant_message += "\n"
                assistant_message += formatted + "\n"
        return assistant_message.strip()
    return last_assistant


def _sync_msg_mgr_with_final_history(orch: Any, final_state: Dict[str, Any]) -> None:
    """Sync graph-produced history (including compactions and tool call metadata) into MessageManager."""
    if not final_state or "history" not in final_state:
        return

    final_history = list(final_state["history"] or [])
    if not final_history:
        return

    lock = getattr(orch.msg_mgr, "_lock", None)

    def _entry_from_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "role": turn.get("role", "user"),
            "content": turn.get("content", ""),
        }
        for key in ("tool_calls", "tool_call_id", "name"):
            if key in turn and turn[key] is not None:
                entry[key] = turn[key]
        return entry

    def _apply() -> None:
        msgs_before = list(orch.msg_mgr.messages)
        msg_count_before = len(msgs_before)

        # Detect compaction: final_history became shorter/replaced and starts with compacted summary
        is_compacted = False
        if len(final_history) <= msg_count_before and final_history:
            first_content = str(final_history[0].get("content") or "")
            if (
                len(final_history) < msg_count_before
                or "<summary>" in first_content
                or "Conversation summary:" in first_content
                or "This session is being continued from a previous conversation" in first_content
            ):
                is_compacted = True

        if is_compacted:
            if hasattr(orch.msg_mgr, "clear"):
                orch.msg_mgr.clear()
            else:
                orch.msg_mgr.messages.clear()
            for turn in final_history:
                extras = {k: turn[k] for k in ("tool_calls", "tool_call_id", "name") if k in turn and turn[k] is not None}
                orch.msg_mgr.append(turn.get("role", "user"), turn.get("content", ""), **extras)
            return

        if len(final_history) <= msg_count_before:
            return

        new_turns = final_history[msg_count_before:]
        for turn in new_turns:
            extras = {k: turn[k] for k in ("tool_calls", "tool_call_id", "name") if k in turn and turn[k] is not None}
            orch.msg_mgr.append(turn.get("role", "user"), turn.get("content", ""), **extras)

    if lock is not None:
        with lock:
            _apply()
    else:
        _apply()


def _extract_assistant_and_tool_results(
    final_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract assistant messages and parsed tool results from final history."""
    assistant_msgs = [
        m["content"] for m in final_state.get("history", []) if m["role"] == "assistant"
    ]
    tool_results = []
    last_tool_name = None

    for i, m in enumerate(final_state.get("history", [])):
        is_tool_result = m.get("role") == "tool" or (
            m.get("role") == "user"
            and "tool_execution_result" in (m.get("content") or "")
        )
        if not is_tool_result:
            continue

        content = m.get("content", "")
        tool_name = None
        if i > 0:
            prev_msg = final_state["history"][i - 1]
            if prev_msg.get("role") == "assistant":
                try:
                    from src.core.orchestration.tool_parser import parse_tool_block

                    parsed = parse_tool_block(prev_msg.get("content", ""))
                    if parsed and parsed.get("name"):
                        tool_name = parsed["name"]
                except Exception:
                    pass

        if "tool_execution_result" in content:
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "tool_execution_result" in data:
                    ter = data["tool_execution_result"]
                    if isinstance(ter, dict) and "result" in ter:
                        result = ter["result"]
                    elif isinstance(ter, dict):
                        result = ter
                    else:
                        result = ter
                    tool_results.append(result)
                    if tool_name:
                        last_tool_name = tool_name
                elif isinstance(data, dict) and "result" in data:
                    tool_results.append(data["result"])
                    if tool_name:
                        last_tool_name = tool_name
                elif isinstance(data, dict) and data.get("ok"):
                    tool_results.append(data)
                    if tool_name:
                        last_tool_name = tool_name
            except (json.JSONDecodeError, TypeError):
                tool_results.append(content)
        elif content:
            tool_results.append(content)

    return {
        "assistant_msgs": assistant_msgs,
        "tool_results": tool_results,
        "last_tool_name": last_tool_name,
    }


def _build_success_response(orch: Any, final_state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the successful run_agent_once response from final graph state."""
    if final_state:
        guilogger.info(
            f"Final state verified reads: {final_state.get('verified_reads')}"
        )

    _sync_msg_mgr_with_final_history(orch, final_state)

    extracted = _extract_assistant_and_tool_results(final_state)
    assistant_msgs = extracted["assistant_msgs"]
    tool_results = extracted["tool_results"]
    last_tool_name = extracted["last_tool_name"]

    guilogger.info(
        f"Graph execution completed in {final_state.get('rounds', 0) if final_state else 0} rounds"
        if final_state
        else "Graph execution completed"
    )

    if final_state is not None:
        final_state["_session_modified_files"] = list(getattr(orch, "_session_modified_files", []))
    try:
        if hasattr(orch, "_publish_session_changes"):
            orch._publish_session_changes()
    except Exception:
        pass

    history = final_state.get("history", []) if final_state else []
    work_summary = _generate_work_summary(final_state, history)
    assistant_message = _build_assistant_message(
        assistant_msgs, tool_results, last_tool_name
    )

    delegation_results = final_state.get("delegation_results") if final_state else None
    if delegation_results:
        guilogger.info(
            f"run_agent_once: delegation_results keys={list(delegation_results.keys())}"
        )

    orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
    orch.flush_execution_trace()

    if assistant_message:
        try:
            _sid = getattr(orch, "_current_task_id", None)
            try:
                _tname = _threading.current_thread().name
            except Exception:
                _tname = "unknown"
            guilogger.debug(
                "inference_loop: add_message (session=%r, role=%s, thread=%s)",
                _sid,
                "assistant",
                _tname,
            )
            orch.session_store.add_message(
                session_id=_sid,
                role="assistant",
                content=assistant_message.strip(),
            )
        except Exception:
            pass

    response: Dict[str, Any] = {
        "assistant_message": assistant_message.strip(),
        "work_summary": work_summary,
        "delegation_results": delegation_results or {},
        "dry_run_intercepted": list(getattr(orch, "_dry_run_log", [])),
    }

    # REM-02 (CRIT-02): Propagate authoritative failure / cancellation status
    # from final_state so headless CLI and HTTP /task never misreport failed or
    # rolled-back pipeline runs as complete (exit code 0 / COMPLETED).
    if isinstance(final_state, dict):
        _run = final_state.get("run")
        _run_status = str(_run.get("status") or "").strip().lower() if isinstance(_run, dict) else ""
        _run_outcome = str(_run.get("outcome") or "").strip().lower() if isinstance(_run, dict) else ""
        _ver_passed = final_state.get("verification_passed")
        _errors = [str(e) for e in (final_state.get("errors") or []) if e]
        _last_res = final_state.get("last_result")
        _last_err = (
            str(_last_res.get("error") or "")
            if isinstance(_last_res, dict) and _last_res.get("error")
            else ""
        )

        is_cancelled = _run_status in ("cancelled", "canceled") or _run_outcome in (
            "cancelled",
            "canceled",
        )
        is_failed = (
            not is_cancelled
            and (
                _run_status in ("failed", "error")
                or _run_outcome in ("failed", "error")
                or _ver_passed is False
            )
        )
        if is_cancelled:
            response["ok"] = False
            response["status"] = "cancelled"
            response["canceled"] = True
            response["cancelled"] = True
        elif is_failed:
            raw_err = _errors[-1] if _errors else (_last_err or f"Run ended with status={_run_status or _run_outcome or 'failed'}")
            try:
                from src.core.security.redaction import redact_secrets

                raw_err = redact_secrets(raw_err)
            except Exception:
                pass
            response["ok"] = False
            response["status"] = "failed"
            response["error"] = raw_err
            if _errors:
                response["errors"] = _errors
        else:
            response["ok"] = True
            response["status"] = "completed"

    return response


def _call_model_fallback_sync(
    *,
    orch: Any,
    prompt: str,
    full_system_prompt: str,
    streaming_enabled: bool,
) -> Any:
    """Call the model directly as a best-effort fallback after graph failure."""
    from src.core.inference.llm_manager import call_model

    provider_name = None
    model_name = None
    try:
        provider_name, model_name = _resolve_provider_and_model(orch)
    except Exception:
        provider_name, model_name = None, None

    messages_for_model = [
        {"role": "system", "content": full_system_prompt},
        {"role": "user", "content": prompt},
    ]
    try:
        _call_coro = call_model(
            messages_for_model,
            provider=provider_name,
            model=model_name,
            stream=streaming_enabled,
            format_json=False,
        )
        try:
            asyncio.get_running_loop()

            _ctx = _cv.copy_context()
            _fb_executor = getattr(orch, "_graph_executor", None)
            if _fb_executor is not None:
                return _fb_executor.submit(_ctx.run, asyncio.run, _call_coro).result()

            import concurrent.futures as _cf_fb

            with _cf_fb.ThreadPoolExecutor(max_workers=1) as _ex:
                return _ex.submit(_ctx.run, asyncio.run, _call_coro).result()
        except RuntimeError:
            return asyncio.run(_call_coro)
    except Exception:
        return None


def _extract_fallback_response_content(resp: Any) -> str:
    """Extract assistant content from a direct call_model fallback response."""
    content = ""
    if isinstance(resp, dict):
        _choices = resp.get("choices")
        if _choices and len(_choices) > 0:
            ch = _choices[0].get("message") if isinstance(_choices[0], dict) else None
        else:
            ch = resp.get("message")
        if isinstance(ch, str):
            content = ch
        elif isinstance(ch, dict):
            content = ch.get("content") or ""
    return content


def _build_graph_failure_response(
    *,
    orch: Any,
    error: Exception,
    prompt: str,
    full_system_prompt: str,
    streaming_enabled: bool,
) -> Dict[str, Any]:
    """Build the fallback response after graph execution fails."""
    guilogger.error(f"Graph execution failed: {error}")
    orch.msg_mgr.append("user", f"Error during tool execution: {error}")

    try:
        resp = _call_model_fallback_sync(
            orch=orch,
            prompt=prompt,
            full_system_prompt=full_system_prompt,
            streaming_enabled=streaming_enabled,
        )
        content = _extract_fallback_response_content(resp)

        if content:
            try:
                orch.msg_mgr.append("assistant", content)
            except Exception:
                pass

        orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
        orch.flush_execution_trace()
        return {
            "assistant_message": content if content else "",
            "error": "graph_failed",
            "exception": str(error),
        }
    except Exception:
        orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))
        orch.flush_execution_trace()
        return {"error": "graph_failed", "exception": str(error)}
