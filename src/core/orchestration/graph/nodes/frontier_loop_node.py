"""frontier_loop_node.py — Tight LLM+tool loop for LARGE/FRONTIER models.

Keeps LLM+tool calls in one node instead of round-tripping the LangGraph state
machine per tool call.  Exits on: no tool calls, budget exhausted, plan-mode
write gate, context overflow, or max turns.  Sets last_result, last_tool_name,
tool_call_count, _frontier_loop_turns, awaiting_plan_approval, and errors.
Used by the tier-aware ``build_tier_graph()``."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, cast

from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.graph.nodes.execution_plan import (
    build_read_then_write_result,
    build_tool_history_messages,
    compute_replan_trigger,
)
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    TOOL_LARGE_TEXT_FIELDS,
    TOOL_OUTPUT_MAX_BYTES,
    prune_tool_outputs,
    truncate_tool_output,
)
from src.core.orchestration.graph.nodes.node_utils import _notify_provider_limit, _resolve_orchestrator
from src.core.orchestration.graph.nodes.repo_context import (
    build_repo_context_block,
    refresh_index_after_write,
)
from src.core.orchestration.graph.nodes.frontier_toolcalls import (
    _append_tool_call_history,
    _normalize_tool_call,
    _render_tool_calls_text,
)
from src.core.orchestration.graph.nodes.frontier_messages import (
    _extract_tool_call_from_text, _normalize_messages_for_native_tools,
)
from src.core.orchestration.graph.completion_contract import (
    _MODIFYING_TOOL_NAMES,
)
from src.core.orchestration.graph.nodes.frontier_completion import (
    _filter_tools_for_tier,
    _is_context_overflow,
    _plan_mode_blocks,
    clear_transient_contract_errors,
    finalize_frontier_exit,
    handle_no_tool_completion,
)
from src.core.orchestration.graph.nodes.in_loop_verification import (
    build_inline_verification_message,
    capture_lint_baseline,
    run_inline_check,
)
from src.core.orchestration.graph.nodes.tool_result_safety import sanitize_tool_result
from src.core.security.redaction import redact_secrets

# Re-exported for backwards compatibility with the legacy test surface
__all__ = ["frontier_loop_node", "_append_tool_call_history", "_extract_tool_call_from_text", "_normalize_messages_for_native_tools", "_normalize_tool_call", "_render_tool_calls_text"]
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.inference.kv_cache_governor import (
    CompactionAction,
    KVCacheGovernor,
    create_governor_for_model,
)

logger = logging.getLogger(__name__)
from src.core.messaging.event_types import AgentStatus  # noqa: E402

# Core tools for small/local models.  Larger tiers get core + extras up to
# their ``get_tool_limit()`` via ``_filter_tools_for_tier``.


# Maximum turns within one invocation of frontier_loop_node.
# Each "turn" is one LLM call + its tool calls.
# This bounds the node's wall-clock time and prevents runaway infinite loops.
# Individual tool-call budget is still governed by state["max_tool_calls"].
_MAX_FRONTIER_TURNS = 20

# P3.1: how many in-loop verification failures the model may accumulate in one
# invocation before the loop gives up and exits.  Each failure is fed back as a
# message, so the cap is what stops "write broken file -> be told -> write it
# again" from burning the whole turn budget.
_MAX_INLINE_VERIFICATION_FAILURES = 3

# OP-9: Cap per-tool output that enters conversation history.
_TOOL_OUTPUT_MAX_BYTES = TOOL_OUTPUT_MAX_BYTES
_TOOL_LARGE_TEXT_FIELDS = TOOL_LARGE_TEXT_FIELDS

# Expose a module-level call_model proxy so tests can patch
async def call_model(*args, **kwargs):
    """Proxy to the real call_model in src.core.inference.llm_manager.

    Keeping this as a tiny proxy avoids import-time cycles and makes the
    symbol patchable from tests (they patch this module's attribute).
    """
    from src.core.inference.llm_manager import call_model as _call
    return await _call(*args, **kwargs)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_tool_output(res: dict) -> dict:
    """Cap any large text fields in a tool result before it enters history."""
    return truncate_tool_output(res, marker_label="frontier_loop")

def _sanitize_tool_output(res: dict) -> dict:
    """P1.1: the only production path where untrusted content enters context.

    Policy (and why detection precedes truncation) lives in
    ``tool_result_safety.sanitize_tool_result``.
    """
    return sanitize_tool_result(res, marker_label="frontier_loop", log=logger)


def _extract_content_text(response: Any) -> str:
    """Extract text content from an LLM response object."""
    try:
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices and isinstance(choices, list):
                msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, str):
                                parts.append(item)
                            elif isinstance(item, dict):
                                parts.append(item.get("text", ""))
                        return "\n".join(p for p in parts if p)
        content = response.content if hasattr(response, "content") else response  # type: ignore[union-attr]
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text", ""))
            return "\n".join(p for p in parts if p)
    except Exception:
        pass
    return ""



# ---------------------------------------------------------------------------
# Turn helpers extracted from the main loop
# ---------------------------------------------------------------------------


async def _prepare_turn_messages(
    orchestrator: Any,
    task: str,
    history: list,
    model_tier: str,
    turns_taken: int,
    retrieved_snippets: "list | None" = None,
    role: str = "operational",
) -> list:
    """Build the messages list for one LLM turn.

    Returns a list of chat messages ready to pass to ``call_model``.
    Falls back to a minimal system prompt on any error.
    """
    try:
        from src.core.context.context_builder import ContextBuilder
        from src.core.inference.provider_context import get_context_budget
        from src.core.orchestration.event_bus import run_with_correlation
        from src.core.orchestration.provider_capabilities import (
            resolve_provider_capabilities as _resolve_pc,
        )
        import functools

        cb = ContextBuilder(working_dir=getattr(orchestrator, "working_dir", None))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[frontier_loop_node] turn=%d history_before_prune=%s", turns_taken, [(m.get("role"), redact_secrets(str(m.get("content", "")))[:100]) for m in history])
        history = prune_tool_outputs(history)

        token_budget = get_context_budget(model_tier=model_tier)

        tools_schema: list = []
        if orchestrator:
            try:
                reg = getattr(orchestrator, "tool_registry", None)
                if reg and hasattr(reg, "get_openai_functions"):
                    tools_schema = reg.get_openai_functions()
            except Exception:
                pass

        if orchestrator and hasattr(orchestrator, "get_tools_for_role"):
            try:
                role_tools = orchestrator.get_tools_for_role(role)
                if role_tools:
                    role_names = {t["name"] for t in role_tools}
                    tools_schema = [
                        t for t in tools_schema
                        if t.get("name") in role_names
                        or t.get("function", {}).get("name") in role_names
                    ]
            except Exception:
                pass

        tools_schema = _filter_tools_for_tier(tools_schema, model_tier)

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

        if (
            provider_caps.get("provider_family") == "local"
            and provider_caps.get("supports_native_tools")
        ):
            _sys_content = (
                "You are a coding assistant with access to tools. "
                "IMPORTANT: You MUST use the provided function-calling tools to complete tasks. "
                "Do NOT write code or answers in plain text. "
                "Always call a tool (read_file, write_file, bash, etc.) to take action. "
                f"Current task: {task}"
            )
            _has_sys = False
            for _i, _m in enumerate(messages):
                if isinstance(_m, dict) and _m.get("role") == "system":
                    messages[_i] = {"role": "system", "content": _sys_content}
                    _has_sys = True
                    break
            if not _has_sys:
                messages = [{"role": "system", "content": _sys_content}] + messages

            messages = _normalize_messages_for_native_tools(messages)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[frontier_loop_node] turn=%d after_normalization=%s", turns_taken, [(m.get("role"), redact_secrets(str(m.get("content") or m.get("tool_calls") or ""))[:100]) for m in messages])
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
) -> "tuple[Any, str, list]":
    """Call the LLM and extract ``(response, content_text, tool_calls)``.

    Raises any LLM exception so the caller can decide how to handle it.
    """
    tools_schema: list = []
    if orchestrator:
        try:
            reg = getattr(orchestrator, "tool_registry", None)
            if reg and hasattr(reg, "get_openai_functions"):
                tools_schema = reg.get_openai_functions()
        except Exception:
            pass

    if orchestrator and hasattr(orchestrator, "get_tools_for_role"):
        try:
            role_tools = orchestrator.get_tools_for_role(role)
            if role_tools:
                role_names = {t["name"] for t in role_tools}
                tools_schema = [
                    t for t in tools_schema
                    if t.get("name") in role_names
                    or t.get("function", {}).get("name") in role_names
                ]
        except Exception:
            pass

    tools_schema = _filter_tools_for_tier(tools_schema, model_tier)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("[frontier_loop_node] turn=%d messages_preview=%s", turns_taken, [(m.get("role"), redact_secrets(str(m.get("content") or m.get("tool_calls") or ""))[:100]) for m in messages])
        logger.debug("[frontier_loop_node] turn=%d messages_has_tool_calls=%s", turns_taken, [bool(m.get("tool_calls")) for m in messages])

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

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[frontier_loop_node] turn=%d tools_sent=%d finish_reason=%s tool_calls_len=%s content_len=%s",
            turns_taken,
            len(tools_schema),
            response.get("finish_reason") if isinstance(response, dict) else "n/a",
            len(response.get("tool_calls") or []) if isinstance(response, dict) else "n/a",
            len(response.get("content", "") or "") if isinstance(response, dict) else "n/a",
        )

    content_text = _extract_content_text(response)

    # Extract native tool calls
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices and len(choices) > 0:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            tool_calls: list[Any] = []
            if isinstance(msg, dict):
                tool_calls = msg.get("tool_calls") or []
            if not tool_calls and isinstance(choice, dict):
                tool_calls = choice.get("tool_calls") or []
        else:
            tool_calls = []
    else:
        tool_calls = getattr(response, "tool_calls", None) or []

    # Text-based fallback
    if not tool_calls and content_text:
        try:
            import re

            content_for_parse = re.sub(
                r"", "", content_text, flags=re.DOTALL
            ).strip()
            content_for_parse = re.sub(
                r"<\|channel\|>thought.*?<\|/channel\|>",
                "",
                content_for_parse,
                flags=re.DOTALL,
            ).strip()
            if content_for_parse:
                parsed = parse_tool_block(content_for_parse)
                if parsed and isinstance(parsed, dict) and (parsed.get("name") or parsed.get("tool")):
                    tool_calls = [parsed]
                    logger.info("frontier_loop_node: parsed tool call: %r", parsed.get("name"))
        except Exception as exc:
            logger.debug("frontier_loop_node: tool parse error: %s", exc)
            tool_calls = []

    # Synthesize tool-call text into content when needed
    synthesized_tool_text = ""
    if tool_calls and not parse_tool_block(content_text or ""):
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
) -> "tuple[list, int, Dict[str, Any] | None, str | None, list]":
    """Execute tool calls; return ``(history, count, last_result, last_tool, iv)``.

    Stops at the budget; callers should re-check ``count >= max_tool_calls``.
    ``last_tool`` is what the graph uses to gate verification (P2.3).
    ``iv`` is the P3.1 in-loop verification record list; failed records have
    already been appended to ``history`` as model-facing feedback.
    ``iv_baselines`` is the caller's per-invocation lint baseline cache.
    """
    last_result: "Dict[str, Any] | None" = None
    last_tool: str | None = None
    inline_verifications: list = []

    for tc in tool_calls:
        tool_name, tool_args = _normalize_tool_call(tc)
        if not tool_name:
            continue

        logger.info("frontier_loop_node: executing tool %r (turn %d)", tool_name, turns_taken)

        # SPEC-08: Begin step transaction before first mutating tool call in this loop visit
        if orchestrator and hasattr(orchestrator, "begin_step_transaction"):
            if tool_name in _MODIFYING_TOOL_NAMES and not getattr(orchestrator, "_step_snapshot_id", None):
                try:
                    orchestrator.begin_step_transaction()
                except Exception as _step_exc:
                    logger.debug("frontier_loop_node: begin_step_transaction skipped: %s", _step_exc)

        # P3.1: snapshot the target's lint state *before* the write so the
        # post-tool checkpoint only reports newly introduced problems.
        workdir = str(state.get("working_dir") or "")
        iv_baseline = capture_lint_baseline(tool_name, tool_args, workdir, iv_baselines)

        # Execute
        action = {"name": tool_name, "arguments": tool_args}
        try:
            if orchestrator:
                if model_tier in ("large", "frontier", "medium"):
                    from src.core.orchestration.event_bus import run_with_correlation

                    loop = asyncio.get_running_loop()
                    tool_result = await run_with_correlation(
                        loop, None, orchestrator.execute_tool, action
                    )
                else:
                    tool_result = orchestrator.execute_tool(action)
            else:
                tool_result = {"ok": False, "error": "orchestrator unavailable"}
        except Exception as exc:
            logger.warning("frontier_loop_node: tool '%s' raised: %s", tool_name, exc)
            tool_result = {"ok": False, "error": str(exc)}

        if not isinstance(tool_result, dict):
            tool_result = (
                {} if tool_result is None else {"ok": False, "error": str(tool_result)}
            )
        tool_result = cast(Dict[str, Any], tool_result)
        tool_result = _sanitize_tool_output(tool_result)
        tool_call_count += 1
        last_result = tool_result
        last_tool = tool_name

        try:
            if bus:
                from src.core.messaging.events import ToolResult
                bus.publish_typed(ToolResult(tool=tool_name, result=tool_result, turn=turns_taken))
        except Exception:
            pass

        # Update file-tracking state
        path_arg = None
        if isinstance(tool_args, dict):
            raw_path = tool_args.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                path_arg = raw_path

        files_read = dict(state.get("files_read") or {})
        tool_last_used = dict(state.get("tool_last_used") or {})
        if path_arg and tool_name in ("read_file", "fs.read"):
            try:
                resolved_path = str(
                    (Path(str(state.get("working_dir") or ".")) / path_arg).resolve()
                )
                files_read[resolved_path] = True
                tool_last_used[f"{tool_name}:{path_arg}"] = tool_call_count
            except Exception:
                pass

        # P1.2: keep the on-disk symbol index consistent after a modifying tool.
        # Unconditional — the helper self-gates and selects its own path keys.
        refresh_index_after_write(
            tool_name, tool_args, str(state.get("working_dir") or "")
        )

        legacy_history = build_tool_history_messages(
            result=tool_result,
            truncate_tool_output=_truncate_tool_output,
        )
        read_then_write_result = build_read_then_write_result(
            state={**state, "tool_call_count": tool_call_count - 1},
            result=tool_result,
            tool_name=tool_name,
            path_arg=path_arg,
            working_dir=str(state.get("working_dir") or "."),
            truncate_tool_output=_truncate_tool_output,
            tool_last_used=tool_last_used,
            files_read=files_read,
        )
        if read_then_write_result and read_then_write_result.get("history"):
            legacy_history = list(read_then_write_result.get("history") or []) + legacy_history

        history = list(history)
        history.extend(legacy_history)

        # P3.1 in-loop verification checkpoint: hand a deterministic failure
        # back to the model inside the same loop so it can self-correct before
        # building on top of a broken file.
        iv_record = run_inline_check(tool_name, tool_args, tool_result, workdir, iv_baseline)
        if iv_record is not None:
            inline_verifications.append(iv_record)
            iv_message = build_inline_verification_message(iv_record)
            if iv_message:
                history.append(iv_message)
                logger.warning(
                    "frontier_loop_node: inline verification failed for %r (%d finding(s))",
                    tool_name,
                    len(iv_record.get("failures") or []),
                )

        if tool_call_count >= max_tool_calls:
            logger.warning(
                "frontier_loop_node: tool budget exhausted after '%s' (%d/%d)",
                tool_name,
                tool_call_count,
                max_tool_calls,
            )
            break

    return history, tool_call_count, last_result, last_tool, inline_verifications


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


async def frontier_loop_node(
    state: StateLike,
    config: Any,
) -> Dict[str, Any]:
    """Tight LLM+tool loop for LARGE/FRONTIER models.

    Keeps looping internally (call LLM → execute tools → loop) rather than
    exiting after every tool call and re-entering via the full LangGraph routing
    pipeline.  This dramatically reduces state-machine overhead for capable models
    that produce high-quality tool plans on the first try.

    Returns a partial AgentState update dict.
    """
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

    # GAP-FRONTIER-3 fix: inject analyst_findings into task context so the LLM
    # benefits from pre-loop analysis produced by analyst_delegation_node.
    analyst_findings: str = (state.get("analyst_findings") or "").strip()
    if analyst_findings:
        task = f"{task}\n\n<analyst_findings>\n{analyst_findings}\n</analyst_findings>"
        logger.info("frontier_loop_node: injecting analyst_findings (%d chars)", len(analyst_findings))

    # P1.2: ground the production loop in repository intelligence.  Previously
    # only the legacy planning/execution nodes consulted the repo index, so the
    # production frontier path ran with no symbol or file awareness at all.
    try:
        repo_context: str = build_repo_context_block(state, task)
    except Exception as exc:
        logger.debug("frontier_loop_node: repo context unavailable (non-critical): %s", exc)
        repo_context = ""
    if repo_context:
        task = f"{task}\n\n{repo_context}"
        logger.info(
            "frontier_loop_node: injected repo_context (%d chars)", len(repo_context)
        )

    retrieved_snippets = state.get("retrieved_snippets")
    if retrieved_snippets is None and orchestrator:
        try:
            from src.core.orchestration.graph.nodes.perception_retrieval import _retrieve_context
            retrieved_snippets = await _retrieve_context(state, orchestrator)
        except Exception:
            retrieved_snippets = []

    provider_name: str | None = None
    model_name: str | None = None

    try:
        from src.core.inference.provider_utils import resolve_provider_capabilities

        caps = resolve_provider_capabilities(orchestrator, getattr(orchestrator, "adapter", None) if orchestrator else None)
        provider_name = caps.get("provider_name") or None
        model_name = caps.get("model") or None
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
    # P2.3: publish the last tool so the lite graph can gate verification.
    last_tool_name: str | None = None
    # P3.1: in-loop verification records for this invocation.
    inline_verifications: list = []
    # Per-invocation lint baselines, captured on first touch of a path so a file
    # the agent breaks keeps being re-reported until it is repaired.
    iv_baselines: dict = {}
    turns_taken: int = 0
    mutating_tool_calls_count: int = 0
    replan_required: str | None = None

    logger.info(
        "frontier_loop_node: starting (task=%r, tool_calls=%d/%d, tier=%s)",
        redact_secrets(task[:80]),
        tool_call_count,
        max_tool_calls,
        model_tier,
    )

    bus: Any = getattr(orchestrator, "event_bus", None) if orchestrator else None
    try:
        if bus:
            bus.publish_typed(AgentStatus(status="working", node="frontier_loop", task=redact_secrets(task[:100])))
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    while turns_taken < _MAX_FRONTIER_TURNS:
        turns_taken += 1

        # ---- Budget check ------------------------------------------------
        if tool_call_count >= max_tool_calls:
            logger.warning(
                "frontier_loop_node: tool budget exhausted (%d/%d), exiting loop",
                tool_call_count,
                max_tool_calls,
            )
            break

        tool_calls: list = []
        content_text: str = ""

        if pending_action is not None:
            tool_calls = [pending_action]
            pending_action = None
            history = _append_tool_call_history(history, tool_calls)
            logger.info(
                "frontier_loop_node: using precomputed next_action before LLM call"
            )
        else:
            role = (
                state.get("active_role")
                or (getattr(orchestrator, "active_role", None) if orchestrator else None)
                or "operational"
            )
            try:
                messages = await _prepare_turn_messages(
                    orchestrator=orchestrator, task=task, history=history,
                    model_tier=model_tier, turns_taken=turns_taken,
                    retrieved_snippets=retrieved_snippets,
                    role=role,
                )
            except TypeError:
                try:
                    messages = await _prepare_turn_messages(
                        orchestrator=orchestrator, task=task, history=history,
                        model_tier=model_tier, turns_taken=turns_taken,
                        role=role,
                    )
                except TypeError:
                    messages = await _prepare_turn_messages(
                        orchestrator=orchestrator, task=task, history=history,
                        model_tier=model_tier, turns_taken=turns_taken,
                    )

            try:
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

            logger.info(
                "frontier_loop_node: turn %d response — content_len=%d, "
                "tool_calls_in_response=%r, finish_reason=%r",
                turns_taken,
                len(content_text),
                bool(
                    isinstance(response, dict)
                    and response.get("choices", [{}])[0]
                    .get("message", {})
                    .get("tool_calls")
                )
                if isinstance(response, dict) and response.get("choices")
                else "n/a",
                (
                    response.get("choices", [{}])[0].get("finish_reason")
                    if isinstance(response, dict) and response.get("choices")
                    else "n/a"
                ),
            )

            if _is_context_overflow(content_text):
                logger.warning("frontier_loop_node: context overflow in LLM response")
                errors = list(errors) + ["context_overflow"]
                break

            if kv_gov is not None and isinstance(response, dict):
                resp_total = int(response.get("total_tokens") or 0)
                if resp_total <= 0:
                    resp_total = int(response.get("prompt_tokens") or 0) + int(response.get("completion_tokens") or 0)
                if resp_total > 0:
                    kv_state = kv_gov.on_context_update(resp_total)
                    if kv_state.action in (CompactionAction.COMPACT, CompactionAction.FORCE_COMPACT):
                        logger.warning(
                            "frontier_loop_node: KV Cache %s at %d tokens (%.0f%% of %d)",
                            kv_state.action.value,
                            resp_total,
                            kv_state.usage_ratio * 100,
                            kv_gov.max_tokens,
                        )
                        errors = list(errors) + ["context_overflow"]
                        break

            # Append assistant message
            history = list(history)
            history.append({"role": "assistant", "content": content_text or ""})

        # ---- No tool calls → evaluate completion contract --------------
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
                errors = list(errors) + [last_result.get("error_code") or "unverified_completion"]
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
                return {
                    "history": history,
                    "tool_call_count": tool_call_count,
                    "last_result": last_result,
                    "last_tool_name": last_tool_name,
                    "errors": errors,
                    "_frontier_loop_turns": turns_taken,
                    "awaiting_plan_approval": True,
                    "next_action": {"name": tool_name, "arguments": tool_args},
                }

        # ---- Execute tool calls ------------------------------------------
        history, tool_call_count, batch_result, batch_tool, batch_iv = await _dispatch_tool_calls(
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
        if batch_result is not None:
            last_result = batch_result
        if batch_tool:
            last_tool_name = batch_tool
        inline_verifications.extend(batch_iv)

        # P3.1: bounded self-correction.  The model gets the failure inline every
        # turn, but repeated unfixed writes must not spin — after the cap the
        # loop exits so verification/evaluation/replan can take over.
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

        # Patch Size Guard: a write flagged ``requires_split`` exits the tight
        # loop so the graph routes through replan (mirrors execution_node).
        _replan_trigger = compute_replan_trigger(result=batch_result or {})
        if _replan_trigger:
            logger.warning(
                "frontier_loop_node: patch too large, triggering replan — %s",
                _replan_trigger.get("replan_required"),
            )
            replan_required = _replan_trigger.get("replan_required")
            break

        if tool_call_count >= max_tool_calls:
            break

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    logger.info(
        "frontier_loop_node: exited after %d turns, %d tool calls",
        turns_taken,
        tool_call_count,
    )

    try:
        if bus:
            bus.publish_typed(AgentStatus(status="idle", node="frontier_loop", turns=turns_taken, tool_calls=tool_call_count))
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
    )
