from src.core.messaging.event_types import ContextOverflow
from langchain_core.runnables import RunnableConfig
import logging
from typing import Mapping, Dict, Any

from src.core.orchestration.graph.state import StateLike, validate_state
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.inference.llm_helpers import call_model_with_timeout
from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    _PRUNE_PROTECT_TOKENS,
    prune_tool_outputs as _prune_tool_outputs,
)

# Subject-matter helpers. Each helper module is self-contained (its runtime
# dependencies and logger are resolved internally), so perception_node imports
# them directly — there is no pass-through wrapper layer.  A few names are
# re-exported purely for the legacy test surface and carry a noqa on import.
from src.core.orchestration.graph.nodes.perception_parsing import (
    _parse_tool_call_and_flags,
    _parse_yaml_tool_call_from_content,  # noqa: F401
)
from src.core.orchestration.graph.nodes.perception_no_tool import (
    _handle_no_tool_or_empty_response,
    _maybe_return_content_after_no_tool_retry,
    _select_corrective_prompt,  # noqa: F401
)
from src.core.orchestration.graph.nodes.perception_retrieval import (
    _retrieve_context,
)
from src.core.orchestration.graph.nodes.perception_post_call import (
    _process_post_call_tokens,
)
from src.core.orchestration.graph.nodes.perception_compaction import (
    _bootstrap_history_for_prompt,
    _run_auto_compaction,
)
from src.core.orchestration.graph.nodes.perception_messages import (
    _build_perception_messages,
)
from src.core.orchestration.graph.nodes.perception_result import (
    _build_perception_result,
)
from src.core.orchestration.graph.nodes.perception_runtime import (
    _build_llm_kwargs,
    _check_small_model_clarification,
    _compute_active_skills_for_task,
    _filter_tools_near_turn_limit,
    _maybe_handle_turn_limit,
    _maybe_warn_small_context_window,
    _resolve_active_model_name,  # noqa: F401
    _resolve_orchestrator_and_cancellation,
    _resolve_perception_provider_context,
    _select_perception_role,
    _validate_call_model_and_adapter,
)

try:
    from src.core.orchestration.project_settings import get_active_settings as _gas
except Exception:
    _gas = None  # type: ignore[assignment]


# Gap 3: Plugin hooks — lazy import so the registry is not required at import time.
try:
    from src.core.plugin.hook_registry import (
        registry as _hook_registry,
        HOOK_ROUND_END as _HOOK_ROUND_END,
    )

    _HAS_HOOKS = True
except Exception:
    _hook_registry = None  # type: ignore[assignment]
    _HOOK_ROUND_END = "round.end"
    _HAS_HOOKS = False

logger = logging.getLogger(__name__)


async def perception_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """
    Perception Layer: Responsible for generating the next action or thought.
    Uses the 'operational' role from ContextBuilder (loaded from agent-brain).
    Dynamic skill injection: If task involves debugging/searching, injects 'context_hygiene' skill.
    """
    logger.info("=== perception_node START ===")
    with _span_node("perception", {"round": state.get("rounds", 0)}):
        result = await _perception_node_impl(state, config)
    # Gap 3: fire HOOK_ROUND_END after every perception round.
    if _HAS_HOOKS and _hook_registry is not None:
        try:
            _hook_registry.call(
                _HOOK_ROUND_END,
                {
                    "round": result.get("rounds", 0),
                    "next_action": result.get("next_action"),
                },
            )
        except Exception:
            pass
    return result


async def _perception_node_impl(
    state: Mapping[str, Any], config: Any
) -> Dict[str, Any]:  # noqa: C901  # type: ignore[reportGeneralTypeIssues]
    # Validate state invariants at node entry (D-02: non-fatal, logs on issues)
    validate_state(state)

    # Resolve orchestrator first (needed for dynamic cancel_event lookup)
    orchestrator, early_result = _resolve_orchestrator_and_cancellation(
        state=state,
        config=config,
    )
    if early_result is not None:
        return early_result

    # Increment turn counter and enforce max_turns limit
    turn_count = (state.get("turn_count") or 0) + 1
    # CP-13: fall back to project-level maxTurns before the hard default of 50
    _project_max_turns: int | None = None
    try:
        if _gas is not None:
            _ps = _gas()
            if _ps is not None and _ps.max_turns is not None:
                _project_max_turns = _ps.max_turns
    except Exception:
        pass
    max_turns = int(state.get("max_turns") or _project_max_turns or 50)
    turn_limit_result = _maybe_handle_turn_limit(
        state=state,
        orchestrator=orchestrator,
        turn_count=turn_count,
        max_turns=max_turns,
    )
    if turn_limit_result is not None:
        return turn_limit_result

    adapter, validation_error = _validate_call_model_and_adapter(
        state=state,
        orchestrator=orchestrator,
        call_model_fn=call_model,
    )
    if validation_error is not None:
        return {**validation_error, "turn_count": turn_count}

    # Pre-retrieval: consult repo intelligence tools if available (search_code, find_symbol, find_references)
    # F9: Skip pre-retrieval on rounds > 0 — context was already gathered in round 0.
    # PB-3 fix: run all retrieval tasks concurrently with asyncio.gather so the total
    # latency is max(individual latencies) rather than sum(individual latencies).
    # Use module-level _retrieve_context helper (extracted above) instead of the
    # nested duplicate.  This reduces cognitive complexity and allows unit
    # testing of retrieval behavior.

    retrieved_snippets = await _retrieve_context(state, orchestrator)

    # Setup prompt
    builder = ContextBuilder(working_dir=state.get("working_dir"))

    # ORCH-W1: When within 2 turns of the limit, remove write tools so the model
    # stops attempting new edits and focuses on summarisation/verification only.
    # PN-4: Use the already-incremented `turn_count` local (computed at top of function)
    # rather than re-reading the stale pre-increment value from state.
    # Normalize to plain ints so static analyzers don't infer Optional[int]
    _turn_count_now = int(turn_count)
    _max_turns_now = int(max_turns)

    # Dynamic skill injection: if task involves debugging or deep searching, inject by name
    active_skills = _compute_active_skills_for_task(
        task=str(state.get("task", "")),
    )

    # CP-6: Pre-turn deterministic auto-compaction.
    # Run before the prompt is built so the compacted history feeds into
    # build_prompt() and the LLM never sees the over-full context.
    # This is separate from the post-turn overflow-based _should_distill path.
    #
    # CP6-PERSIST: If a prior turn already produced a compacted snapshot,
    # start from that instead of the ever-growing raw history.  This prevents
    # the compactor from re-firing on every turn once the threshold is crossed.
    _history_for_prompt = _bootstrap_history_for_prompt(state)

    # Run the extracted auto-compaction helper to keep _perception_node_impl
    # focused and easily testable.
    _new_compacted_history = None
    try:
        _history_for_prompt, _new_compacted_history = _run_auto_compaction(
            _history_for_prompt, adapter, orchestrator, state
        )
    except Exception:
        # The helper already swallows non-fatal errors, but guard here as well.
        _new_compacted_history = None

    # PRUNE: Zero out old tool-result content beyond the token boundary so
    # that large file-read outputs from earlier turns don't crowd out recent
    # context.  Runs after CP-6 so the compacted history is pruned, not the
    # raw one.  Does not mutate AgentState — local to this prompt build.
    try:
        _pruned_result = _prune_tool_outputs(
            _history_for_prompt, return_pruned_count=True
        )
        _history_for_prompt, _pruned = _pruned_result  # type: ignore[assignment]
        if _pruned:
            logger.info(
                "perception_node PRUNE: zeroed %d old tool result(s) beyond "
                "%d-token boundary",
                _pruned,
                _PRUNE_PROTECT_TOKENS,
            )
    except Exception as _prune_err:
        logger.debug("perception_node PRUNE: skipped (non-fatal): %s", _prune_err)


    # ORCH-W4: Select role; build tool list filtered to the role's YAML toolset.
    _perception_role = _select_perception_role(state, orchestrator)
    try:
        tools_list = orchestrator.get_tools_for_role(_perception_role)
    except Exception as _tl_err:
        logger.debug("perception_node: get_tools_for_role failed (%s); using full registry", _tl_err)
        tools_list = [{"name": n, "description": m.get("description", "")} for n, m in orchestrator.tool_registry.tools.items()]

    tools_list = _filter_tools_near_turn_limit(
        tools_list=tools_list,
        turn_count=_turn_count_now,
        max_turns=_max_turns_now,
    )

    # Assemble the tiered context / provider metadata used by prompt assembly and warnings.
    _provider_context = _resolve_perception_provider_context(
        orchestrator=orchestrator,
        adapter=adapter,
    )
    _active_model_name = _provider_context["active_model_name"]

    messages = _build_perception_messages(
        builder=builder,
        state=state,
        orchestrator=orchestrator,
        adapter=adapter,
        retrieved_snippets=retrieved_snippets,
        active_skills=active_skills,
        tools_list=tools_list,
        history_for_prompt=_history_for_prompt,
        perception_role=_perception_role,
        active_model_name=_active_model_name,
    )

    provider = _provider_context["provider"]
    model = _provider_context["model"]
    _model_tier_str = _provider_context["model_tier_str"]

    _rounds_now = state.get("rounds") or 0
    _maybe_warn_small_context_window(
        state=state,
        orchestrator=orchestrator,
        adapter=adapter,
        model=model,
        model_tier_str=_model_tier_str,
    )

    # GAP-SMALL-4: Clarification guard for NANO/SMALL models on round 0.
    _clarify_result = _check_small_model_clarification(
        state=state,
        rounds=_rounds_now,
        model_tier_str=_model_tier_str,
        turn_count=turn_count,
    )
    if _clarify_result is not None:
        return _clarify_result

    llm_kwargs = _build_llm_kwargs(orchestrator)

    tools_schema = None
    try:
        registry = getattr(orchestrator, "tool_registry", None) if orchestrator else None
        allowed_tool_names = [tool.get("name") for tool in tools_list if tool.get("name")]
        if registry and hasattr(registry, "filter_by_names") and allowed_tool_names:
            registry = registry.filter_by_names(allowed_tool_names)
        if registry and hasattr(registry, "get_openai_functions"):
            tools_schema = registry.get_openai_functions() or None
        # Safety cap for small/local models: if the role toolset still produces
        # more than 20 schemas (e.g. toolset YAML is very broad), fall back to a
        # model-appropriate subset via the toolset loader's model-aware path.
        # This replaces the previous hardcoded 9-tool _CORE_TOOL_NAMES cap which
        # was both too aggressive and hid YAML-level toolset misconfiguration.
        _SMALL_MODEL_TOOL_LIMIT = 20
        if tools_schema and len(tools_schema) > _SMALL_MODEL_TOOL_LIMIT:
            try:
                from src.config.toolsets.loader import (
                    load_toolset_for_model,
                    _is_small_model,
                )
                if _is_small_model(model):
                    _sm_ts = load_toolset_for_model(_perception_role, model)
                    if _sm_ts and "tools" in _sm_ts:
                        _sm_names = set(_sm_ts["tools"])
                        _reduced = [
                            t for t in tools_schema
                            if t.get("function", {}).get("name") in _sm_names
                        ]
                        if len(_reduced) >= 3:
                            tools_schema = _reduced
            except Exception:
                pass
    except Exception as _ts_exc:
        # B1: Log at WARNING so tool-stripping failures are visible in traces.
        # Falling back to tools_schema=None means the LLM is called with no
        # tools, which effectively stalls the agent — this should never be silent.
        logger.warning(
            "perception_node: failed to build tools_schema; LLM will have no tools. "
            "role=%s model=%s error=%s",
            _perception_role,
            model,
            _ts_exc,
            exc_info=True,
        )
        tools_schema = None

    # LLM Inference
    logger.info(
        f"perception_node: calling call_model with provider={provider}, model={model}"
    )

    # Dynamically resolve cancel_event from orchestrator if not in state
    cancel_event = state.get("cancel_event")
    if not cancel_event and orchestrator:
        cancel_event = getattr(orchestrator, "cancel_event", None)

    # Use shared helper to call model with timeout/cancel handling. Pass the
    # local `call_model` so tests that patch perception_node.call_model continue
    # to work.
    early_resp, resp = await call_model_with_timeout(
        messages,
        provider,
        model,
        state,
        orchestrator,
        llm_kwargs,
        tools=tools_schema,
        call_model_fn=call_model,
    )
    if early_resp is not None:
        return early_resp

    # DEBUG: log raw response for diagnosing empty/empty-tool_calls responses
    logger.info(
        "perception_node: raw resp keys=%s finish_reason=%s tool_calls_len=%s content_len=%s",
        list(resp.keys()) if isinstance(resp, dict) else type(resp),
        resp.get("finish_reason") if isinstance(resp, dict) else "n/a",
        len(resp.get("tool_calls") or []) if isinstance(resp, dict) else "n/a",
        len(resp.get("content", "") or "") if isinstance(resp, dict) else "n/a",
    )

    # Phase 4: Track token usage for budget management
    # REACT-OVF-EARLY-EXIT: Skip the corrective-prompt retry loop entirely.
    # If the adapter signalled context_overflow in the response body, we must
    # truncate and route to memory_sync immediately rather than attempting a
    # corrective prompt which would simply be rejected by the provider.
    if isinstance(resp, dict) and resp.get("context_overflow"):
        logger.warning(
            "perception_node: context overflow error from provider — "
            "triggering reactive compaction"
        )
        try:
            if orchestrator and hasattr(orchestrator, "event_bus"):
                orchestrator.event_bus.publish_typed(ContextOverflow(prompt_tokens=0, budget=0, reserved=0, session_id=state.get("session_id", ""), source="api_error"))
        except Exception:
            pass

        # REACT-OVF-EARLY-EXIT
        _OVERFLOW_HISTORY_KEEP = 6
        _raw_history = list(state.get("history") or [])
        _truncated = (
            _raw_history[-_OVERFLOW_HISTORY_KEEP:]
            if len(_raw_history) > _OVERFLOW_HISTORY_KEEP
            else _raw_history
        )
        logger.warning(
            "perception_node: context overflow early-exit — "
            f"truncating history {len(_raw_history)} → {len(_truncated)} messages; "
            "errors=['context_overflow'] will route to memory_sync"
        )
        return {
            "history": [],  # nothing new to append (operator.add)
            "_compacted_history": _truncated,  # replace-semantics: sets compacted base
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "errors": ["context_overflow"],
            "_budget_compaction": True,
            "_should_distill": True,
            "empty_response_count": 0,
            "last_result": {
                "ok": False,
                "error": "Context window overflow — history truncated, compaction triggered",
            },
        }

    # Delegate detailed token handling to helper (cost accounting + post-call overflow)
    early_result, _overflow_compaction, _session_cost_delta = _process_post_call_tokens(
        resp, state, orchestrator, adapter
    )
    if early_result is not None:
        return early_result

    # Debug: log raw response for troubleshooting (guarded so the potentially
    # expensive repr()/content extraction is skipped when INFO logging is off —
    # audit 2.5).
    if logger.isEnabledFor(logging.INFO):
        try:
            _choices = resp.get("choices")
            if _choices:
                _msg = (
                    _choices[0].get("message", {}) if isinstance(_choices[0], dict) else {}
                )
                _content = _msg.get("content", "") if isinstance(_msg, dict) else ""
            else:
                _content = ""
            logger.info(f"perception_node: raw LLM resp content: {repr(_content)[:100]}")
            logger.info(f"perception_node: raw LLM resp: {repr(resp)[:1000]}")
        except Exception:
            pass

    # Extract response
    ch = None
    if isinstance(resp, dict):
        _choices = resp.get("choices")
        if _choices and len(_choices) > 0:
            ch = _choices[0].get("message") if isinstance(_choices[0], dict) else None
        elif resp.get("message"):
            ch = resp.get("message")

    content = ""
    if isinstance(ch, str):
        content = ch
    elif isinstance(ch, dict):
        content = ch.get("content") or ""

    # UI Sync: Forward raw content immediately to TUI so user can see thinking
    if content and orchestrator and hasattr(orchestrator, "msg_mgr"):
        try:
            orchestrator.msg_mgr.append("assistant", content)
        except Exception as e:
            logger.debug(f"UI sync failed: {e}")

    try:
        logger.info(f"perception_node: extracted content: {repr(content)[:1000]}")
    except Exception:
        pass

    # Parse the content and compute flags with the extracted helper
    (
        tool_call,
        content_stripped,
        thinking_only,
        _content_no_thinking,
    ) = _parse_tool_call_and_flags(resp, content, state)

    # Only run the corrective/no-tool helper when no tool_call was extracted
    # (matches the original inline behaviour).
    if tool_call is None:
        try:
            _is_truncated_yaml = bool(
                content_stripped and "```yaml" in content_stripped.lower()
            )
            _no_tool_result = _handle_no_tool_or_empty_response(
                content,
                content_stripped,
                thinking_only,
                state,
                orchestrator,
                _model_tier_str,
                _is_truncated_yaml=_is_truncated_yaml,
            )
            if _no_tool_result is not None:
                return _no_tool_result
        except Exception:
            # Non-fatal: continue normal flow when helper fails
            pass

    # Handle content without tool calls - if we have meaningful content and retried, return it
    _content_retry_result = _maybe_return_content_after_no_tool_retry(
        _content_no_thinking,
        state,
        _rounds_now,
        turn_count,
        _model_tier_str,
    )
    if _content_retry_result is not None:
        return _content_retry_result

    return await _build_perception_result(
        state=state,
        orchestrator=orchestrator,
        content=content,
        tool_call=tool_call,
        turn_count=turn_count,
        overflow_compaction=_overflow_compaction,
        model_tier_str=_model_tier_str,
        session_cost_delta=_session_cost_delta,
        new_compacted_history=_new_compacted_history,
    )
