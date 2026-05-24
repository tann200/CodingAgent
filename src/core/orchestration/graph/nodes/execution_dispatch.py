"""execution_dispatch.py — LLM-driven action generation for plan steps.

Extracted from execution_helpers.py (P3-4) for improved modularity.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


async def generate_action_for_plan_step(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    current_plan: List[Dict[str, Any]],
    current_step: int,
    original_task: Optional[str],
    execution_max_prompt_tokens: int,
    context_builder_cls: Any,
    resolve_provider_capabilities: Callable[[Any], Mapping[str, Any]],
    call_model_fn: Any,
    parse_tool_block: Callable[[str], Optional[dict]],
    extract_tool_call_from_response_fn: Callable[
        ..., Tuple[str, Optional[List[Any]], Optional[dict]]
    ],
    logger: Any,
) -> Tuple[Optional[dict], List[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
    """Best-effort LLM generation of a tool call for the current plan step."""
    current_step_desc = current_plan[current_step].get("description", "")
    logger.info("No action provided, generating tool for step: %s", current_step_desc)

    if not orchestrator or not getattr(orchestrator, "tool_registry", None):
        raise RuntimeError(
            "execution_node: orchestrator or tool_registry unavailable for LLM step generation"
        )

    builder = context_builder_cls(working_dir=state.get("working_dir"))
    tools_list = [
        {"name": name, "description": meta.get("description", "")}
        for name, meta in orchestrator.tool_registry.tools.items()
    ]

    active_skills = []
    relevant_files = state.get("relevant_files") or []
    if len(relevant_files) > 2:
        active_skills.append("dry")
        logger.info("execution_node: injected DRY skill due to many relevant files")

    step_prompt = (
        f"Execute this specific step: {current_step_desc}\n\n"
        f"Working directory: {state.get('working_dir')}\n"
        f"Original task: {original_task or state.get('task')}\n\n"
        "Generate the appropriate tool call to complete this step. "
        "Respond with ONLY a JSON function call (no YAML)."
    )

    provider_capabilities = resolve_provider_capabilities(orchestrator)
    messages = builder.build_prompt(
        role_name="operational",
        active_skills=active_skills,
        task_description=step_prompt,
        tools=tools_list,
        conversation=state.get("history", []),
        max_tokens=execution_max_prompt_tokens,
        provider_capabilities=provider_capabilities,
        model_tier=state.get("model_tier"),
        model_name=provider_capabilities.get("model") or "",
    )

    cancel_event = state.get("cancel_event") or getattr(
        orchestrator, "cancel_event", None
    )
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled before LLM call")
        return (
            None,
            current_plan,
            "",
            {
                "last_result": {"ok": False, "error": "Task canceled by user"},
                "next_action": None,
                "errors": ["canceled"],
            },
        )

    functions = None
    if orchestrator and hasattr(orchestrator.tool_registry, "get_openai_functions"):
        functions = orchestrator.tool_registry.get_openai_functions()

    exec_model_override = state.get("override_model") or None
    if not exec_model_override:
        try:
            from src.core.config_loader import get_model_for_role as _gmfr

            exec_model_override = _gmfr("operational")
        except Exception:
            pass

    exec_llm_timeout: int | None = 120
    try:
        from src.core.orchestration.project_settings import (
            get_active_settings as _gas_exec,
        )

        ps_exec = _gas_exec()
        if ps_exec is not None:
            exec_llm_timeout = ps_exec.max_llm_wait_seconds or None
    except Exception:
        pass

    try:
        call_coro = call_model_fn(
            messages,
            stream=False,
            format_json=False,
            tools=functions,
            temperature=0.0,
            session_id=state.get("session_id"),
            **({"model": exec_model_override} if exec_model_override else {}),
        )
        if exec_llm_timeout:
            resp = await asyncio.wait_for(
                call_coro, timeout=exec_llm_timeout
            )
        else:
            resp = await call_coro

    except asyncio.TimeoutError:
        logger.warning(
            "execution_node: LLM call timed out after %ss",
            exec_llm_timeout,
        )
        return (
            None,
            current_plan,
            "",
            {
                "last_result": {
                    "ok": False,
                    "error": f"LLM call timed out after {exec_llm_timeout}s",
                },
                "next_action": "wait_for_user",
                "errors": [f"llm_timeout:{exec_llm_timeout}s"],
            },
        )

    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled after LLM call")
        return (
            None,
            current_plan,
            "",
            {
                "last_result": {"ok": False, "error": "Task canceled by user"},
                "next_action": None,
                "errors": ["canceled"],
            },
        )

    if isinstance(resp, dict) and resp.get("context_overflow"):
        raw_hist = list(state.get("history") or [])
        trunc_hist = raw_hist[-6:] if len(raw_hist) > 6 else raw_hist
        logger.warning(
            "execution_node: context overflow detected — truncating history %d → %d messages; errors=['context_overflow'] will route to memory_sync",
            len(raw_hist),
            len(trunc_hist),
        )
        return (
            None,
            current_plan,
            "",
            {
                "history": [],
                "_compacted_history": trunc_hist,
                "next_action": None,
                "_budget_compaction": True,
                "_should_distill": True,
                "errors": ["context_overflow"],
                "last_result": {
                    "ok": False,
                    "error": "Context window overflow in execution — history truncated, compaction triggered",
                },
            },
        )

    content, _tool_calls, tool_call = extract_tool_call_from_response_fn(
        resp,
        parse_tool_block=parse_tool_block,
        logger=logger,
    )
    updated_plan = current_plan
    if tool_call:
        logger.info("Generated tool call for step: %s", tool_call)
        updated_plan = [dict(step) for step in current_plan]
        updated_plan[current_step]["action"] = tool_call

    return tool_call, updated_plan, content, None
