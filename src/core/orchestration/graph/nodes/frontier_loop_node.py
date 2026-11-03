"""frontier_loop_node.py — Unified canonical agent loop across all 2026 model tiers.

Executes the tight LLM + tool-calling loop inside a single LangGraph node while
adapting tool schemas, turn budgets, read-only wave parallelism, and compaction
triggers via ``ModelRuntimePolicy`` (``nano``, ``compact``, ``standard``, ``frontier``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Dict, cast

from src.core.inference.kv_cache_governor import (
    CompactionAction,
    KVCacheGovernor,
    create_governor_for_model,
)
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
    append_tool_result_to_history,
    build_native_assistant_message,
    capture_workspace_file_state,
    compute_tool_call_signature,
    execute_raw_tool,
    is_tool_result_failure,
    normalize_tool_calls_with_ids,
    partition_tool_call_waves,
    record_bash_mutations,
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
from src.core.orchestration.graph.nodes.in_loop_verification import (
    build_inline_verification_message,
    capture_lint_baseline,
    run_inline_check,
    update_verified_file_hashes,
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
    prune_tool_outputs,
    truncate_tool_output,
)
from src.core.orchestration.graph.nodes.tool_result_safety import sanitize_tool_result
from src.core.orchestration.graph.runtime_policy import resolve_effective_tool_schemas
from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.tool_parser import parse_tool_block
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
) -> list:
    """Build the messages list for one LLM turn while preserving the KV-cache prefix."""
    try:
        from src.core.context.context_builder import ContextBuilder
        from src.core.inference.provider_context import get_context_budget
        from src.core.orchestration.event_bus import run_with_correlation
        from src.core.orchestration.provider_capabilities import (
            resolve_provider_capabilities as _resolve_pc,
        )

        cb = ContextBuilder(working_dir=getattr(orchestrator, "working_dir", None))
        history = prune_tool_outputs(history)
        token_budget = get_context_budget(model_tier=model_tier)
        tools_schema = resolve_effective_tool_schemas(
            orchestrator, runtime_policy, active_role=role
        )
        provider_caps = _resolve_pc(orchestrator)

        loop = asyncio.get_running_loop()
        partial_fn = functools.partial(
            cb.build_prompt,
            role,
            retrieved_snippets or [],
            task,
            tools_schema or [],
            history,
            token_budget,
            None,
            provider_caps,
            None,
            model_tier,
        )
        built_messages = await run_with_correlation(loop, None, partial_fn)
        messages = list(built_messages) if isinstance(built_messages, list) else []
        if not messages:
            messages = [
                {
                    "role": "system",
                    "content": f"You are a capable coding assistant. Task: {task}",
                }
            ]

        if provider_caps.get("supports_native_tools") and tools_schema:
            messages = _normalize_messages_for_native_tools(messages)
        return messages
    except Exception as exc:
        logger.warning("frontier_loop_node: context build failed: %s", exc)
        return [
            {
                "role": "system",
                "content": f"You are a capable coding assistant. Task: {task}",
            }
        ]


async def _call_llm_for_turn(
    messages: list,
    provider_name: "str | None",
    model_name: "str | None",
    orchestrator: Any,
    turns_taken: int,
    model_tier: str = "frontier",
    role: str = "operational",
    runtime_policy: Any = None,
) -> "tuple[Any, str, list]":
    """Call the LLM and extract ``(response, content_text, tool_calls)``."""
    tools_schema = resolve_effective_tool_schemas(
        orchestrator, runtime_policy, active_role=role
    )

    _llm_timeout: int | None = 120
    try:
        from src.core.orchestration.project_settings import get_active_settings as _gas_fl

        _ps_fl = _gas_fl()
        if _ps_fl is not None:
            _llm_timeout = _ps_fl.max_llm_wait_seconds or None
    except Exception:
        pass

    response = await asyncio.wait_for(
        call_model(
            messages=messages,
            provider=provider_name,
            model=model_name,
            tools=tools_schema or None,
        ),
        timeout=_llm_timeout,
    )

    content_text = _extract_content_text(response)

    tool_calls: list[Any] = []
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices and len(choices) > 0:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            if isinstance(msg, dict):
                tool_calls = list(msg.get("tool_calls") or [])
            if not tool_calls and isinstance(choice, dict):
                tool_calls = list(choice.get("tool_calls") or [])
        if not tool_calls and response.get("tool_calls"):
            tool_calls = list(response.get("tool_calls") or [])
    else:
        tool_calls = list(getattr(response, "tool_calls", None) or [])

    # Text-based tool parser fallback (non-native models or inline YAML/JSON blocks)
    if not tool_calls and content_text:
        try:
            import re

            content_for_parse = re.sub(
                r"<\|channel\|>thought.*?<\|/channel\|>",
                "",
                content_text,
                flags=re.DOTALL,
            ).strip()
            if content_for_parse:
                parsed = parse_tool_block(content_for_parse)
                if parsed and isinstance(parsed, dict) and (parsed.get("name") or parsed.get("tool")):
                    tool_calls = [parsed]
        except Exception as exc:
            logger.debug("frontier_loop_node: tool parse error: %s", exc)
            tool_calls = []

    # Synthesize JSON tool-call text into content ONLY when running without native tool schemas
    if not tools_schema and tool_calls and not parse_tool_block(content_text or ""):
        synthesized_tool_text = _render_tool_calls_text(tool_calls)
        if synthesized_tool_text:
            content_text = (
                f"{content_text.rstrip()}\n\n{synthesized_tool_text}"
                if content_text.strip()
                else synthesized_tool_text
            )

    return response, content_text, tool_calls


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
    last_result: "Dict[str, Any] | None" = None
    last_tool: str | None = None
    inline_verifications: list = []
    history = list(history)
    workdir = str(state.get("working_dir") or "")

    norm_calls = (
        normalized_calls
        if normalized_calls is not None
        else normalize_tool_calls_with_ids(tool_calls, turns_taken)
    )
    waves = partition_tool_call_waves(norm_calls, orchestrator, max_parallel_reads=max_parallel_reads)

    for is_parallel, wave_items in waves:
        remaining = max_tool_calls - tool_call_count
        if remaining <= 0:
            break
        active_items = wave_items[:remaining]

        if is_parallel and len(active_items) > 1:
            raw_results = await asyncio.gather(
                *[
                    execute_raw_tool(
                        orchestrator,
                        {"name": t_name, "arguments": t_args},
                        model_tier,
                        run_async=True,
                        log=logger,
                    )
                    for (_cid, t_name, t_args, _otc) in active_items
                ]
            )
            paired_outcomes = [(item, raw_res, None) for item, raw_res in zip(active_items, raw_results)]
        else:
            item = active_items[0]
            call_id, tool_name, tool_args, _ = item
            sig = compute_tool_call_signature(tool_name, tool_args)
            if last_failed_sig and last_failed_sig[0] == sig:
                raw_res = {
                    "ok": False,
                    "error": (
                        f"Blocked identical repeat of failing tool call '{tool_name}' "
                        "with unchanged arguments. Inspect the previous error and modify "
                        "arguments or try an alternative approach."
                    ),
                }
                iv_baseline = None
            else:
                if (
                    orchestrator
                    and hasattr(orchestrator, "begin_step_transaction")
                    and tool_name in _MODIFYING_TOOL_NAMES
                    and not getattr(orchestrator, "_step_snapshot_id", None)
                ):
                    try:
                        orchestrator.begin_step_transaction()
                    except Exception as _step_exc:
                        logger.debug("frontier_loop_node: begin_step_transaction skipped: %s", _step_exc)

                iv_baseline = capture_lint_baseline(tool_name, tool_args, workdir, iv_baselines)
                bash_before = capture_workspace_file_state(workdir) if tool_name == "bash" and workdir else {}
                raw_res = await execute_raw_tool(
                    orchestrator,
                    {"name": tool_name, "arguments": tool_args},
                    model_tier,
                    run_async=False,
                    log=logger,
                )
                if tool_name == "bash":
                    record_bash_mutations(orchestrator, workdir, bash_before)
            paired_outcomes = [(item, raw_res, iv_baseline)]

        for (call_id, tool_name, tool_args, _), raw_res, iv_baseline in paired_outcomes:
            if not isinstance(raw_res, dict):
                raw_res = {} if raw_res is None else {"ok": False, "error": str(raw_res)}
            tool_result = cast(Dict[str, Any], raw_res)
            tool_result = _sanitize_tool_output(tool_result)

            tool_call_count += 1
            last_result = tool_result
            last_tool = tool_name
            if isinstance(last_tool_args_out, dict):
                last_tool_args_out.clear()
                last_tool_args_out.update(tool_args)

            if last_failed_sig is not None:
                if is_tool_result_failure(tool_result):
                    last_failed_sig[:] = [compute_tool_call_signature(tool_name, tool_args)]
                else:
                    last_failed_sig.clear()

            try:
                if bus:
                    from src.core.messaging.events import ToolResult

                    bus.publish_typed(ToolResult(tool=tool_name, result=tool_result, turn=turns_taken))
            except Exception:
                pass

            refresh_index_after_write(tool_name, tool_args, workdir)
            append_tool_result_to_history(
                history=history,
                use_native_tools=use_native_tools,
                call_id=call_id,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=tool_result,
                state=state,
                tool_call_count=tool_call_count,
                truncate_fn=_truncate_tool_output,
            )

            iv_record = run_inline_check(tool_name, tool_args, tool_result, workdir, iv_baseline)
            if iv_record is not None:
                inline_verifications.append(iv_record)
                if isinstance(verified_hashes, dict):
                    update_verified_file_hashes(tool_args, workdir, iv_record, verified_hashes)
                iv_message = build_inline_verification_message(iv_record)
                if iv_message:
                    history.append(iv_message)

        if tool_call_count >= max_tool_calls:
            break

    return history, tool_call_count, last_result, last_tool, inline_verifications


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
    max_tool_calls: int = int(state.get("max_tool_calls") or 30)
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
    if _MAX_FRONTIER_TURNS != _DEFAULT_MAX_FRONTIER_TURNS:
        effective_max_turns = _MAX_FRONTIER_TURNS
    elif isinstance(runtime_policy, dict) and runtime_policy.get("turn_limit"):
        effective_max_turns = int(runtime_policy["turn_limit"])
    elif state.get("max_turns"):
        effective_max_turns = int(state["max_turns"])
    else:
        effective_max_turns = _MAX_FRONTIER_TURNS

    max_parallel_reads = (
        int(runtime_policy.get("max_parallel_reads") or 1)
        if isinstance(runtime_policy, dict) and runtime_policy.get("readonly_parallelism", True)
        else (3 if model_tier in ("frontier", "large", "medium") else 1)
    )

    analyst_findings: str = (state.get("analyst_findings") or "").strip()
    if analyst_findings:
        task = f"{task}\n\n<analyst_findings>\n{analyst_findings}\n</analyst_findings>"

    try:
        repo_context: str = build_repo_context_block(state, task)
    except Exception as exc:
        logger.debug("frontier_loop_node: repo context unavailable: %s", exc)
        repo_context = ""
    if repo_context:
        task = f"{task}\n\n{repo_context}"

    current_plan = state.get("current_plan")
    if isinstance(current_plan, list) and current_plan:
        steps_str = "\n".join(
            f"{i + 1}. {'[x]' if s.get('completed') else '[ ]'} {s.get('description', '')}"
            for i, s in enumerate(current_plan)
        )
        task = f"{task}\n\nExecution Plan (split steps to execute):\n{steps_str}"

    retrieved_snippets = state.get("retrieved_snippets")
    if retrieved_snippets is None and orchestrator:
        try:
            from src.core.orchestration.graph.nodes.repo_context import _retrieve_context

            retrieved_snippets = await _retrieve_context(state, orchestrator)
        except Exception:
            retrieved_snippets = []

    provider_name: str | None = None
    model_name: str | None = None
    supports_native: bool = True
    try:
        from src.core.inference.provider_utils import resolve_provider_capabilities

        caps = resolve_provider_capabilities(
            orchestrator, getattr(orchestrator, "adapter", None) if orchestrator else None
        )
        provider_name = caps.get("provider_name") or None
        model_name = caps.get("model") or None
        supports_native = bool(caps.get("supports_native_tools", True))
    except Exception:
        provider_name = None
        model_name = None

    kv_gov: KVCacheGovernor | None = None
    try:
        if model_name:
            kv_gov = create_governor_for_model(model_name)
    except Exception:
        pass

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

    bus: Any = getattr(orchestrator, "event_bus", None) if orchestrator else None
    try:
        if bus:
            bus.publish_typed(
                AgentStatus(status="working", node="frontier_loop", task=redact_secrets(task[:100]))
            )
    except Exception:
        pass

    while turns_taken < effective_max_turns:
        turns_taken += 1

        # MID-INJ: Drain any in-flight operator injections from the bridge buffer
        inj_source = state.get("_pending_injections_source") or getattr(
            orchestrator, "_injection_source", None
        )
        if inj_source is not None and hasattr(inj_source, "pop_pending_injections"):
            try:
                mid_run_injections = inj_source.pop_pending_injections()
                if mid_run_injections:
                    history = list(history)
                    for inj_text in mid_run_injections:
                        if inj_text and isinstance(inj_text, str):
                            logger.info(
                                "frontier_loop_node: drained operator mid-run steering injection: %s",
                                inj_text[:60],
                            )
                            history.append(
                                {
                                    "role": "user",
                                    "content": f"<operator_steering>\n{inj_text}\n</operator_steering>",
                                }
                            )
            except Exception as inj_exc:
                logger.debug("frontier_loop_node: pop_pending_injections failed: %s", inj_exc)

        if tool_call_count >= max_tool_calls:
            logger.warning(
                "frontier_loop_node: tool budget exhausted (%d/%d), exiting loop",
                tool_call_count,
                max_tool_calls,
            )
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
            tool_calls = [pending_action]
            pending_action = None
            normalized_calls = normalize_tool_calls_with_ids(tool_calls, turns_taken)
            if use_native_tools:
                if history and history[-1].get("role") == "assistant" and history[-1].get("tool_calls"):
                    existing_tcs = history[-1]["tool_calls"]
                    if existing_tcs and isinstance(existing_tcs[0], dict) and existing_tcs[0].get("id"):
                        cid = str(existing_tcs[0]["id"])
                        _, tn, ta, otc = normalized_calls[0]
                        otc = {**otc, "id": cid}
                        normalized_calls = [(cid, tn, ta, otc)]
                else:
                    history = list(history)
                    history.append(
                        build_native_assistant_message(
                            "", [item[3] for item in normalized_calls]
                        )
                    )
            else:
                history = _append_tool_call_history(history, tool_calls)
        else:
            try:
                messages = await _prepare_turn_messages(
                    orchestrator=orchestrator,
                    task=task,
                    history=history,
                    model_tier=model_tier,
                    turns_taken=turns_taken,
                    retrieved_snippets=retrieved_snippets,
                    role=role,
                    runtime_policy=runtime_policy,
                )
            except TypeError:
                try:
                    messages = await _prepare_turn_messages(
                        orchestrator=orchestrator,
                        task=task,
                        history=history,
                        model_tier=model_tier,
                        turns_taken=turns_taken,
                        retrieved_snippets=retrieved_snippets,
                        role=role,
                    )
                except TypeError:
                    messages = await _prepare_turn_messages(
                        orchestrator=orchestrator,
                        task=task,
                        history=history,
                        model_tier=model_tier,
                        turns_taken=turns_taken,
                    )

            try:
                try:
                    response, content_text, tool_calls = await _call_llm_for_turn(
                        messages=messages,
                        provider_name=provider_name,
                        model_name=model_name,
                        orchestrator=orchestrator,
                        turns_taken=turns_taken,
                        model_tier=model_tier,
                        role=role,
                        runtime_policy=runtime_policy,
                    )
                except TypeError:
                    response, content_text, tool_calls = await _call_llm_for_turn(
                        messages=messages,
                        provider_name=provider_name,
                        model_name=model_name,
                        orchestrator=orchestrator,
                        turns_taken=turns_taken,
                        model_tier=model_tier,
                        role=role,
                    )
            except Exception as exc:
                error_str = str(exc)
                logger.warning("frontier_loop_node: LLM call failed: %s", error_str)
                _notify_provider_limit(error_str)
                if _is_context_overflow(error_str):
                    errors = list(errors) + ["context_overflow"]
                break

            if (isinstance(response, dict) and response.get("context_overflow") is True) or _is_context_overflow(
                content_text
            ):
                logger.warning("frontier_loop_node: context overflow in LLM response")
                errors = list(errors) + ["context_overflow"]
                break

            if isinstance(response, dict):
                prompt_toks = int(response.get("prompt_tokens") or 0)
                if isinstance(runtime_policy, dict) and prompt_toks > 0:
                    ctx_win = int(runtime_policy.get("context_window") or 0)
                    trig_ratio = float(runtime_policy.get("compaction_trigger_ratio") or 0.80)
                    if ctx_win >= 1024 and prompt_toks >= int(ctx_win * trig_ratio) and not tool_calls:
                        errors = list(errors) + ["context_overflow"]
                        break

                if kv_gov is not None:
                    resp_total = int(response.get("total_tokens") or 0)
                    if resp_total <= 0:
                        resp_total = prompt_toks + int(response.get("completion_tokens") or 0)
                    if resp_total > 0:
                        kv_state = kv_gov.on_context_update(resp_total)
                        if kv_state.action in (CompactionAction.COMPACT, CompactionAction.FORCE_COMPACT):
                            errors = list(errors) + ["context_overflow"]
                            break

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
                        "empty_response",
                        "empty_inspection",
                    )
                    and incomplete_nudges < _MAX_INCOMPLETE_NUDGES
                    and turns_taken < effective_max_turns
                ):
                    incomplete_nudges += 1
                    history = list(history)
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                f"<system_reminder>\nIncomplete turn ({err_code}): "
                                f"{last_result.get('error')}\nCall the appropriate tool "
                                "to complete the requested work before finishing.\n</system_reminder>"
                            ),
                        }
                    )
                    continue
                errors = list(errors) + [err_code]
            else:
                errors = clear_transient_contract_errors(errors)
            break

        # ---- Plan-mode gate ----------------------------------------------
        for tc in tool_calls:
            tool_name, tool_args = _normalize_tool_call(tc)
            if tool_name and tool_name in _MODIFYING_TOOL_NAMES:
                mutating_tool_calls_count += 1
            if tool_name and orchestrator and _plan_mode_blocks(orchestrator, tool_name):
                logger.info(
                    "frontier_loop_node: plan-mode blocked '%s' — suspending for approval",
                    tool_name,
                )
                return build_plan_mode_blocked_exit(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    history=history,
                    tool_call_count=tool_call_count,
                    last_result=last_result,
                    last_tool_name=last_tool_name,
                    errors=errors,
                    turns_taken=turns_taken,
                    state=state,
                )

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

        if tool_call_count >= max_tool_calls:
            break

    try:
        if bus:
            bus.publish_typed(
                AgentStatus(
                    status="idle",
                    node="frontier_loop",
                    turns=turns_taken,
                    tool_calls=tool_call_count,
                )
            )
    except Exception:
        pass

    return finalize_frontier_exit(
        state=state,
        orchestrator=orchestrator,
        history=history,
        tool_call_count=tool_call_count,
        last_result=last_result,
        last_tool_name=last_tool_name,
        errors=errors,
        turns_taken=turns_taken,
        inline_verifications=inline_verifications,
        replan_required=replan_required,
        retrieved_snippets=retrieved_snippets,
        last_tool_args=last_tool_args or None,
        verified_file_hashes=verified_hashes or None,
    )


# Canonical alias for the unified 6-node architecture
agent_loop_node = frontier_loop_node
