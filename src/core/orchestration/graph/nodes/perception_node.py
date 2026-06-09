from src.core.messaging.event_types import ContextOverflow
from langchain_core.runnables import RunnableConfig
import logging
import re
from typing import Mapping, Dict, Any, Optional

import yaml

from src.core.orchestration.graph.state import StateLike, validate_state
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.inference.llm_helpers import call_model_with_timeout
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
)
from src.core.orchestration.graph.nodes.perception_parsing import (
    _parse_tool_call_and_flags,
)
from src.core.orchestration.graph.nodes.perception_no_tool import (
    _handle_no_tool_or_empty_response as _handle_no_tool_or_empty_response_impl,
    _maybe_return_content_after_no_tool_retry as _maybe_return_content_after_no_tool_retry_impl,
)
from src.core.orchestration.graph.nodes.perception_retrieval import (
    _retrieve_context as _retrieve_context_impl,
)
from src.core.orchestration.graph.nodes.perception_result import (
    _build_perception_result as _build_perception_result_impl,
)
from src.core.orchestration.graph.nodes.perception_runtime import (
    _build_llm_kwargs as _build_llm_kwargs_impl,
    _compute_active_skills_for_task as _compute_active_skills_for_task_impl,
    _filter_tools_near_turn_limit as _filter_tools_near_turn_limit_impl,
    _maybe_handle_turn_limit as _maybe_handle_turn_limit_impl,
    _maybe_warn_small_context_window as _maybe_warn_small_context_window_impl,
    _resolve_orchestrator_and_cancellation as _resolve_orchestrator_and_cancellation_impl,
    _resolve_perception_provider_context as _resolve_perception_provider_context_impl,
    _resolve_active_model_name as _resolve_active_model_name_impl,
    _select_perception_role as _select_perception_role_impl,
    _validate_call_model_and_adapter as _validate_call_model_and_adapter_impl,
)
from src.core.orchestration.graph.nodes.perception_messages import (
    _build_perception_messages as _build_perception_messages_impl,
)
from src.core.orchestration.graph.nodes.perception_compaction import (
    _bootstrap_history_for_prompt as _bootstrap_history_for_prompt_impl,
    _run_auto_compaction as _run_auto_compaction_impl,
)
from src.core.orchestration.graph.nodes.perception_post_call import (
    _process_post_call_tokens as _process_post_call_tokens_impl,
)
from src.core.inference.provider_utils import (
    resolve_provider_capabilities as _resolve_provider_caps,
)
from src.core.utils.strings import extract_str as _extract_str
from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node


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

_ACTION_VERBS_SMALL_MODEL = {
    "add", "fix", "update", "change", "create", "delete", "remove",
    "refactor", "write", "read", "run", "test", "debug", "find", "search",
    "show", "list", "explain", "implement", "build", "install", "deploy",
    "check", "verify", "move", "rename", "summarize", "summary",
    "describe", "review", "analyse", "analyze", "generate", "print",
    "display", "get", "fetch", "make", "set",
}


def _check_small_model_clarification(
    state: Mapping[str, Any],
    rounds: int,
    model_tier_str: Optional[str],
    turn_count: int,
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """GAP-SMALL-4: return a clarification prompt for ambiguous small-model tasks.

    When the task is very short (< 8 words) and contains no file references,
    code identifiers, or action verbs, small models are likely to hallucinate
    a plan. Returns a dict with the clarification response, or None to continue.
    """
    if rounds != 0 or model_tier_str != "small":
        return None

    raw_task = (state.get("task") or "").strip()
    task_words = raw_task.split()
    has_action = any(w.lower() in _ACTION_VERBS_SMALL_MODEL for w in task_words)
    has_file_ref = bool(re.search(r"\w+\.\w+|/\w+|\w+\.py\b", raw_task))
    has_code_id = bool(re.search(r"`[^`]+`|\"[A-Za-z_]\w+\"", raw_task))

    if len(task_words) >= 8 or has_action or has_file_ref or has_code_id:
        return None

    logger.info(
        "perception_node: GAP-SMALL-4 ambiguous task detected for small model, "
        "returning clarification prompt (task=%r, words=%d)",
        raw_task[:80],
        len(task_words),
    )
    clarify_msg = (
        "I need a bit more detail to help you effectively. Could you tell me:\n"
        "- What file or component should I work on?\n"
        "- What should change or be created?\n"
        "- What is the expected outcome?"
    )
    return {
        "history": [{"role": "assistant", "content": clarify_msg}],
        "next_action": None,
        "needs_clarification": True,
        "rounds": rounds + 1,
        "turn_count": turn_count,
        "empty_response_count": 0,
        **({"model_tier": model_tier_str} if model_tier_str else {}),
    }


def _classify_model_tier(
    model: str,
    adapter: Any,
    logger: logging.Logger,
) -> Optional[str]:
    """Classify model into a tier string (small, frontier, nano, etc.).

    Returns the tier .value string, or None on failure.
    """
    try:
        if _classify_model is None:
            raise RuntimeError("model_tiers unavailable")
        ctx_window = 0
        if adapter and hasattr(adapter, "context_window"):
            ctx_window = int(adapter.context_window or 0)
        return _classify_model(model, ctx_window).value
    except Exception:
        logger.debug("perception_node: model tier classification failed")
        return None


# Deferred intra-function imports (CODE_QUALITY_AUDIT #7)
# CODE_QUALITY_AUDIT #7 fix: promote deferred intra-function imports to module
# level.  perception_node() is called on every agent round; re-importing 11
# symbols on each invocation was unnecessary overhead.
# Each block is wrapped in try/except so the node degrades gracefully when an
# optional dependency is absent (e.g. in minimal test environments).
try:
    from src.core.orchestration.project_settings import get_active_settings as _gas
except Exception:
    _gas = None  # type: ignore[assignment]

_SymbolGraph: Any = None
try:
    from src.core.indexing.symbol_graph import SymbolGraph as _SymbolGraph  # type: ignore[assignment]
except Exception:
    pass

try:
    from src.core.orchestration.loop_guards import MODIFYING_TOOLS as _MODIFYING_TOOLS
except Exception:
    _MODIFYING_TOOLS = set()  # type: ignore[assignment]

_AutoCompactConfig: Any = None
try:
    from src.core.memory.auto_compactor import (
        AutoCompactConfig as _AutoCompactConfig,  # type: ignore[assignment]
        should_compact as _should_compact,
        compact_messages as _compact_messages,
    )
except Exception:
    _should_compact = None  # type: ignore[assignment]
    _compact_messages = None  # type: ignore[assignment]

try:
    from src.core.config_loader import get as _cfg_get
except Exception:
    _cfg_get = None  # type: ignore[assignment]

try:
    from src.core.inference.model_tiers import classify_model as _classify_model
except Exception:
    _classify_model = None  # type: ignore[assignment]

try:
    from src.core.inference.provider_context import (
        get_context_budget as _get_context_budget,
        estimate_cost_usd as _estimate_cost_usd,
    )
except Exception:
    _get_context_budget = None  # type: ignore[assignment]
    _estimate_cost_usd = None  # type: ignore[assignment]

try:
    from src.core.orchestration.graph.builder import _task_is_complex as _tic
except Exception:
    _tic = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# PRUNE: Placeholder inserted in place of old tool result content.
_PRUNED_TOOL_PLACEHOLDER = "[Old tool result content cleared to save context]"
# PRUNE: Token threshold (from the end of history) beyond which tool outputs
# are zeroed.  OP-3: Raised from 15K → 40K to match OpenCode's PRUNE_PROTECT.
# The 15K value was calibrated for the old 7600-token LM Studio default context;
# that override was removed in P1-A.  Modern models (Gemma 4: 128K-256K ctx,
# LM Studio defaults: 32K-128K) can safely protect 40K tokens of recent history.
# Raising this reduces spurious "content cleared" entries in mid-session context.
_PRUNE_PROTECT_TOKENS = 40_000
# PRUNE: Always keep this many recent messages intact regardless of size.
_PRUNE_PROTECT_RECENT = 6


def _prune_tool_outputs(history: list) -> tuple:
    """Zero out old tool-result content beyond the _PRUNE_PROTECT_TOKENS boundary.

    Walks the history from newest to oldest, accumulating a token count.
    Once the running total exceeds _PRUNE_PROTECT_TOKENS, any subsequent
    messages whose content looks like a tool_execution_result are replaced with
    the _PRUNED_TOOL_PLACEHOLDER.  The last _PRUNE_PROTECT_RECENT messages are
    always preserved verbatim.

    OP-10: Messages with ``metadata.preserve = True`` are never pruned.
    P2-D: Uses tiktoken for accurate token counting when available; falls back
          to ``len // 4`` rather than the aggressive ``len // 4`` estimate.

    Returns (pruned_history, pruned_count) — pruned_history is a new list
    (input is not mutated), pruned_count is the number of messages zeroed.
    """
    if not history:
        return history, 0

    # P2-D: prefer tiktoken for accurate counting; fall back to char heuristic.
    try:
        from src.core.inference.tokenizer import count_tokens as _count_tokens

        def _est(msg: dict) -> int:
            c = msg.get("content") or ""
            s = c if isinstance(c, str) else str(c)
            try:
                return max(1, _count_tokens(s))
            except Exception:
                return max(1, len(s) // 4)

    except Exception:

        def _est(msg: dict) -> int:  # type: ignore[misc]
            c = msg.get("content") or ""
            s = c if isinstance(c, str) else str(c)
            return max(1, len(s) // 4)

    def _is_tool_result(msg: dict) -> bool:
        if msg.get("role") == "tool":
            return True
        if msg.get("role") == "user":
            c = msg.get("content") or ""
            return "tool_execution_result" in (c if isinstance(c, str) else str(c))
        return False

    result = list(history)
    total = len(result)
    running_tokens = 0
    pruned_count = 0

    for i in range(total - 1, -1, -1):
        msg = result[i]
        # Always protect the most recent N messages
        if (total - 1 - i) < _PRUNE_PROTECT_RECENT:
            running_tokens += _est(msg)
            continue
        # OP-10: Never prune messages tagged with preserve=True metadata.
        if msg.get("metadata", {}).get("preserve"):
            running_tokens += _est(msg)
            continue
        running_tokens += _est(msg)
        if running_tokens > _PRUNE_PROTECT_TOKENS and _is_tool_result(msg):
            if msg.get("content") != _PRUNED_TOOL_PLACEHOLDER:
                result[i] = {**msg, "content": _PRUNED_TOOL_PLACEHOLDER}
                pruned_count += 1

    return result, pruned_count


# P1-D: Graduated corrective prompts helper.
# Selects a corrective prompt variant based on the number of consecutive empty/no-tool
# responses (attempt) and model tier.
def _select_corrective_prompt(
    attempt: int = 1,
    model_tier: str | None = None,
    truncated_yaml: bool = False,
) -> str:
    try:
        att = int(attempt or 1)
    except Exception:
        att = 1
    # Graduated prompts: gentle -> specific -> critical
    prompts = [
        (
            "\n\n<system_reminder>\n"
            "Please provide a valid YAML tool call for your next action.\n"
            "Use this format:\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  arg: value\n"
            "```\n"
            "Avoid empty responses or thinking-only blocks.\n"
            "If you cannot determine the next action, you may use the 'respond' tool.\n"
            "</system_reminder>\n"
        ),
        (
            "\n\n<system_reminder>\n"
            "Please output a valid YAML tool call block now. No analysis or preamble.\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  key: value\n"
            "```\n"
            "</system_reminder>\n"
        ),
        (
            "\n\n<system_reminder>\n"
            "Important: Please provide a valid YAML tool call block.\n"
            "Format:\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  key: value\n"
            "```\n"
            "Avoid thinking-only responses or empty outputs.\n"
            "</system_reminder>\n"
        ),
    ]
    idx = max(0, min(att - 1, len(prompts) - 1))
    tier = (model_tier or "").lower()
    if truncated_yaml:
        return (
            "\n\n<system_reminder>\n"
            "Your previous YAML tool block may have been cut off or malformed. "
            "Please resend a complete YAML tool call.\n"
            "```yaml\n"
            "name: tool_name\n"
            "arguments:\n"
            "  key: value\n"
            "```\n"
            "</system_reminder>\n"
        )
    if tier == "small" and att >= 2:
        return prompts[1]
    return prompts[idx]


def _parse_yaml_tool_call_from_content(content: str) -> dict | None:
    """Backward-compatible YAML tool-call parser used by legacy tests."""
    try:
        stripped = (content or "").strip()
        if "```yaml" in stripped:
            start = stripped.find("```yaml") + len("```yaml")
            end = stripped.find("```", start)
            yaml_block = stripped[start:end].strip() if end != -1 else stripped[start:].strip()
        else:
            yaml_block = stripped

        if not yaml_block:
            return None

        data = yaml.safe_load(yaml_block)
        if not data:
            return None
        if isinstance(data, dict) and "name" in data:
            return {
                "name": data.get("name"),
                "arguments": data.get("arguments") or {},
            }
        if isinstance(data, dict) and len(data) == 1:
            name, arguments = next(iter(data.items()))
            return {
                "name": name,
                "arguments": arguments or {},
            }
    except Exception:
        pass

    try:
        return parse_tool_block(content)
    except Exception:
        return None


# Delegate LLM waiting/call helpers to shared implementation in llm_helpers.
# The original implementations below are preserved for fallback if llm_helpers
# is unavailable. This avoids code divergence while maintaining test compatibility.
_llm_helpers: Any = None
try:
    from src.core.inference import llm_helpers as _llm_helpers  # type: ignore[assignment]
except Exception:
    pass


if _llm_helpers is not None:
    _await_llm_task = _llm_helpers._await_llm_task  # type: ignore[assignment]
else:
    async def _await_llm_task(task, timeout=None, cancel_event=None):  # type: ignore[misc]
        """No-op fallback when llm_helpers is unavailable."""
        return await task


def _handle_no_tool_or_empty_response(
    content: str,
    content_stripped: str,
    thinking_only: bool,
    state: Mapping[str, Any],
    orchestrator: Any,
    _model_tier_str: str | None,
    *,
    _is_truncated_yaml: bool = False,
) -> dict | None:
    """Compatibility wrapper around the extracted no-tool helper."""
    return _handle_no_tool_or_empty_response_impl(
        content=content,
        content_stripped=content_stripped,
        thinking_only=thinking_only,
        state=state,
        orchestrator=orchestrator,
        _model_tier_str=_model_tier_str,
        _is_truncated_yaml=_is_truncated_yaml,
        select_corrective_prompt=_select_corrective_prompt,
    )


def _maybe_return_content_after_no_tool_retry(
    content_no_thinking: str,
    state: Mapping[str, Any],
    rounds_now: int,
    turn_count: int,
    model_tier_str: str | None,
) -> dict | None:
    """Compatibility wrapper around the extracted no-tool content fallback."""
    return _maybe_return_content_after_no_tool_retry_impl(
        content_no_thinking,
        state,
        rounds_now,
        turn_count,
        model_tier_str,
    )


async def _retrieve_context(state: Mapping[str, Any], orchestrator: Any) -> list:
    """Compatibility wrapper around the extracted retrieval helper."""
    return await _retrieve_context_impl(
        state,
        orchestrator,
        symbol_graph_cls=_SymbolGraph,
    )


def _process_post_call_tokens(
    resp: Any, state: Mapping[str, Any], orchestrator: Any, adapter: Any
) -> tuple[dict | None, dict, float]:
    """Compatibility wrapper around the extracted post-call token helper."""
    return _process_post_call_tokens_impl(
        resp,
        state,
        orchestrator,
        adapter,
        estimate_cost_usd=_estimate_cost_usd,
    )


def _run_auto_compaction(
    history_for_prompt: list, adapter: Any, orchestrator: Any, state: Mapping[str, Any]
) -> tuple[list, list | None]:
    """Compatibility wrapper around the extracted auto-compaction helper."""
    return _run_auto_compaction_impl(
        history_for_prompt,
        adapter,
        orchestrator,
        state,
        auto_compact_config_cls=_AutoCompactConfig,
        should_compact_fn=_should_compact,
        compact_messages_fn=_compact_messages,
        cfg_get_fn=_cfg_get,
        get_context_budget_fn=_get_context_budget,
    )


def _build_perception_messages(
    builder: Any,
    state: Mapping[str, Any],
    orchestrator: Any,
    adapter: Any,
    retrieved_snippets: list,
    active_skills: list,
    tools_list: list,
    history_for_prompt: list,
    perception_role: str,
    active_model_name: str | None,
) -> list:
    """Compatibility wrapper around the extracted perception message builder."""
    return _build_perception_messages_impl(
        builder,
        state,
        orchestrator,
        adapter,
        retrieved_snippets,
        active_skills,
        tools_list,
        history_for_prompt,
        perception_role,
        active_model_name,
        get_context_budget=_get_context_budget,
        get_agent_settings=_gas,
    )


def _resolve_active_model_name(
    provider_capabilities: Mapping[str, Any] | None,
    orchestrator: Any,
) -> str:
    """Compatibility wrapper around active-model resolution."""
    return _resolve_active_model_name_impl(
        provider_capabilities,
        orchestrator,
        extract_str=_extract_str,
    )


def _build_llm_kwargs(orchestrator: Any) -> dict:
    """Compatibility wrapper around LLM runtime kwarg preparation."""
    return _build_llm_kwargs_impl(orchestrator, logger)


def _maybe_warn_small_context_window(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    adapter: Any,
    model: str | None,
    model_tier_str: str | None,
) -> None:
    """Compatibility wrapper around the GAP-10 context window warning helper."""
    _maybe_warn_small_context_window_impl(
        state=state,
        orchestrator=orchestrator,
        adapter=adapter,
        model=model,
        model_tier_str=model_tier_str,
        logger=logger,
    )


async def _build_perception_result(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    content: str,
    tool_call: dict | None,
    turn_count: int,
    overflow_compaction: dict,
    model_tier_str: str | None,
    session_cost_delta: float,
    new_compacted_history: list | None,
) -> dict:
    """Compatibility wrapper around final perception result assembly."""
    return await _build_perception_result_impl(
        state=state,
        orchestrator=orchestrator,
        content=content,
        tool_call=tool_call,
        turn_count=turn_count,
        overflow_compaction=overflow_compaction,
        model_tier_str=model_tier_str,
        session_cost_delta=session_cost_delta,
        new_compacted_history=new_compacted_history,
        task_is_complex_fn=_tic,
        logger=logger,
    )


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
    orchestrator, early_result = _resolve_orchestrator_and_cancellation_impl(
        state=state,
        config=config,
        resolve_orchestrator_fn=_resolve_orchestrator,
        logger=logger,
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
    turn_limit_result = _maybe_handle_turn_limit_impl(
        state=state,
        orchestrator=orchestrator,
        turn_count=turn_count,
        max_turns=max_turns,
        logger=logger,
    )
    if turn_limit_result is not None:
        return turn_limit_result

    adapter, validation_error = _validate_call_model_and_adapter_impl(
        state=state,
        orchestrator=orchestrator,
        call_model_fn=call_model,
        logger=logger,
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
    active_skills = _compute_active_skills_for_task_impl(
        task=str(state.get("task", "")),
        logger=logger,
    )

    # CP-6: Pre-turn deterministic auto-compaction.
    # Run before the prompt is built so the compacted history feeds into
    # build_prompt() and the LLM never sees the over-full context.
    # This is separate from the post-turn overflow-based _should_distill path.
    #
    # CP6-PERSIST: If a prior turn already produced a compacted snapshot,
    # start from that instead of the ever-growing raw history.  This prevents
    # the compactor from re-firing on every turn once the threshold is crossed.
    _history_for_prompt = _bootstrap_history_for_prompt_impl(state)

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
        _history_for_prompt, _pruned = _prune_tool_outputs(_history_for_prompt)
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
    _perception_role = _select_perception_role_impl(state, orchestrator)
    try:
        tools_list = orchestrator.get_tools_for_role(_perception_role)
    except Exception as _tl_err:
        logger.debug("perception_node: get_tools_for_role failed (%s); using full registry", _tl_err)
        tools_list = [{"name": n, "description": m.get("description", "")} for n, m in orchestrator.tool_registry.tools.items()]

    tools_list = _filter_tools_near_turn_limit_impl(
        tools_list=tools_list,
        turn_count=_turn_count_now,
        max_turns=_max_turns_now,
        modifying_tools=_MODIFYING_TOOLS,
        logger=logger,
    )

    # Assemble the tiered context / provider metadata used by prompt assembly and warnings.
    _provider_context = _resolve_perception_provider_context_impl(
        orchestrator=orchestrator,
        adapter=adapter,
        resolve_provider_caps_fn=_resolve_provider_caps,
        resolve_active_model_name_fn=_resolve_active_model_name,
        classify_model_tier_fn=_classify_model_tier,
        logger=logger,
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
        logger=logger,
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
                orchestrator.event_bus.publish_typed(ContextOverflow(prompt_tokens=0, budget=0, reserved=0, session_id=state.get("session_id"), source="api_error"))
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

    # Debug: log raw response for troubleshooting
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
    except Exception:
        pass

    # Debug: log raw response for troubleshooting
    try:
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
