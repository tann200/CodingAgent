"""frontier_loop_node.py — Unified canonical agent loop across all 2026 model tiers.

Executes the tight LLM + tool-calling loop inside a single LangGraph node while
adapting tool schemas, turn budgets, read-only wave parallelism, and compaction
triggers via ``ModelRuntimePolicy`` (``nano``, ``compact``, ``standard``, ``frontier``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.core.messaging.event_types import AgentStatus
from src.core.orchestration.graph.completion_contract import _MODIFYING_TOOL_NAMES
from src.core.orchestration.graph.nodes.frontier_completion import (
    _filter_tools_for_tier,
    _is_context_overflow,
    _plan_mode_blocks,
    build_plan_mode_blocked_exit,
    clear_transient_contract_errors,
    compute_replan_trigger,
    finalize_frontier_exit,
    handle_no_tool_completion,
)
from src.core.orchestration.graph.nodes.frontier_dispatch import (
    build_native_assistant_message,
    execute_raw_tool,
    normalize_tool_calls_with_ids,
)
from src.core.orchestration.graph.nodes.frontier_messages import (
    _extract_content_text,
    _extract_tool_call_from_text,
    _normalize_messages_for_native_tools,
)
from src.core.orchestration.graph.nodes.frontier_toolcalls import (
    _append_tool_call_history,
    _normalize_tool_call,
    _render_tool_calls_text,
)
from src.core.orchestration.graph.nodes.frontier_turn_helpers import (
    call_llm_for_turn_impl,
    dispatch_tool_calls_impl,
    drain_operator_injections,
    enrich_loop_task,
    prepare_turn_messages_impl,
    resolve_provider_and_governor,
    response_triggers_compaction,
    resume_pending_action,
)
from src.core.orchestration.graph.nodes.node_utils import (
    _notify_provider_limit,
    _resolve_orchestrator,
)
from src.core.orchestration.graph.nodes.repo_context import (
    build_repo_context_block,
    refresh_index_after_write,
)
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    TOOL_LARGE_TEXT_FIELDS,
    TOOL_OUTPUT_MAX_BYTES,
    truncate_tool_output,
)
from src.core.orchestration.graph.nodes.tool_result_safety import sanitize_tool_result
from src.core.orchestration.graph.runtime_policy import resolve_effective_tool_schemas
from src.core.orchestration.graph.state import StateLike
from src.core.security.redaction import redact_secrets

# Re-exported for backwards compatibility with the legacy test surface
__all__ = [
    "frontier_loop_node",
    "agent_loop_node",
    "_append_tool_call_history",
    "_extract_content_text",
    "_extract_tool_call_from_text",
    "_filter_tools_for_tier",
    "_is_context_overflow",
    "_normalize_messages_for_native_tools",
    "_normalize_tool_call",
    "_plan_mode_blocks",
    "_render_tool_calls_text",
]

logger = logging.getLogger(__name__)

_DEFAULT_MAX_FRONTIER_TURNS = 20
_MAX_FRONTIER_TURNS = _DEFAULT_MAX_FRONTIER_TURNS
_MAX_INLINE_VERIFICATION_FAILURES = 3
_MAX_INCOMPLETE_NUDGES = 2

_TOOL_OUTPUT_MAX_BYTES = TOOL_OUTPUT_MAX_BYTES
_TOOL_LARGE_TEXT_FIELDS = TOOL_LARGE_TEXT_FIELDS


async def call_model(*args, **kwargs):
    """Module-level proxy to ``llm_manager.call_model`` so unit tests can patch it."""
    from src.core.inference.llm_manager import call_model as _call

    return await _call(*args, **kwargs)


def _truncate_tool_output(res: dict) -> dict:
    """Cap large text fields in a tool result before it enters history."""
    return truncate_tool_output(res, marker_label="frontier_loop")


def _sanitize_tool_output(res: dict) -> dict:
    """Scan untrusted tool output for prompt injection before truncation."""
    return sanitize_tool_result(res, marker_label="frontier_loop", log=logger)


async def _prepare_turn_messages(
    orchestrator: Any,
    task: str,
    history: list,
    model_tier: str,
    turns_taken: int,
    retrieved_snippets: "list | None" = None,
    role: str = "operational",
    runtime_policy: Any = None,
    thinking_mode: "str | None" = None,
    model_name: "str | None" = None,
    tools_schema: "list | None" = None,
) -> list:
    """Build the messages list for one LLM turn while preserving the KV-cache prefix."""
    return await prepare_turn_messages_impl(
        orchestrator=orchestrator,
        task=task,
        history=history,
        model_tier=model_tier,
        turns_taken=turns_taken,
        retrieved_snippets=retrieved_snippets,
        role=role,
        runtime_policy=runtime_policy,
        thinking_mode=thinking_mode,
        model_name=model_name,
        tools_schema=tools_schema,
    )


async def _call_llm_for_turn(
    messages: list,
    provider_name: "str | None",
    model_name: "str | None",
    orchestrator: Any,
    turns_taken: int,
    model_tier: str = "frontier",
    role: str = "operational",
    runtime_policy: Any = None,
    thinking_mode: "str | None" = None,
    tools_schema: "list | None" = None,
) -> "tuple[Any, str, list]":
    """Call the LLM and extract ``(response, content_text, tool_calls)``."""
    return await call_llm_for_turn_impl(
        messages=messages,
        provider_name=provider_name,
        model_name=model_name,
        orchestrator=orchestrator,
        turns_taken=turns_taken,
        call_model_fn=call_model,
        model_tier=model_tier,
        role=role,
        runtime_policy=runtime_policy,
        thinking_mode=thinking_mode,
        tools_schema=tools_schema,
    )


async def _dispatch_tool_calls(
    tool_calls: list,
    history: list,
    state: "StateLike",
    orchestrator: Any,
    model_tier: str,
    bus: Any,
    turns_taken: int,
    tool_call_count: int,
    max_tool_calls: int,
    iv_baselines: dict,
    *,
    use_native_tools: bool = False,
    max_parallel_reads: int = 1,
    normalized_calls: "list | None" = None,
    last_failed_sig: "list | None" = None,
    last_tool_args_out: "dict | None" = None,
    verified_hashes: "dict | None" = None,
) -> "tuple[list, int, Dict[str, Any] | None, str | None, list]":
    """Execute tool calls in ordered waves (parallel read-only waves + serial mutations)."""
    return await dispatch_tool_calls_impl(
        tool_calls=tool_calls,
        history=history,
        state=state,
        orchestrator=orchestrator,
        model_tier=model_tier,
        bus=bus,
        turns_taken=turns_taken,
        tool_call_count=tool_call_count,
        max_tool_calls=max_tool_calls,
        iv_baselines=iv_baselines,
        sanitize_fn=_sanitize_tool_output,
        truncate_fn=_truncate_tool_output,
        execute_raw_tool_fn=execute_raw_tool,
        refresh_index_fn=refresh_index_after_write,
        use_native_tools=use_native_tools,
        max_parallel_reads=max_parallel_reads,
        normalized_calls=normalized_calls,
        last_failed_sig=last_failed_sig,
        last_tool_args_out=last_tool_args_out,
        verified_hashes=verified_hashes,
    )


async def frontier_loop_node(
    state: StateLike,
    config: Any,
) -> Dict[str, Any]:
    """Unified tight LLM + tool loop across all model tiers."""
    orchestrator = _resolve_orchestrator(state, config)
    task: str = state.get("task") or ""
    raw_user_task: str = task
    history: list = list(state.get("history") or [])
    tool_call_count: int = int(state.get("tool_call_count") or 0)
    model_tier: str = (state.get("model_tier") or "frontier").lower()
    errors: list = list(state.get("errors") or [])
    pending_action: Dict[str, Any] | None = (
        state.get("next_action") if isinstance(state.get("next_action"), dict) else None
    )

    runtime_block = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
    runtime_policy = (
        runtime_block.get("policy")
        if isinstance(runtime_block, dict) and isinstance(runtime_block.get("policy"), dict)
        else None
    )
    max_tool_calls: int = int(
        state.get("max_tool_calls") or 30
    )
    thinking_mode_val = state.get("thinking_mode")
    thinking_mode_str: str | None = (
        str(getattr(thinking_mode_val, "value", thinking_mode_val)).lower()
        if thinking_mode_val is not None
        else None
    )
    if _MAX_FRONTIER_TURNS != _DEFAULT_MAX_FRONTIER_TURNS:
        effective_max_turns = _MAX_FRONTIER_TURNS
    elif isinstance(runtime_policy, dict) and runtime_policy.get("turn_limit"):
        effective_max_turns = int(runtime_policy["turn_limit"])
    elif state.get("max_turns"):
        effective_max_turns = int(state.get("max_turns") or _MAX_FRONTIER_TURNS)
    else:
        effective_max_turns = _MAX_FRONTIER_TURNS

    max_parallel_reads = (
        int(runtime_policy.get("max_parallel_reads") or 1)
        if isinstance(runtime_policy, dict) and runtime_policy.get("readonly_parallelism", True)
        else (3 if model_tier in ("frontier", "large", "medium") else 1)
    )

    try:
        repo_context: str = build_repo_context_block(state, task)
    except Exception as exc:
        logger.debug("frontier_loop_node: repo context unavailable: %s", exc)
        repo_context = ""
    task = enrich_loop_task(state, task, repo_context)

    retrieved_snippets = state.get("retrieved_snippets")
    if retrieved_snippets is None and orchestrator:
        try:
            from src.core.orchestration.graph.nodes.repo_context import _retrieve_context

            retrieved_snippets = await _retrieve_context(state, orchestrator)
        except Exception:
            retrieved_snippets = []

    provider_name, model_name, supports_native, kv_gov = resolve_provider_and_governor(
        orchestrator, state
    )

    last_result: Dict[str, Any] | None = None
    last_tool_name: str | None = None
    last_tool_args: Dict[str, Any] = {}
    last_failed_sig: list[str] = []
    verified_hashes: Dict[str, str] = dict(state.get("_verified_file_hashes") or {})
    inline_verifications: list = []
    iv_baselines: dict = {}
    turns_taken: int = 0
    incomplete_nudges: int = 0
    mutating_tool_calls_count: int = 0
    replan_required: str | None = None
    cancelled: bool = False
    budget_exhausted: bool = False
    llm_error: str | None = None

    prev_run_turns = int((state.get("run") or {}).get("turns") or 0)
    remaining_turns = max(0, effective_max_turns - prev_run_turns)
    cancel_event = state.get("cancel_event") or (
        getattr(orchestrator, "cancel_event", None) if orchestrator else None
    )

    def _loop_cancelled() -> bool:
        try:
            return bool(
                cancel_event is not None
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set() is True
            )
        except Exception:
            return False

    bus: Any = getattr(orchestrator, "event_bus", None) if orchestrator else None
    try:
        if bus:
            bus.publish_typed(
                AgentStatus(status="working", node="frontier_loop", task=redact_secrets(task[:100]))
            )
    except Exception:
        pass

    while turns_taken < remaining_turns:
        if _loop_cancelled():
            logger.info("frontier_loop_node: cancellation requested — exiting loop")
            cancelled = True
            break
        turns_taken += 1
        history = drain_operator_injections(state, orchestrator, history)

        if tool_call_count >= max_tool_calls:
            logger.warning(
                "frontier_loop_node: tool budget exhausted (%d/%d), exiting loop",
                tool_call_count,
                max_tool_calls,
            )
            budget_exhausted = True
            break

        role = (
            state.get("active_role")
            or (getattr(orchestrator, "active_role", None) if orchestrator else None)
            or "operational"
        )
        effective_tools = resolve_effective_tool_schemas(
            orchestrator, runtime_policy, active_role=role
        )
        use_native_tools = bool(effective_tools and supports_native)

        tool_calls: list = []
        content_text: str = ""
        response: Any = None
        normalized_calls: list | None = None

        if pending_action is not None:
            tool_calls, normalized_calls, history = resume_pending_action(
                pending_action, history, turns_taken, use_native_tools
            )
            pending_action = None
        else:
            try:
                messages = await _prepare_turn_messages(
                    orchestrator=orchestrator, task=task, history=history,
                    model_tier=model_tier, turns_taken=turns_taken,
                    retrieved_snippets=retrieved_snippets, role=role,
                    runtime_policy=runtime_policy, thinking_mode=thinking_mode_str,
                    model_name=model_name, tools_schema=effective_tools,
                )
            except TypeError:
                try:
                    messages = await _prepare_turn_messages(
                        orchestrator=orchestrator, task=task, history=history,
                        model_tier=model_tier, turns_taken=turns_taken,
                        retrieved_snippets=retrieved_snippets, role=role,
                        runtime_policy=runtime_policy,
                    )
                except TypeError:
                    messages = await _prepare_turn_messages(
                        orchestrator=orchestrator, task=task, history=history,
                        model_tier=model_tier, turns_taken=turns_taken,
                    )
            from src.core.orchestration.loop_guards import augment_turn_messages

            messages = augment_turn_messages(messages, state, turns_taken - 1)

            try:
                try:
                    response, content_text, tool_calls = await _call_llm_for_turn(
                        messages=messages, provider_name=provider_name, model_name=model_name,
                        orchestrator=orchestrator, turns_taken=turns_taken, model_tier=model_tier,
                        role=role, runtime_policy=runtime_policy, thinking_mode=thinking_mode_str,
                        tools_schema=effective_tools,
                    )
                except TypeError:
                    response, content_text, tool_calls = await _call_llm_for_turn(
                        messages=messages, provider_name=provider_name, model_name=model_name,
                        orchestrator=orchestrator, turns_taken=turns_taken,
                        model_tier=model_tier, role=role,
                    )
            except Exception as exc:
                error_str = str(exc)
                logger.warning("frontier_loop_node: LLM call failed: %s", error_str)
                _notify_provider_limit(error_str)
                if _is_context_overflow(error_str):
                    errors = list(errors) + ["context_overflow"]
                else:
                    llm_error = error_str or "LLM call failed"
                    errors = list(errors) + ["llm_call_failed"]
                    last_result = {"ok": False, "error": llm_error}
                break

            if response_triggers_compaction(
                response, content_text, tool_calls, runtime_policy, kv_gov, _is_context_overflow
            ):
                errors = list(errors) + ["context_overflow"]
                break

            if tool_calls and orchestrator:
                for tc in tool_calls:
                    tool_name, tool_args = _normalize_tool_call(tc)
                    if tool_name and _plan_mode_blocks(orchestrator, tool_name):
                        logger.info("frontier_loop_node: plan-mode blocked '%s' — suspending", tool_name)
                        return build_plan_mode_blocked_exit(
                            tool_name=tool_name, tool_args=tool_args, history=history,
                            tool_call_count=tool_call_count, last_result=last_result,
                            last_tool_name=last_tool_name, errors=errors, turns_taken=turns_taken, state=state,
                        )

            history = list(history)
            if tool_calls and use_native_tools:
                normalized_calls = normalize_tool_calls_with_ids(tool_calls, turns_taken)
                history.append(
                    build_native_assistant_message(
                        content_text,
                        [item[3] for item in normalized_calls],
                        response=response,
                    )
                )
            else:
                history.append({"role": "assistant", "content": content_text or ""})

        # ---- No tool calls -> evaluate completion contract --------------
        if not tool_calls:
            last_result = handle_no_tool_completion(
                task=raw_user_task,
                content_text=content_text,
                turns_taken=turns_taken,
                tool_call_count=tool_call_count,
                state=state,
                orchestrator=orchestrator,
                last_result=last_result,
                last_tool_name=last_tool_name,
                mutating_tool_calls_count=mutating_tool_calls_count,
                errors=errors,
            )
            if not last_result.get("ok"):
                err_code = last_result.get("error_code") or "unverified_completion"
                if (
                    use_native_tools
                    and err_code
                    in (
                        "unverified_text_only_completion",
                        "unverified_mutation_missing",
                        "missing_named_deliverable",
                        "empty_response",
                        "empty_inspection",
                        "mutation_failed",
                    )
                    and incomplete_nudges < _MAX_INCOMPLETE_NUDGES
                    and turns_taken < effective_max_turns
                ):
                    incomplete_nudges += 1
                    if orchestrator is not None:
                        setattr(orchestrator, "_incomplete_nudge_retries", incomplete_nudges)
                    from src.core.orchestration.loop_guards import build_incomplete_turn_nudge

                    history = list(history)
                    history.append({"role": "user", "content": build_incomplete_turn_nudge(err_code, str(last_result.get("error") or ""), response=response, content_text=content_text)})
                    continue
                errors = list(errors) + [err_code]
            else:
                errors = clear_transient_contract_errors(errors)
            break

        # Track mutating tool calls
        if orchestrator is not None and bool(getattr(orchestrator, "_incomplete_nudge_retries", False)):
            setattr(orchestrator, "_incomplete_nudge_retries", 0)
        for tc in tool_calls:
            tool_name, _ = _normalize_tool_call(tc)
            if tool_name and tool_name in _MODIFYING_TOOL_NAMES:
                mutating_tool_calls_count += 1

        if _loop_cancelled():
            logger.info("frontier_loop_node: cancellation requested before tool dispatch")
            cancelled = True
            break

        # ---- Execute tool calls ------------------------------------------
        dispatch_kwargs: Dict[str, Any] = dict(
            tool_calls=tool_calls,
            history=history,
            state=state,
            orchestrator=orchestrator,
            model_tier=model_tier,
            bus=bus,
            turns_taken=turns_taken,
            tool_call_count=tool_call_count,
            max_tool_calls=max_tool_calls,
            iv_baselines=iv_baselines,
        )
        try:
            history, tool_call_count, batch_result, batch_tool, batch_iv = await _dispatch_tool_calls(
                **dispatch_kwargs,
                use_native_tools=use_native_tools,
                max_parallel_reads=max_parallel_reads,
                normalized_calls=normalized_calls,
                last_failed_sig=last_failed_sig,
                last_tool_args_out=last_tool_args,
                verified_hashes=verified_hashes,
            )
        except TypeError:
            history, tool_call_count, batch_result, batch_tool, batch_iv = await _dispatch_tool_calls(
                **dispatch_kwargs
            )

        if batch_result is not None:
            last_result = batch_result
        if batch_tool:
            last_tool_name = batch_tool
        inline_verifications.extend(batch_iv)

        _iv_failures = [r for r in inline_verifications if r.get("status") == "failed"]
        if len(_iv_failures) >= _MAX_INLINE_VERIFICATION_FAILURES:
            logger.error(
                "frontier_loop_node: %d inline verification failures — exiting loop",
                len(_iv_failures),
            )
            last_result = {
                "ok": False,
                "error": (
                    f"Inline verification failed {len(_iv_failures)}x — the edits are "
                    "not producing valid files"
                ),
            }
            errors = list(errors) + ["inline_verification_failed"]
            break

        _replan_trigger = compute_replan_trigger(result=batch_result or {})
        if _replan_trigger:
            replan_required = _replan_trigger.get("replan_required")
            break

        if len(last_failed_sig) > 2 and int(last_failed_sig[2]) >= 3:
            replan_required = (
                f"Blocked identical repeat of '{last_tool_name}' 3 times: "
                f"{last_failed_sig[1] or 'change tool or arguments'}"
            )
            break

        if tool_call_count >= max_tool_calls:
            budget_exhausted = True
            break

    try:
        if bus:
            bus.publish_typed(AgentStatus(status="idle", node="frontier_loop", turns=turns_taken, tool_calls=tool_call_count))
    except Exception:
        pass

    turn_budget_exhausted = bool(
        not cancelled and not budget_exhausted and not llm_error and not replan_required
        and "context_overflow" not in (errors or [])
        and not (last_result and last_result.get("completed_without_tool"))
        and turns_taken >= remaining_turns
    )

    return finalize_frontier_exit(
        state=state, orchestrator=orchestrator, history=history, tool_call_count=tool_call_count,
        last_result=last_result, last_tool_name=last_tool_name, errors=errors, turns_taken=turns_taken,
        inline_verifications=inline_verifications, replan_required=replan_required,
        retrieved_snippets=retrieved_snippets, last_tool_args=last_tool_args or None,
        verified_file_hashes=verified_hashes or None, cancelled=cancelled,
        budget_exhausted=budget_exhausted, llm_error=llm_error, turn_budget_exhausted=turn_budget_exhausted,
    )


# Canonical alias for the unified 6-node architecture
agent_loop_node = frontier_loop_node
