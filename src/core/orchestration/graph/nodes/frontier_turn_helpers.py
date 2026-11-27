"""Turn preparation, LLM invocation, and wave tool-dispatch helpers for frontier_loop_node.

Extracted under TASK-43 so ``frontier_loop_node.py`` stays under the 600-LOC node
budget while preserving all module-level monkeypatch entry points
(``call_model``, ``_prepare_turn_messages``, ``_call_llm_for_turn``,
``_dispatch_tool_calls``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, cast

from src.core.orchestration.graph.completion_contract import _MODIFYING_TOOL_NAMES
from src.core.orchestration.graph.nodes.frontier_dispatch import (
    append_tool_result_to_history,
    capture_workspace_file_state,
    check_repeat_block,
    compute_tool_call_signature,
    execute_raw_tool,
    is_tool_result_failure,
    normalize_tool_calls_with_ids,
    partition_tool_call_waves,
    record_bash_mutations,
    update_failed_signature,
)
from src.core.orchestration.graph.nodes.frontier_messages import (
    _extract_content_text,
    _normalize_messages_for_native_tools,
    apply_thinking_suppression,
)
from src.core.orchestration.graph.nodes.frontier_toolcalls import _render_tool_calls_text
from src.core.orchestration.graph.nodes.in_loop_verification import (
    build_inline_verification_message,
    capture_lint_baseline,
    run_inline_check,
    update_verified_file_hashes,
)
from src.core.orchestration.graph.nodes.repo_context import refresh_index_after_write
from src.core.orchestration.graph.nodes.tool_output_truncation import prune_tool_outputs
from src.core.orchestration.graph.runtime_policy import resolve_effective_tool_schemas
from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.tool_parser import parse_response_tool_call, parse_tool_block

logger = logging.getLogger(__name__)


async def prepare_turn_messages_impl(
    orchestrator: Any,
    task: str,
    history: list,
    model_tier: str,
    turns_taken: int,
    retrieved_snippets: list | None = None,
    role: str = "operational",
    runtime_policy: Any = None,
    thinking_mode: str | None = None,
    model_name: str | None = None,
    tools_schema: list | None = None,
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
        if tools_schema is None:
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
            model_name,
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
        return apply_thinking_suppression(messages, thinking_mode, model_name)
    except Exception as exc:
        logger.warning("frontier_loop_node: context build failed: %s", exc)
        return [
            {
                "role": "system",
                "content": f"You are a capable coding assistant. Task: {task}",
            }
        ]


async def call_llm_for_turn_impl(
    messages: list,
    provider_name: str | None,
    model_name: str | None,
    orchestrator: Any,
    turns_taken: int,
    *,
    call_model_fn: Callable[..., Awaitable[Any]],
    model_tier: str = "frontier",
    role: str = "operational",
    runtime_policy: Any = None,
    thinking_mode: str | None = None,
    tools_schema: list | None = None,
) -> tuple[Any, str, list]:
    """Call the LLM via *call_model_fn* and extract ``(response, content_text, tool_calls)``."""
    if tools_schema is None:
        tools_schema = resolve_effective_tool_schemas(
            orchestrator, runtime_policy, active_role=role
        )

    _llm_timeout: int | None = 120
    if (provider_name or "").lower() in ("", "ollama", "llama_server", "lm_studio", "mlx_lm"):
        _llm_timeout = int(float(os.getenv("CODINGAGENT_LOCAL_TIMEOUT", "600")))
    try:
        from src.core.orchestration.project_settings import get_active_settings as _gas_fl

        _ps_fl = _gas_fl()
        if _ps_fl is not None and _ps_fl.max_llm_wait_seconds:
            _llm_timeout = _ps_fl.max_llm_wait_seconds
    except Exception:
        pass

    _call_kwargs: Dict[str, Any] = dict(
        messages=messages,
        provider=provider_name,
        model=model_name,
        tools=tools_schema or None,
    )
    if thinking_mode == "off":
        _call_kwargs["disable_thinking"] = True
    _pol_tier = getattr(runtime_policy, "policy_tier", None) if runtime_policy is not None else None
    if _pol_tier:
        _call_kwargs["policy_tier"] = str(_pol_tier)
    _ver_retries = int(getattr(orchestrator, "_last_verification_retries", 0) or 0)
    _nudge_retries = int(getattr(orchestrator, "_incomplete_nudge_retries", 0) or 0)
    _retry_idx = max(_ver_retries, _nudge_retries)
    if _retry_idx > 0:
        from src.core.orchestration.loop_guards import compute_retry_temperature

        if _ver_retries > 0:
            _call_kwargs["verification_retries"] = _ver_retries
        _call_kwargs["temperature"] = compute_retry_temperature(0.2, _retry_idx)
    response = await asyncio.wait_for(call_model_fn(**_call_kwargs), timeout=_llm_timeout)

    content_text = _extract_content_text(response)

    tool_calls: list[Any] = []
    reasoning_text: str | None = None
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices and len(choices) > 0:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            if isinstance(msg, dict):
                tool_calls = list(msg.get("tool_calls") or [])
                reasoning_text = msg.get("reasoning_content") or msg.get("thinking")
            if not tool_calls and isinstance(choice, dict):
                tool_calls = list(choice.get("tool_calls") or [])
        if not tool_calls and response.get("tool_calls"):
            tool_calls = list(response.get("tool_calls") or [])
    else:
        tool_calls = list(getattr(response, "tool_calls", None) or [])

    # Text-based tool parser fallback + reasoning_content rescue (TASK-29)
    if not tool_calls and (content_text or reasoning_text):
        try:
            content_for_parse = re.sub(
                r"<\|channel\|>thought.*?<\|/channel\|>", "", content_text or "", flags=re.DOTALL
            ).strip()
            parsed = parse_response_tool_call(content_for_parse, reasoning_text)
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


async def dispatch_tool_calls_impl(
    tool_calls: list,
    history: list,
    state: StateLike,
    orchestrator: Any,
    model_tier: str,
    bus: Any,
    turns_taken: int,
    tool_call_count: int,
    max_tool_calls: int,
    iv_baselines: dict,
    *,
    sanitize_fn: Callable[[dict], dict],
    truncate_fn: Callable[[dict], dict],
    execute_raw_tool_fn: Callable[..., Awaitable[Any]] = execute_raw_tool,
    use_native_tools: bool = False,
    max_parallel_reads: int = 1,
    normalized_calls: list | None = None,
    last_failed_sig: list | None = None,
    last_tool_args_out: dict | None = None,
    verified_hashes: dict | None = None,
) -> tuple[list, int, Dict[str, Any] | None, str | None, list]:
    """Execute tool calls in ordered waves (parallel read-only waves + serial mutations)."""
    last_result: Dict[str, Any] | None = None
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
            was_repeat_blocked = False
            raw_results = await asyncio.gather(
                *[
                    execute_raw_tool_fn(
                        orchestrator,
                        {"name": t_name, "arguments": t_args},
                        model_tier,
                        run_async=True,
                        log=logger,
                    )
                    for (_cid, t_name, t_args, _otc) in active_items
                ]
            )
            paired_outcomes: list[tuple[Any, Any, Any]] = [
                (item, raw_res, None) for item, raw_res in zip(active_items, raw_results)
            ]
        else:
            item = active_items[0]
            call_id, tool_name, tool_args, _ = item
            sig = compute_tool_call_signature(tool_name, tool_args)
            blocked_res = check_repeat_block(last_failed_sig, sig, tool_name)
            was_repeat_blocked = blocked_res is not None
            if blocked_res is not None:
                raw_res, iv_baseline = blocked_res, None
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
                raw_res = await execute_raw_tool_fn(
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
            tool_result = sanitize_fn(tool_result)

            tool_call_count += 1
            last_result = tool_result
            last_tool = tool_name
            if isinstance(last_tool_args_out, dict):
                last_tool_args_out.clear()
                last_tool_args_out.update(tool_args)

            update_failed_signature(
                last_failed_sig,
                was_repeat_blocked,
                is_tool_result_failure(tool_result),
                compute_tool_call_signature(tool_name, tool_args),
                tool_result,
            )

            try:
                if bus:
                    from src.core.messaging.event_types import ToolResult

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
                truncate_fn=truncate_fn,
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


def enrich_loop_task(state: StateLike, task: str, repo_context: str) -> str:
    """Attach analyst findings, repo context, and active plan steps to the loop task."""
    analyst_findings: str = (state.get("analyst_findings") or "").strip()
    if analyst_findings:
        task = f"{task}\n\n<analyst_findings>\n{analyst_findings}\n</analyst_findings>"
    if repo_context:
        task = f"{task}\n\n{repo_context}"
    current_plan = state.get("current_plan")
    if isinstance(current_plan, list) and current_plan:
        steps_str = "\n".join(
            f"{i + 1}. {'[x]' if s.get('completed') else '[ ]'} {s.get('description', '')}"
            for i, s in enumerate(current_plan)
        )
        task = f"{task}\n\nExecution Plan (split steps to execute):\n{steps_str}"
    return task


def drain_operator_injections(
    state: StateLike, orchestrator: Any, history: list
) -> list:
    """Drain any in-flight operator steering injections into *history*."""
    inj_source = state.get("_pending_injections_source") or getattr(
        orchestrator, "_injection_source", None
    )
    if inj_source is None or not hasattr(inj_source, "pop_pending_injections"):
        return history
    try:
        mid_run_injections = inj_source.pop_pending_injections()
        if not mid_run_injections:
            return history
        updated = list(history)
        for inj_text in mid_run_injections:
            if inj_text and isinstance(inj_text, str):
                logger.info(
                    "frontier_loop_node: drained operator mid-run steering injection: %s",
                    inj_text[:60],
                )
                updated.append(
                    {
                        "role": "user",
                        "content": f"<operator_steering>\n{inj_text}\n</operator_steering>",
                    }
                )
        return updated
    except Exception as inj_exc:
        logger.debug("frontier_loop_node: pop_pending_injections failed: %s", inj_exc)
        return history


def response_triggers_compaction(
    response: Any,
    content_text: str,
    tool_calls: list,
    runtime_policy: Any,
    kv_gov: Any,
    is_context_overflow_fn: Callable[[str], bool],
) -> bool:
    """Return True when LLM response signals context overflow or KV-governor compaction."""
    from src.core.inference.kv_cache_governor import CompactionAction

    if (isinstance(response, dict) and response.get("context_overflow") is True) or is_context_overflow_fn(
        content_text
    ):
        logger.warning("frontier_loop_node: context overflow in LLM response")
        return True

    if isinstance(response, dict):
        prompt_toks = int(response.get("prompt_tokens") or 0)
        if isinstance(runtime_policy, dict) and prompt_toks > 0:
            ctx_win = int(runtime_policy.get("context_window") or 0)
            trig_ratio = float(runtime_policy.get("compaction_trigger_ratio") or 0.80)
            if ctx_win >= 1024 and prompt_toks >= int(ctx_win * trig_ratio) and not tool_calls:
                return True

        if kv_gov is not None:
            resp_total = int(response.get("total_tokens") or 0)
            if resp_total <= 0:
                resp_total = prompt_toks + int(response.get("completion_tokens") or 0)
            if resp_total > 0:
                kv_state = kv_gov.on_context_update(resp_total)
                if kv_state.action in (CompactionAction.COMPACT, CompactionAction.FORCE_COMPACT):
                    return True
    return False


def resolve_provider_and_governor(
    orchestrator: Any, state: StateLike
) -> tuple[str | None, str | None, bool, Any]:
    """Resolve provider capabilities and an optional KV-cache governor."""
    from src.core.inference.kv_cache_governor import get_or_create_governor_for_orchestrator

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

    kv_gov: Any = None
    try:
        if model_name:
            kv_gov = get_or_create_governor_for_orchestrator(
                orchestrator, model_name, state=dict(state)
            )
    except Exception:
        pass
    return provider_name, model_name, supports_native, kv_gov


def resume_pending_action(
    pending_action: Dict[str, Any],
    history: list,
    turns_taken: int,
    use_native_tools: bool,
) -> tuple[list, list, list]:
    """Resume an approved ``pending_action`` into ``(tool_calls, normalized_calls, history)``."""
    from src.core.orchestration.graph.nodes.frontier_dispatch import (
        build_native_assistant_message,
        normalize_tool_calls_with_ids,
    )
    from src.core.orchestration.graph.nodes.frontier_toolcalls import (
        _append_tool_call_history,
    )

    tool_calls = [pending_action]
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
                build_native_assistant_message("", [item[3] for item in normalized_calls])
            )
    else:
        history = _append_tool_call_history(history, tool_calls)
    return tool_calls, normalized_calls, history
