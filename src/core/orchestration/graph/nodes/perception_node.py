from langchain_core.runnables import RunnableConfig
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Mapping, Dict, Any

from src.core.orchestration.graph.state import StateLike, validate_state
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.inference.llm_helpers import call_model_with_timeout
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
)
from src.core.orchestration.event_bus import run_with_correlation
from src.core.utils.strings import valid_str as _valid_str, extract_str as _extract_str
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


# CODE_QUALITY_AUDIT #7 fix: promote deferred intra-function imports to module
# level.  perception_node() is called on every agent round; re-importing 11
# symbols on each invocation was unnecessary overhead.
# Each block is wrapped in try/except so the node degrades gracefully when an
# optional dependency is absent (e.g. in minimal test environments).
try:
    from src.core.orchestration.project_settings import get_active_settings as _gas
except Exception:
    _gas = None  # type: ignore[assignment]

try:
    from src.core.indexing.symbol_graph import SymbolGraph as _SymbolGraph
except Exception:
    _SymbolGraph = None  # type: ignore[assignment]

try:
    from src.core.orchestration.loop_guards import MODIFYING_TOOLS as _MODIFYING_TOOLS
except Exception:
    _MODIFYING_TOOLS = set()  # type: ignore[assignment]

try:
    from src.core.memory.auto_compactor import (
        AutoCompactConfig as _AutoCompactConfig,
        should_compact as _should_compact,
        compact_messages as _compact_messages,
    )
except Exception:
    _AutoCompactConfig = None  # type: ignore[assignment]
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
# responses (attempt) and model tier. The truncated_yaml path is a special-case that
# instructs the model to emit a minimal tool call.
def _select_corrective_prompt(
    attempt: int = 1, model_tier: str | None = None, truncated_yaml: bool = False
) -> str:
    try:
        att = int(attempt or 1)
    except Exception:
        att = 1
    # Truncated YAML gets a concise, explicit prompt.
    if truncated_yaml:
        return (
            "\n\n<system_reminder>\n"
            "Your response may have been cut off because the context window is full. "
            "Please output a minimal YAML tool call — include the tool name and at most 1-2 arguments. "
            "Keep the response brief (under 50 tokens). No analysis or preamble.\n"
            "```yaml\nname: tool_name\narguments:\n  key: value\n```\n"
            "</system_reminder>\n"
        )
    # Graduated prompts: gentle -> specific -> critical
    prompts = [
        # 1st attempt: gentle reminder and fallback to 'respond' tool
        (
            "\n\n<system_reminder>\n"
            "Please provide a valid YAML tool call for your next action. Avoid empty responses or thinking-only blocks.\n"
            "If you cannot determine the next action, you may use the 'respond' tool to explain what you need.\n"
            "</system_reminder>\n"
        ),
        # 2nd attempt: prescriptive example and formatting guidance
        (
            "\n\n<system_reminder>\n"
            "Please output a valid YAML tool call block now. Use the YAML format below and keep it concise (name and at most 1-2 arguments). No analysis or preamble.\n"
            "```yaml\nname: tool_name\narguments:\n  key: value\n```\n"
            "</system_reminder>\n"
        ),
        # 3rd+ attempt: firmer guidance while remaining polite
        (
            "\n\n<system_reminder>\n"
            "Important: Please provide a valid YAML tool call block. "
            "Format:\n```yaml\nname: tool_name\narguments:\n  key: value\n```\n"
            "Avoid thinking-only responses or empty outputs.\n"
            "</system_reminder>\n"
        ),
    ]
    idx = max(0, min(att - 1, len(prompts) - 1))
    # Model-tier adjustments: for SMALL models use more concise variant
    # for attempts >=2 to reduce token usage.
    tier = (model_tier or "").lower()
    if tier == "small" and att >= 2:
        # Return the prescriptive example (index 1) which is compact.
        return prompts[1]
    return prompts[idx]


# Delegate LLM waiting/call helpers to shared implementation in llm_helpers.
# The original implementations below are preserved for fallback if llm_helpers
# is unavailable. This avoids code divergence while maintaining test compatibility.
try:
    from src.core.inference import llm_helpers as _llm_helpers
except Exception:
    _llm_helpers = None


if _llm_helpers is not None:
    _await_llm_task = _llm_helpers._await_llm_task  # type: ignore[assignment]
else:
    async def _await_llm_task(task, timeout=None, cancel_event=None):  # type: ignore[misc]
        """No-op fallback when llm_helpers is unavailable."""
        return await task


def _extract_message_obj(resp: Any) -> dict:
    """Return the normalized message object from an adapter response.

    Safe wrapper: returns empty dict on unexpected shapes.
    """
    try:
        if isinstance(resp, dict):
            _choices = resp.get("choices")
            if _choices and len(_choices) > 0:
                return (
                    _choices[0].get("message", {})
                    if isinstance(_choices[0], dict)
                    else {}
                )
    except Exception:
        pass
    return {}


def _parse_native_tool_call_from_resp(resp: Any) -> dict | None:
    """Parse native tool_calls from provider response (Frontier models).

    Returns a dict like {"name": str, "arguments": dict} or None.
    """
    try:
        message_obj = _extract_message_obj(resp)
        native_tool_calls = message_obj.get("tool_calls")
        if (
            native_tool_calls
            and isinstance(native_tool_calls, list)
            and len(native_tool_calls) > 0
        ):
            tc = native_tool_calls[0]
            if isinstance(tc, dict):
                func = tc.get("function")
                if func:
                    name = func.get("name")
                    args = func.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if name:
                        logger.info(f"perception_node: native function call: {name}")
                        return {"name": name, "arguments": args or {}}
    except Exception:
        pass
    return None


def _parse_yaml_tool_call_from_content(content: str) -> dict | None:
    """Attempt to parse a YAML tool block from content.

    Tries thinking-stripped content first, then falls back to raw content.
    """
    if not content:
        return None
    try:
        try:
            from src.core.inference.thinking_utils import strip_thinking as _st

            _stripped_for_parse = _st(content)
            if _stripped_for_parse:
                parsed = parse_tool_block(_stripped_for_parse)
                if parsed:
                    return parsed
        except Exception:
            pass
        return parse_tool_block(content)
    except Exception:
        return None


def _detect_prompt_injection(tool_call: dict | None, state: Mapping[str, Any]) -> bool:
    """Detect if a parsed tool_call mirrors a prior user message (prompt injection).

    Returns True when an injection is detected and the tool call should be rejected.
    """
    if not tool_call:
        return False
    tool_name_extracted = tool_call.get("name", "")
    if not tool_name_extracted:
        return False
    user_messages = [
        m.get("content", "")
        for m in (state.get("history") or [])
        if m.get("role") == "user"
    ]
    _name_pattern = f"name: {tool_name_extracted}"
    _tool_args = tool_call.get("arguments") or {}
    _arg_keys = list(_tool_args.keys())[:3]
    for um in user_messages:
        if not um or _name_pattern not in um:
            continue
        if _arg_keys:
            if any(f"{k}:" in um for k in _arg_keys):
                return True
        else:
            if "arguments:" in um:
                return True
    return False


def _handle_no_tool_or_empty_response(
    content: str,
    content_stripped: str,
    thinking_only: bool,
    _is_truncated_yaml: bool,
    state: Mapping[str, Any],
    orchestrator: Any,
    _model_tier_str: str | None,
) -> dict | None:
    """Encapsulate the corrective-prompt retry logic when no tool was parsed.

    Returns an early-exit result dict when corrective attempts are exhausted or
    when a corrective prompt should be issued. Returns None to indicate
    perception_node should continue normal execution.
    """
    if not (content_stripped or thinking_only or _is_truncated_yaml):
        return None

    empty_response_count = int(state.get("empty_response_count") or 0) + 1
    logger.info(
        f"perception_node: No tool call extracted (count: {empty_response_count})"
    )

    tier = (_model_tier_str or "").lower()
    if tier == "small":
        _max_corrective_2 = 2
    elif tier == "medium":
        _max_corrective_2 = 3
    else:
        _max_corrective_2 = 4

    if empty_response_count >= _max_corrective_2:
        logger.error(
            f"perception_node: {_max_corrective_2} consecutive failed tool extractions (tier={tier}) - breaking loop"
        )
        return {
            "history": [{"role": "assistant", "content": content or ""}],
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "last_result": {
                "ok": False,
                "error": f"Infinite loop detected: model failed to generate valid tool calls {_max_corrective_2} times",
            },
            "errors": ["infinite_loop_no_tool"],
            "empty_response_count": 0,
        }

    corrective_prompt = _select_corrective_prompt(
        attempt=empty_response_count,
        model_tier=_model_tier_str,
        truncated_yaml=bool(_is_truncated_yaml),
    )
    new_messages = [
        {"role": "assistant", "content": content or ""},
        {
            "role": "user",
            "content": corrective_prompt + "\n\nProvide a valid YAML tool call now.",
        },
    ]
    try:
        if orchestrator and hasattr(orchestrator, "event_bus"):
            evt = {
                "session_id": state.get("session_id"),
                "attempt": empty_response_count,
                "reason": "truncated_yaml" if _is_truncated_yaml else "no_tool",
                "truncated_yaml": bool(_is_truncated_yaml),
                "model_tier": _model_tier_str,
            }
            try:
                orchestrator.event_bus.publish("perception.corrective_prompt", evt)
            except Exception:
                pub = getattr(orchestrator.event_bus, "publish", None)
                if callable(pub):
                    pub("perception.corrective_prompt", evt)
    except Exception:
        pass
    return {
        "history": new_messages,
        "next_action": None,
        "rounds": state.get("rounds", 0) + 1,
        "empty_response_count": empty_response_count,
    }


async def _retrieve_context(state: Mapping[str, Any], orchestrator: Any) -> list:
    """Module-level extraction of the pre-retrieval logic.

    This was previously nested inside _perception_node_impl. Extracting it to
    top-level reduces cyclomatic complexity and makes the retrieval behavior
    unit-testable.
    """
    retrieved_snippets: list = []
    try:
        if (
            state.get("rounds", 0) == 0
            and orchestrator
            and hasattr(orchestrator, "tool_registry")
        ):
            raw_task = state.get("task") or ""
            # Extract CamelCase identifiers, snake_case names, and quoted tokens
            # from the raw task so retrieval targets symbols rather than prose.
            _sym_re = re.compile(
                r"`([^`]+)`"  # backtick-quoted tokens
                r"|\"([A-Za-z_]\w*)\""  # double-quoted identifiers
                r"|([A-Z][a-z]+(?:[A-Z][a-z]+)+)"  # CamelCase (≥2 words)
                r"|([a-z_][a-z0-9]*(?:_[a-z0-9]+){1,})"  # snake_case (≥2 parts)
            )
            _extracted: list = []
            for m in _sym_re.finditer(raw_task):
                tok = next(g for g in m.groups() if g)
                if tok and tok not in _extracted:
                    _extracted.append(tok)
            query = _extracted[0] if _extracted else raw_task
            symbol_queries = _extracted if _extracted else [raw_task]

            # Build coroutines for each retrieval operation so they run concurrently.
            # Tool fns are synchronous, so wrap each in run_in_executor.
            loop = asyncio.get_running_loop()

            def _safe_call(tool_name, **kwargs):
                try:
                    t = orchestrator.tool_registry.get(tool_name)
                    if t and callable(t.get("fn")):
                        return t["fn"](**kwargs)
                except Exception:
                    pass
                return None

            _workdir = state.get("working_dir")

            async def _fetch_search_code():
                _queries = symbol_queries[:3] if symbol_queries else [query]
                results = await asyncio.gather(
                    *[
                        run_with_correlation(
                            loop,
                            None,
                            lambda _q=_q: _safe_call(
                                "search_code", query=_q, workdir=_workdir
                            ),
                        )
                        for _q in _queries
                    ],
                    return_exceptions=True,
                )
                # Merge all non-error results into a single list-style response
                merged: list = []
                for r in results:
                    if r and not isinstance(r, Exception):
                        if isinstance(r, dict):
                            merged.extend(r.get("results", []))
                        elif isinstance(r, list):
                            merged.extend(r)
                return {"results": merged} if merged else None

            async def _fetch_symbols():
                results = []
                for _sq in symbol_queries[:3]:
                    r = await run_with_correlation(
                        loop,
                        None,
                        lambda sq=_sq: _safe_call(
                            "find_symbol", name=sq, workdir=_workdir
                        ),
                    )
                    results.append(r)
                return results

            async def _fetch_references():
                return await run_with_correlation(
                    loop,
                    None,
                    lambda: _safe_call("find_references", name=query, workdir=_workdir),
                )

            async def _fetch_test_files():
                results = []
                try:
                    if _SymbolGraph is None:
                        return results
                    sg = _SymbolGraph(_workdir)
                    for _sq in symbol_queries[:2]:
                        tests = await run_with_correlation(
                            loop, None, lambda sq=_sq: sg.find_tests_for_module(sq)
                        )
                        if tests and isinstance(tests, list):
                            results.extend(tests[:2])
                except Exception:
                    pass
                return results

            (
                sc_result,
                sym_results,
                fr_result,
                test_file_results,
            ) = await asyncio.gather(
                _fetch_search_code(),
                _fetch_symbols(),
                _fetch_references(),
                _fetch_test_files(),
                return_exceptions=True,
            )

            # Process search_code result
            if sc_result and not isinstance(sc_result, Exception):
                raw_list = (
                    sc_result.get("results") if isinstance(sc_result, dict) else None
                ) or (sc_result if isinstance(sc_result, list) else [])
                for r in raw_list:
                    if isinstance(r, dict):
                        retrieved_snippets.append(
                            {
                                "file_path": r.get("file_path") or r.get("file"),
                                "snippet": r.get("snippet")
                                or r.get("text")
                                or r.get("content"),
                                "reason": "search_code",
                            }
                        )

            # Process find_symbol results
            if (
                sym_results
                and not isinstance(sym_results, Exception)
                and isinstance(sym_results, list)
            ):
                for fs in sym_results:
                    if fs and isinstance(fs, dict) and fs.get("file_path"):
                        retrieved_snippets.append(
                            {
                                "file_path": fs.get("file_path"),
                                "snippet": fs.get("snippet"),
                                "reason": "find_symbol",
                            }
                        )

            # Process find_references result
            if (
                fr_result
                and not isinstance(fr_result, Exception)
                and isinstance(fr_result, list)
            ):
                for r in fr_result:
                    if isinstance(r, dict):
                        retrieved_snippets.append(
                            {
                                "file_path": r.get("file_path"),
                                "snippet": r.get("excerpt") or r.get("context"),
                                "reason": "find_references",
                            }
                        )

            # P3-2: Process test file results
            if (
                test_file_results
                and not isinstance(test_file_results, Exception)
                and isinstance(test_file_results, list)
            ):
                for test_path in test_file_results[:3]:
                    if isinstance(test_path, str) and test_path:
                        retrieved_snippets.append(
                            {
                                "file_path": test_path,
                                "snippet": None,
                                "reason": "find_tests_for_module",
                            }
                        )
    except Exception as _retrieval_exc:
        logger.debug(
            "perception_node: context retrieval failed (non-fatal, continuing with empty snippets): %s",
            _retrieval_exc,
        )
        retrieved_snippets = []
    return retrieved_snippets


def _process_post_call_tokens(
    resp: Any, state: Mapping[str, Any], orchestrator: Any, adapter: Any
) -> tuple[dict | None, dict, float]:
    """Process post-LLM token usage and context-overflow handling.

    Returns a tuple: (early_result_or_None, overflow_compaction_dict, session_cost_delta)

    The helper mirrors the inline logic previously present in _perception_node_impl
    and is intentionally synchronous so it is easy to unit-test.
    """
    _overflow_compaction: dict = {}
    _session_cost_delta: float = 0.0

    # REACT-OVF: Reactive overflow signalled by the adapter in the response
    if isinstance(resp, dict) and resp.get("context_overflow"):
        logger.warning(
            "perception_node: context overflow error from provider — "
            "triggering reactive compaction"
        )
        _overflow_compaction = {"_budget_compaction": True, "_should_distill": True}
        try:
            if orchestrator and hasattr(orchestrator, "event_bus"):
                orchestrator.event_bus.publish(
                    "context.overflow",
                    {
                        "prompt_tokens": 0,
                        "budget": 0,
                        "reserved": 0,
                        "session_id": state.get("session_id"),
                        "source": "api_error",
                    },
                )
        except Exception:
            pass

        # Early-exit path mirrors the original behaviour: truncate history and
        # instruct the caller to persist the compacted snapshot.
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
        return (
            {
                "history": [],
                "_compacted_history": _truncated,
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
            },
            _overflow_compaction,
            _session_cost_delta,
        )

    if isinstance(resp, dict):
        # Read token counts from the normalized response shape
        _resp_prompt_tokens: int = int(resp.get("prompt_tokens") or 0)
        _resp_completion_tokens: int = int(resp.get("completion_tokens") or 0)
        _resp_total_tokens: int = int(
            resp.get("total_tokens") or _resp_prompt_tokens + _resp_completion_tokens
        )
        # Anthropic cache tokens (may be zero)
        _cache_creation_tokens: int = int(resp.get("cache_creation_input_tokens") or 0)
        _cache_read_tokens: int = int(resp.get("cache_read_input_tokens") or 0)

        _has_usage = (_resp_prompt_tokens + _resp_completion_tokens) > 0
        if _has_usage and orchestrator:
            try:
                token_monitor = getattr(orchestrator, "token_monitor", None)
                if token_monitor:
                    token_monitor.record_usage(
                        session_id=state.get("session_id", "default"),
                        prompt_tokens=_resp_prompt_tokens,
                        completion_tokens=_resp_completion_tokens,
                        total_tokens=_resp_total_tokens,
                    )
                    # Accumulate session cost when pricing helper is available
                    try:
                        if _estimate_cost_usd is not None:
                            _active_model = resp.get("model") or (
                                adapter.default_model
                                if adapter and hasattr(adapter, "default_model")
                                else ""
                            )
                            _session_cost_delta = _estimate_cost_usd(
                                _resp_prompt_tokens,
                                _resp_completion_tokens,
                                _active_model or "",
                            )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Token tracking error: {e}")

        # Post-call overflow detection using the provider's actual context window
        try:
            from src.core.inference.provider_context import get_actual_context_window

            _prompt_tokens = _resp_prompt_tokens
            _RESERVED_OUTPUT_BUFFER = 4096
            _budget = get_actual_context_window()
            _available = _budget - _RESERVED_OUTPUT_BUFFER
            if _prompt_tokens > 0 and _prompt_tokens >= _available:
                logger.warning(
                    f"perception_node: context overflow detected — "
                    f"prompt_tokens={_prompt_tokens} >= available={_available} "
                    f"(budget={_budget}, reserved={_RESERVED_OUTPUT_BUFFER}); "
                    "triggering compaction"
                )
                _overflow_compaction = {
                    "_budget_compaction": True,
                    "_should_distill": True,
                }
                try:
                    if orchestrator and hasattr(orchestrator, "event_bus"):
                        orchestrator.event_bus.publish(
                            "context.overflow",
                            {
                                "prompt_tokens": _prompt_tokens,
                                "context_window": _budget,
                                "reserved": _RESERVED_OUTPUT_BUFFER,
                                "session_id": state.get("session_id"),
                            },
                        )
                except Exception:
                    pass
        except Exception as _ov_err:
            logger.debug(f"context overflow check error (non-fatal): {_ov_err}")

    return None, _overflow_compaction, _session_cost_delta


def _run_auto_compaction(
    history_for_prompt: list, adapter: Any, orchestrator: Any, state: Mapping[str, Any]
) -> tuple[list, list | None]:
    """Run the CP-6 deterministic auto-compaction logic.

    Returns (possibly_modified_history_for_prompt, new_compacted_history_or_None).
    The helper is non-fatal: any exceptions are caught and the original history
    is returned unchanged.
    """
    _new_compacted_history = None
    try:
        if (
            _AutoCompactConfig is None
            or _should_compact is None
            or _compact_messages is None
            or _cfg_get is None
        ):
            raise RuntimeError("auto_compactor unavailable")
        # Determine a context window to size the compaction threshold
        _ctx_window: int = 0
        try:
            if adapter and hasattr(adapter, "context_window"):
                _ctx_window = int(adapter.context_window or 0)
            if not _ctx_window and _get_context_budget is not None:
                _ctx_window = _get_context_budget()
        except Exception:
            pass

        _config_default_max = int(_cfg_get("auto_compact_max_tokens", 10_000) or 10_000)
        _ac_max_tokens: int = (
            int(_ctx_window * 0.85) if _ctx_window > 0 else _config_default_max
        )
        _ac_preserve: int = int(_cfg_get("auto_compact_preserve_recent", 4) or 4)
        _ac_config = _AutoCompactConfig(
            preserve_recent=_ac_preserve,
            max_tokens=_ac_max_tokens,
        )

        _compaction_last_round = state.get("_compaction_last_round")
        _current_rounds = int(state.get("rounds") or 0)
        _COMPACTION_MIN_GAP = 3
        _compaction_last_round_int = (
            int(_compaction_last_round) if _compaction_last_round is not None else None
        )
        gap: int | None = None
        if _compaction_last_round_int is not None:
            gap = _current_rounds - _compaction_last_round_int
            _compaction_on_cooldown = gap < _COMPACTION_MIN_GAP
        else:
            _compaction_on_cooldown = False

        if _compaction_on_cooldown:
            # cooldown active; skip compaction
            logger.debug(
                f"perception_node CP-6: skipping compaction — cooldown active (last={_compaction_last_round_int}, current={_current_rounds}, gap={gap} < {_COMPACTION_MIN_GAP})"
            )
        elif _should_compact(history_for_prompt, _ac_config):
            _compact_result = _compact_messages(history_for_prompt, _ac_config)
            if _compact_result.removed_message_count > 0:
                history_for_prompt = _compact_result.compacted_messages
                _new_compacted_history = _compact_result.compacted_messages
                logger.info(
                    "perception_node CP-6: auto-compacted history — removed=%d, new_len=%d",
                    _compact_result.removed_message_count,
                    len(history_for_prompt),
                )
                try:
                    if orchestrator and hasattr(orchestrator, "event_bus"):
                        orchestrator.event_bus.publish(
                            "context.auto_compacted",
                            {
                                "removed_message_count": _compact_result.removed_message_count,
                                "new_message_count": len(history_for_prompt),
                                "session_id": state.get("session_id"),
                            },
                        )
                except Exception:
                    pass
    except Exception as _ac_err:
        logger.debug(
            "perception_node CP-6: auto-compaction skipped (non-fatal): %s", _ac_err
        )
    return history_for_prompt, _new_compacted_history


def _parse_tool_call_and_flags(
    resp: Any, content: str, state: Mapping[str, Any]
) -> tuple[dict | None, str, bool, bool, str]:
    """Parse tool call from response/content and compute helper flags.

    Returns (tool_call, content_stripped, thinking_only, is_truncated_yaml, content_no_thinking)
    """
    # Normalize content
    content_stripped = content.strip() if content else ""

    # Compute thinking-stripped content
    try:
        from src.core.inference.thinking_utils import strip_thinking as _st

        content_no_thinking = _st(content_stripped)
    except Exception:
        content_no_thinking = content_stripped

    thinking_only = not content_no_thinking

    # Log if last history message was a tool result (non-fatal)
    try:
        prior_history = state.get("history") or []
        if isinstance(prior_history, list) and prior_history:
            last_msg = prior_history[-1]
            if last_msg.get("role") == "tool":
                logger.info("perception_node: last message was a tool result")
    except Exception:
        pass

    tool_call = None
    try:
        # Prefer native provider tool_calls first
        tool_call = _parse_native_tool_call_from_resp(resp)

        # Fallback to YAML parsing when appropriate
        if (
            not tool_call
            and content
            and "tool_execution_result" not in content
            and '"tool_execution_result"' not in content
        ):
            tool_call = _parse_yaml_tool_call_from_content(content)
        elif not tool_call:
            logger.info(
                "perception_node: skipping parse_tool_block because content contains tool_execution_result"
            )

        # Prompt injection guard
        if tool_call is not None and _detect_prompt_injection(tool_call, state):
            tool_name_extracted = tool_call.get("name", "")
            logger.warning(
                f"perception_node: F8 injection guard — tool call '{tool_name_extracted}' "
                "matches a user-role message (name + args); rejecting to prevent prompt injection"
            )
            tool_call = None
    except Exception:
        tool_call = None

    # Detect truncated YAML: model started a ```yaml block but couldn't complete it.
    _is_truncated_yaml = bool(
        tool_call is None
        and content_stripped
        and not thinking_only
        and "```yaml" in content_stripped
        and not any(
            sig in content_stripped.lower()
            for sig in ("status: complete", "task is complete", "result:")
        )
    )

    return (
        tool_call,
        content_stripped,
        thinking_only,
        _is_truncated_yaml,
        content_no_thinking,
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
    """Wrap ContextBuilder.build_prompt and the in-place message injections.

    Preserves the exact behaviour previously inline in _perception_node_impl so
    callers can be switched to this helper without behavioural changes.
    """
    # Build the base messages via ContextBuilder
    try:
        max_tokens = _get_context_budget() if _get_context_budget is not None else 6000
    except Exception:
        max_tokens = 6000

    provider_capabilities = {}
    try:
        if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
            provider_capabilities = orchestrator.get_provider_capabilities()
    except Exception:
        provider_capabilities = {}

    messages = builder.build_prompt(
        role_name=perception_role,
        active_skills=active_skills,
        task_description=state["task"],
        tools=tools_list,
        conversation=history_for_prompt,
        retrieved_snippets=retrieved_snippets,
        max_tokens=max_tokens,
        provider_capabilities=provider_capabilities,
        model_tier=state.get("model_tier"),
        model_name=active_model_name or "",
    )

    # S9-A: Inject prior-session memories into the system message (round 0 only).
    try:
        _prior_context_block = ""
        if (state.get("rounds") or 0) == 0:
            try:
                _prior_context_block = builder.inject_prior_session_memories(
                    task=state.get("task", ""), limit=3
                )
            except Exception:
                _prior_context_block = ""
        if _prior_context_block and messages and messages[0].get("role") == "system":
            messages[0] = {
                **messages[0],
                "content": _prior_context_block + "\n\n" + messages[0]["content"],
            }
    except Exception:
        pass

    # MEM-2: Inject recent cross-session decisions on round 0 (non-critical).
    try:
        if (
            (state.get("rounds") or 0) == 0
            and messages
            and messages[0].get("role") == "system"
        ):
            _ss = getattr(orchestrator, "session_store", None) if orchestrator else None
            if _ss and hasattr(_ss, "read_recent_decisions"):
                _recent = _ss.read_recent_decisions(max_entries=5)
                if _recent:
                    _dec_lines = "\n".join(
                        f"- {d.get('decision', '')} ({d.get('created_at', '')})"
                        for d in _recent
                    )
                    messages[0] = {
                        **messages[0],
                        "content": (
                            f"## Recent task decisions (cross-session memory)\n{_dec_lines}\n\n"
                            + messages[0]["content"]
                        ),
                    }
    except Exception:
        pass

    # ORCH-W1: Inject max_steps.txt warning into the system message when near the turn limit.
    try:
        _turn_count_now = int((state.get("turn_count") or 0))
        _project_max_turns: int | None = None
        try:
            if _gas is not None:
                _ps = _gas()
                if _ps is not None and _ps.max_turns is not None:
                    _project_max_turns = _ps.max_turns
        except Exception:
            pass
        _max_turns_now = int(state.get("max_turns") or _project_max_turns or 50)
        _near_limit = _turn_count_now >= (_max_turns_now - 2)
        if _near_limit and messages and messages[0].get("role") == "system":
            try:
                _tpl_path = (
                    Path(__file__).parent.parent.parent.parent
                    / "prompts"
                    / "templates"
                    / "max_steps.txt"
                )
                if _tpl_path.exists():
                    _max_steps_text = _tpl_path.read_text(encoding="utf-8").strip()
                    if _max_steps_text:
                        messages[0] = {
                            **messages[0],
                            "content": messages[0]["content"]
                            + f"\n\n{_max_steps_text}",
                        }
            except Exception:
                pass
    except Exception:
        pass

    # MID-INJ: On rounds > 0, drain any mid-run user messages buffered by the
    # TUI bridge and inject them as <system-reminder> blocks appended to the
    # messages list.
    try:
        _current_round = state.get("rounds") or 0
        if _current_round > 0:
            _inj_source = state.get("_pending_injections_source")
            if _inj_source is not None and callable(
                getattr(_inj_source, "pop_pending_injections", None)
            ):
                _injected_msgs = _inj_source.pop_pending_injections()
                for _inj_text in _injected_msgs:
                    _reminder = (
                        "<system-reminder>\n"
                        "The user sent the following message:\n"
                        f"{_inj_text}\n\n"
                        "Please address this message and continue with your tasks.\n"
                        "</system-reminder>"
                    )
                    messages.append({"role": "user", "content": _reminder})
    except Exception:
        pass

    return messages


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
    orchestrator = _resolve_orchestrator(state, config)
    if orchestrator is None:
        logger.error("perception_node: orchestrator is None in config")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": ["orchestrator not found in config"],
        }

    # Check for cancellation - dynamically resolve from orchestrator if not in state
    cancel_event = state.get("cancel_event")
    if not cancel_event:
        cancel_event = getattr(orchestrator, "cancel_event", None)
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("perception_node: Task canceled by user")
        return {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
            "empty_response_count": 0,
        }

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
    if turn_count > max_turns:
        logger.warning(
            "perception_node: turn_count=%d >= max_turns=%d — routing to END",
            turn_count,
            max_turns,
        )
        try:
            orchestrator.event_bus.publish(
                "task.turn_limit",
                {"turn_count": turn_count, "max_turns": max_turns},
            )
        except Exception:
            pass
        return {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "turn_count": turn_count,
            "last_result": {
                "ok": False,
                "error": f"Turn limit reached ({max_turns} turns). Task stopped.",
            },
            "errors": ["turn_limit_reached"],
        }

    # Validate call_model is available
    if not callable(call_model):
        logger.error(f"perception_node: call_model is not callable: {call_model}")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "turn_count": turn_count,
            "errors": ["call_model not available"],
        }

    try:
        adapter = orchestrator.adapter
    except Exception as e:
        logger.error(f"perception_node: failed to get adapter: {e}")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": [f"adapter error: {e}"],
        }
    if adapter is None:
        logger.warning("perception_node: orchestrator.adapter is None")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": ["adapter is None"],
        }

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
    tools_list = [
        {"name": n, "description": m.get("description", "")}
        for n, m in orchestrator.tool_registry.tools.items()
    ]

    # ORCH-W1: When within 2 turns of the limit, remove write tools so the model
    # stops attempting new edits and focuses on summarisation/verification only.
    # PN-4: Use the already-incremented `turn_count` local (computed at top of function)
    # rather than re-reading the stale pre-increment value from state.
    # Normalize to plain ints so static analyzers don't infer Optional[int]
    _turn_count_now = int(turn_count)
    _max_turns_now = int(max_turns)
    _near_limit = _turn_count_now >= (_max_turns_now - 2)
    if _near_limit:
        try:
            if _MODIFYING_TOOLS:
                tools_list = [
                    t for t in tools_list if t["name"] not in _MODIFYING_TOOLS
                ]
            logger.info(
                "perception_node: near turn limit (%d/%d) — write tools removed from prompt",
                _turn_count_now,
                _max_turns_now,
            )
        except Exception:
            pass

    # Dynamic skill injection: if task involves debugging or deep searching, inject by name
    active_skills = []
    task_lower = state.get("task", "").lower()
    if any(
        kw in task_lower
        for kw in ["debug", "fix", "error", "bug", "search", "find", "analyze"]
    ):
        active_skills.append("context_hygiene")
        logger.info(
            "perception_node: injected context_hygiene skill for debugging/searching task"
        )

    # CP-6: Pre-turn deterministic auto-compaction.
    # Run before the prompt is built so the compacted history feeds into
    # build_prompt() and the LLM never sees the over-full context.
    # This is separate from the post-turn overflow-based _should_distill path.
    #
    # CP6-PERSIST: If a prior turn already produced a compacted snapshot,
    # start from that instead of the ever-growing raw history.  This prevents
    # the compactor from re-firing on every turn once the threshold is crossed.
    _prior_compacted = state.get("_compacted_history")
    if _prior_compacted and isinstance(_prior_compacted, list):
        _history_for_prompt = list(_prior_compacted)
        # Append any raw turns that arrived after the compaction snapshot by
        # counting how many messages the compacted base already covers.  The
        # raw history still grows via operator.add; new turns are anything
        # beyond what the compacted snapshot represents.  We approximate by
        # appending raw turns whose content is NOT already in the compacted set.
        _compacted_contents = {
            m.get("content", "") for m in _prior_compacted if isinstance(m, dict)
        }
        _raw_history = list(state.get("history") or [])
        for _m in _raw_history:
            if (
                isinstance(_m, dict)
                and _m.get("content", "") not in _compacted_contents
            ):
                _history_for_prompt.append(_m)
    else:
        _history_for_prompt = list(state.get("history") or [])

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

    # S9-A: On the first round of a new task, inject relevant memories from prior sessions.
    _prior_context_block = ""
    if (state.get("rounds") or 0) == 0:
        try:
            _prior_context_block = builder.inject_prior_session_memories(
                task=state.get("task", ""), limit=3
            )
        except Exception:
            pass

    # Assemble the tiered context
    # Conservative provider/model resolution used throughout the codebase.
    # Resolution priority:
    # 1) orchestrator.get_provider_capabilities() (authoritative)
    # 2) ProviderManager.get_provider_capabilities(adapter)
    # 3) adapter attributes (provider, default_model, models)
    # Accept only concrete strings (no MagicMock placeholders). Guard imports
    # locally to avoid circular import issues in tests.
    provider_capabilities = {}
    try:
        caps: dict = {}

        # 1) Orchestrator-level capabilities (authoritative)
        try:
            if (
                orchestrator
                and hasattr(orchestrator, "get_provider_capabilities")
                and callable(getattr(orchestrator, "get_provider_capabilities"))
            ):
                _rc = orchestrator.get_provider_capabilities()
                if isinstance(_rc, dict) and _rc:
                    caps = dict(_rc)
        except Exception:
            caps = {}

        # 2) ProviderManager fallback
        if not caps:
            try:
                from src.core.inference.llm_manager import (
                    get_provider_manager as _gpm,
                )

                _pm = _gpm()
                _rc = _pm.get_provider_capabilities(adapter)
                if isinstance(_rc, dict) and _rc:
                    caps = dict(_rc)
            except Exception:
                caps = caps or {}

        # 3) Adapter-only last resort (no network probes)
        if not caps and adapter:
            try:
                prov_attr = getattr(adapter, "provider", None)
            except Exception:
                prov_attr = None
            provider_name = None
            try:
                provider_name = _extract_str(prov_attr)
            except Exception:
                provider_name = None
            if not provider_name:
                try:
                    provider_name = _extract_str(getattr(adapter, "name", None))
                except Exception:
                    provider_name = None

            model = None
            try:
                model = _extract_str(getattr(adapter, "default_model", None))
            except Exception:
                model = None
            if not model:
                try:
                    models_attr = getattr(adapter, "models", None)
                    if isinstance(models_attr, (list, tuple)):
                        for m in models_attr:
                            mm = _extract_str(m)
                            if mm:
                                model = mm
                                break
                    else:
                        model = _extract_str(models_attr)
                except Exception:
                    model = None

            supports_native_tools = False
            try:
                if isinstance(prov_attr, dict):
                    supports_native_tools = bool(
                        prov_attr.get("supports_native_tools", False)
                    )
                else:
                    supports_native_tools = bool(
                        getattr(adapter, "supports_native_tools", False)
                    )
            except Exception:
                supports_native_tools = False

            provider_family = "default"
            try:
                from src.core.orchestration.provider_capabilities import (
                    _map_provider_family_impl as _map_pf,
                )

                provider_family = _map_pf(provider_name or "")
            except Exception:
                provider_family = "default"

            caps = {
                "supports_native_tools": bool(supports_native_tools),
                "provider_family": provider_family,
                "model": model,
                "provider_name": provider_name or "",
            }

        # Sanitize and expose a conservative provider_capabilities dict
        try:
            _pname = _extract_str(
                caps.get("provider_name") or caps.get("provider") or caps.get("name")
            )
        except Exception:
            _pname = None
        try:
            _model = _extract_str(caps.get("model") or caps.get("default_model"))
        except Exception:
            _model = None

        provider_capabilities = {
            "supports_native_tools": bool(caps.get("supports_native_tools", False)),
            "provider_family": caps.get("provider_family") or "default",
            "model": _model,
            "provider_name": _pname or "",
        }
    except Exception:
        provider_capabilities = {}

    # ORCH-W4: Select role based on agent_mode.  When plan_enter has been called,
    # the orchestrator sets _agent_mode="planning" and the state carries agent_mode.
    # Use the strategic role so the LLM focuses on planning rather than execution.
    _agent_mode = (
        state.get("agent_mode")
        or getattr(orchestrator, "_agent_mode", None)
        or "execution"
    )
    _perception_role = "strategic" if _agent_mode == "planning" else "operational"

    # OP-1: Resolve active model name for per-provider prompt selection.
    # Prefer the sanitized provider_capabilities model (authoritative). Only
    # fall back to adapter.models / adapter.default_model after applying the
    # shared extract_str heuristic so MagicMock placeholders are not propagated
    # into the prompt generation logic.
    _active_model_name: str = ""
    try:
        if provider_capabilities and provider_capabilities.get("model"):
            _active_model_name = provider_capabilities.get("model") or ""
        else:
            if orchestrator and getattr(orchestrator, "adapter", None):
                _extract = _extract_str

                # Inspect adapter.models first
                _models = getattr(orchestrator.adapter, "models", None)
                if _models:
                    if isinstance(_models, (list, tuple)):
                        for m in _models:
                            mm = _extract(m)
                            if mm:
                                _active_model_name = mm
                                break
                    else:
                        mm = _extract(_models)
                        if mm:
                            _active_model_name = mm

                # Fall back to default_model if still empty
                if not _active_model_name and hasattr(
                    orchestrator.adapter, "default_model"
                ):
                    dm = _extract(getattr(orchestrator.adapter, "default_model", None))
                    if dm:
                        _active_model_name = dm
    except Exception:
        pass

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

    # Determine model/provider. Prefer orchestrator.get_provider_capabilities()
    # so ProviderManager is the authoritative source. If that doesn't yield
    # values, try the ProviderManager directly with the current adapter, and
    # only as a last resort inspect adapter.provider/default_model.
    provider = None
    model = None

    try:
        if (
            orchestrator
            and hasattr(orchestrator, "get_provider_capabilities")
            and callable(getattr(orchestrator, "get_provider_capabilities"))
        ):
            caps = orchestrator.get_provider_capabilities()
            if isinstance(caps, dict):
                p_raw = _extract_str(
                    caps.get("provider_name")
                    or caps.get("provider")
                    or caps.get("name")
                )
                if p_raw:
                    provider = p_raw
                m_raw = _extract_str(caps.get("model") or caps.get("default_model"))
                if m_raw:
                    model = m_raw
    except Exception:
        provider = None
        model = None

    # If orchestrator-level caps didn't provide both values, try ProviderManager
    # with the adapter as a conservative secondary source.
    if provider is None or model is None:
        try:
            from src.core.inference.llm_manager import get_provider_manager

            pm = get_provider_manager()
            if pm:
                try:
                    caps = pm.get_provider_capabilities(adapter)
                    if isinstance(caps, dict):
                        p_raw = caps.get("provider_name") or caps.get("provider")
                        if provider is None and _valid_str(p_raw):
                            provider = p_raw
                        m_raw = caps.get("model")
                        if model is None and _valid_str(m_raw):
                            model = m_raw
                except Exception:
                    # Non-fatal: fall through to adapter inspection
                    pass
        except Exception:
            # Import/lookup failed; continue to adapter inspection
            pass

    # Final fallback: inspect adapter.provider / adapter.models / adapter.default_model
    if (provider is None or model is None) and adapter:
        logger.info(f"perception_node: adapter type: {type(adapter)}")
        try:
            # provider may be a dict or a string on different adapters
            if hasattr(adapter, "provider"):
                p_attr = getattr(adapter, "provider")
                p_cand = _extract_str(p_attr)
                if p_cand and provider is None:
                    provider = p_cand
                    logger.info(f"perception_node: provider from adapter: {provider}")
        except Exception:
            pass
        try:
            # Prefer explicit default_model, else pick the first concrete entry
            # from adapter.models
            if provider is None:
                # no-op: keep provider unchanged
                pass
            if hasattr(adapter, "default_model"):
                dm = _extract_str(getattr(adapter, "default_model", None))
                if dm and model is None:
                    model = dm
                    logger.info(
                        f"perception_node: model from adapter.default_model: {model}"
                    )
            if model is None and hasattr(adapter, "models"):
                ms = getattr(adapter, "models")
                if isinstance(ms, list):
                    for m in ms:
                        m_cand = _extract_str(m)
                        if m_cand:
                            model = m_cand
                            break
        except Exception:
            pass
    elif not adapter:
        logger.warning("perception_node: adapter is None!")

    # S1-A: Classify model tier and inject into state for ContextBuilder + execution_node.
    _model_tier_str: str | None = None
    if model:
        try:
            if _classify_model is None:
                raise RuntimeError("model_tiers unavailable")
            ctx_window = 0
            if adapter and hasattr(adapter, "context_window"):
                ctx_window = int(adapter.context_window or 0)
            _model_tier_str = _classify_model(model, ctx_window).value
        except Exception:
            pass

    # GAP-10: Warn once on round 0 when the context window is suspiciously small
    # for SMALL/FRONTIER models.  The primary trigger is Gemma 4 E4B running in
    # LM Studio with the default n_ctx=7168 — the model supports 128K but the
    # slot configuration limits it to 7168, causing context overflow within a
    # few turns.  Published as ui.notification (warning) so the TUI toast fires.
    _rounds_now = state.get("rounds") or 0
    if _rounds_now == 0 and _model_tier_str in ("small", "frontier"):
        try:
            _ctx_win = 0
            if adapter and hasattr(adapter, "context_window"):
                _ctx_win = int(adapter.context_window or 0)
            # 16384 = 16K threshold: anything below this for a SMALL/FRONTIER
            # model is almost certainly a misconfigured slot, not intentional.
            if 0 < _ctx_win < 16384:
                _warn_msg = (
                    f"Context window is very small ({_ctx_win:,} tokens) for "
                    f"{model}. "
                    f"The model supports up to 128K\u2013256K tokens. "
                    f"Increase n_ctx in LM Studio to at least 32768 for better "
                    f"agentic performance."
                )
                logger.warning("GAP-10 context-window warning: %s", _warn_msg)
                try:
                    if orchestrator and hasattr(orchestrator, "event_bus"):
                        orchestrator.event_bus.publish(
                            "ui.notification",
                            {
                                "level": "warning",
                                "message": _warn_msg,
                                "source": "context_window_check",
                            },
                        )
                except Exception:
                    pass
        except Exception:
            pass

    # GAP-SMALL-4: Clarification guard for NANO/SMALL models on round 0.
    # When the task string is very short (< 8 words) and contains no file
    # references, code identifiers, or clear action verbs, small models are
    # likely to hallucinate a plan.  Return a clarifying question instead of
    # entering the pipeline on a bad premise.
    if _rounds_now == 0 and _model_tier_str == "small":
        _raw_task = (state.get("task") or "").strip()
        _task_words = _raw_task.split()
        _ACTION_VERBS = {
            "add",
            "fix",
            "update",
            "change",
            "create",
            "delete",
            "remove",
            "refactor",
            "write",
            "read",
            "run",
            "test",
            "debug",
            "find",
            "search",
            "show",
            "list",
            "explain",
            "implement",
            "build",
            "install",
            "deploy",
            "check",
            "verify",
            "move",
            "rename",
            # GAP-SMALL-4 fix: common summarization / information verbs that
            # were missing and caused "summarize the project readme" to be
            # misclassified as ambiguous, triggering unnecessary clarification.
            "summarize",
            "summary",
            "describe",
            "review",
            "analyse",
            "analyze",
            "generate",
            "print",
            "display",
            "get",
            "fetch",
            "make",
            "set",
        }
        _has_action = any(w.lower() in _ACTION_VERBS for w in _task_words)
        _has_file_ref = bool(re.search(r"\w+\.\w+|/\w+|\w+\.py\b", _raw_task))
        _has_code_id = bool(re.search(r"`[^`]+`|\"[A-Za-z_]\w+\"", _raw_task))
        if (
            len(_task_words) < 8
            and not _has_action
            and not _has_file_ref
            and not _has_code_id
        ):
            logger.info(
                "perception_node: GAP-SMALL-4 ambiguous task detected for small model, "
                "returning clarification prompt (task=%r, words=%d)",
                _raw_task[:80],
                len(_task_words),
            )
            _clarify_msg = (
                "I need a bit more detail to help you effectively. Could you tell me:\n"
                "- What file or component should I work on?\n"
                "- What should change or be created?\n"
                "- What is the expected outcome?"
            )
            return {
                "history": [{"role": "assistant", "content": _clarify_msg}],
                "next_action": None,
                "needs_clarification": True,
                "rounds": _rounds_now + 1,
                "turn_count": turn_count,
                "empty_response_count": 0,
                **({"model_tier": _model_tier_str} if _model_tier_str else {}),
            }
    llm_kwargs = {}
    try:
        if orchestrator and getattr(orchestrator, "deterministic", False):
            llm_kwargs["temperature"] = 0.0
            seed = getattr(orchestrator, "seed", None)
            if seed is not None:
                llm_kwargs["seed"] = seed
        else:
            llm_kwargs["temperature"] = 0.4
    except Exception:
        pass

    # Handle thinking mode for reasoning models like Qwen3.5-9B
    # Increase token budget to accommodate thinking tokens and disable thinking for efficiency
    try:
        if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
            caps = orchestrator.get_provider_capabilities()
            if isinstance(caps, dict):
                model_name = caps.get("model") or caps.get("default_model") or ""
                if model_name:
                    from src.core.inference.thinking_utils import (
                        is_reasoning_model,
                        budget_max_tokens,
                        supports_no_think,
                    )

                    if is_reasoning_model(model_name):
                        # Increase max_tokens to accommodate thinking tokens
                        current_max = llm_kwargs.get("max_tokens", 0)
                        if current_max > 0:
                            adjusted_max = budget_max_tokens(current_max, model_name)
                            llm_kwargs["max_tokens"] = adjusted_max
                            logger.info(
                                f"[THINKING_MODE] Increased max_tokens for reasoning model {model_name}: {current_max} -> {adjusted_max}"
                            )

                        # For Qwen3 and similar models that support /no_think, disable thinking to save tokens
                        if supports_no_think(model_name):
                            llm_kwargs["think"] = False
                            logger.info(
                                f"[THINKING_MODE] Disabled thinking for model {model_name}"
                            )
                    else:
                        # For non-reasoning models, ensure thinking is off if supported
                        if supports_no_think(model_name):
                            llm_kwargs["think"] = False
                            logger.info(
                                f"[THINKING_MODE] Disabled thinking for non-reasoning model {model_name}"
                            )
    except Exception as e:
        logger.debug(
            f"[THINKING_MODE] Error in thinking mode handling: {e}"
        )  # Fail gracefully - don't break LLM calls over thinking mode issues

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
        call_model_fn=call_model,
    )
    if early_resp is not None:
        return early_resp

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
                orchestrator.event_bus.publish(
                    "context.overflow",
                    {
                        "prompt_tokens": 0,
                        "budget": 0,
                        "reserved": 0,
                        "session_id": state.get("session_id"),
                        "source": "api_error",
                    },
                )
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
        _is_truncated_yaml,
        _content_no_thinking,
    ) = _parse_tool_call_and_flags(resp, content, state)

    # Only run the corrective/no-tool helper when no tool_call was extracted
    # (matches the original inline behaviour).
    if tool_call is None:
        try:
            _no_tool_result = _handle_no_tool_or_empty_response(
                content,
                content_stripped,
                thinking_only,
                _is_truncated_yaml,
                state,
                orchestrator,
                _model_tier_str,
            )
            if _no_tool_result is not None:
                return _no_tool_result
        except Exception:
            # Non-fatal: continue normal flow when helper fails
            pass

    # Handle content without tool calls - if we have meaningful content and retried, return it
    current_empty_response_count = int(state.get("empty_response_count") or 0)
    if _content_no_thinking.strip() and current_empty_response_count >= 1:
        # Check if the content looks like a reasonable answer (not just thinking process)
        content_lower = _content_no_thinking.lower().strip()
        # Skip if it's clearly still thinking-related
        if not any(
            phrase in content_lower
            for phrase in [
                "thinking process",
                "analyze the request",
                "let me think",
                "i need to think",
                "first,",
                "second,",
                "third,",
                "step 1",
                "step 2",
                "step 3",
            ]
        ):
            # Return the content as final answer
            return {
                "history": state["history"]
                + [{"role": "assistant", "content": _content_no_thinking.strip()}],
                "next_action": None,  # We are done
                "rounds": _rounds_now + 1,
                "turn_count": turn_count,
                "empty_response_count": 0,  # Reset counter since we got a response
                **({"model_tier": _model_tier_str} if _model_tier_str else {}),
            }

    # Preserve plan state if already exists
    current_plan = state.get("current_plan")
    current_step = state.get("current_step")
    task_decomposed = state.get("task_decomposed")
    original_task = state.get("original_task")

    # FIX: Return ONLY the new message, not the full history.
    # LangGraph's operator.add reducer will handle appending to the existing history.
    # This prevents the exponential duplication bug (2→4→8→16→32→64 messages).
    # Also: don't add empty content to history - it confuses the model!
    if content and content.strip():
        new_messages = [{"role": "assistant", "content": content}]
    else:
        # Skip adding empty content to history - it causes confusion
        new_messages = []

    # Ensure empty_response_count is always defined for the result shape.
    empty_response_count = int(state.get("empty_response_count") or 0)

    result = {
        "history": new_messages,
        "next_action": tool_call,
        "rounds": state.get("rounds", 0) + 1,
        "turn_count": turn_count,
        "empty_response_count": empty_response_count,
        # CF-1 fix: clear errors from prior turns so stale context_overflow (or any
        # previous error) does not propagate into the next graph round and cause
        # route_after_perception to mis-route to memory_sync without doing anything.
        "errors": [],
        **_overflow_compaction,
    }

    # S1-A: Persist model tier into state so downstream nodes can adapt.
    if _model_tier_str is not None:
        result["model_tier"] = _model_tier_str

    # S6-A: Accumulate session cost.
    if _session_cost_delta > 0:
        _prior_cost = float(state.get("session_cost_usd") or 0.0)
        result["session_cost_usd"] = round(_prior_cost + _session_cost_delta, 8)

    # S4-A: Take a lightweight git tree-hash snapshot before each LLM turn.
    # The hash is appended to AgentState.snapshots so the full session diff
    # (first → last) is available for /diff and session revert.
    try:
        _snap_mgr = (
            getattr(orchestrator, "snapshot_manager", None) if orchestrator else None
        )
        if _snap_mgr is not None:
            _snap_hash = await _snap_mgr.track()
            if _snap_hash:
                # P1-G: cap snapshots to the most recent 10 entries to prevent
                # unbounded list growth over a long multi-task session.
                _prior_snaps = list(state.get("snapshots") or [])[-9:]
                _prior_snaps.append(_snap_hash)
                result["snapshots"] = _prior_snaps
    except Exception:
        pass

    # Preserve plan-related fields
    if current_plan is not None:
        result["current_plan"] = current_plan
    if current_step is not None:
        result["current_step"] = current_step
    if task_decomposed is not None:
        result["task_decomposed"] = task_decomposed
    if original_task is not None:
        result["original_task"] = original_task

    # ORCH-W4: Persist agent_mode into state so it survives across node transitions.
    _current_agent_mode = (
        getattr(orchestrator, "_agent_mode", None) if orchestrator else None
    )
    if _current_agent_mode is not None:
        result["agent_mode"] = _current_agent_mode

    # WF-1: Set task_complexity flag so route_after_perception can read a pre-computed
    # verdict instead of re-running the keyword heuristic blind.
    # perception_node has richer context here: relevant_files count, tool_call_count,
    # plus the same keyword check used by builder._task_is_complex().
    try:
        if _tic is None:
            raise RuntimeError("builder unavailable")
        _tc_flag = "complex" if _tic(state) else "simple"
        result["task_complexity"] = _tc_flag
        logger.info("perception_node WF-1: task_complexity=%s", _tc_flag)
    except Exception:
        pass  # Never block on routing helper failure; builder falls back to heuristic

    # CP6-PERSIST: Write the new compacted snapshot back to AgentState so the
    # next turn starts from the compacted base rather than re-compacting the
    # ever-growing raw history.
    # P2-C: Also record the round number for the min-gap cooldown.
    if _new_compacted_history is not None:
        result["_compacted_history"] = _new_compacted_history
        result["_compaction_last_round"] = int(state.get("rounds") or 0)

    return result
