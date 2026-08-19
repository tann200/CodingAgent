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
    """Append only new graph-produced turns into the MessageManager."""
    if not final_state or "history" not in final_state:
        return

    msg_count_before = len(orch.msg_mgr.messages)
    if len(final_state["history"]) <= msg_count_before:
        return

    new_turns = final_state["history"][msg_count_before:]
    for turn in new_turns:
        orch.msg_mgr.append(turn["role"], turn["content"])


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

    return {
        "assistant_message": assistant_message.strip(),
        "work_summary": work_summary,
        "delegation_results": delegation_results or {},
        "dry_run_intercepted": list(getattr(orch, "_dry_run_log", [])),
    }


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
