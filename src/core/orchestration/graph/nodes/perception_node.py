import asyncio
import logging
import re
from typing import Mapping, Dict, Any

from src.core.orchestration.graph.state import AgentState, validate_state
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.event_bus import run_with_correlation

logger = logging.getLogger(__name__)


async def perception_node(state: Mapping[str, Any], config: Any) -> Dict[str, Any]:
    """
    Perception Layer: Responsible for generating the next action or thought.
    Uses the 'operational' role from ContextBuilder (loaded from agent-brain).
    Dynamic skill injection: If task involves debugging/searching, injects 'context_hygiene' skill.
    """
    logger.info("=== perception_node START ===")

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
        from src.core.orchestration.project_settings import get_active_settings as _gas

        _ps = _gas()
        if _ps is not None and _ps.max_turns is not None:
            _project_max_turns = _ps.max_turns
    except Exception:
        pass
    max_turns = state.get("max_turns") or _project_max_turns or 50
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
        if adapter is None:
            logger.warning("perception_node: orchestrator.adapter is None")
    except Exception as e:
        logger.error(f"perception_node: failed to get adapter: {e}")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": [f"adapter error: {e}"],
        }

    # Pre-retrieval: consult repo intelligence tools if available (search_code, find_symbol, find_references)
    # F9: Skip pre-retrieval on rounds > 0 — context was already gathered in round 0.
    # PB-3 fix: run all retrieval tasks concurrently with asyncio.gather so the total
    # latency is max(individual latencies) rather than sum(individual latencies).
    retrieved_snippets = []
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
                # RA-2 fix: issue parallel search_code calls for all extracted
                # symbols (up to 3) instead of only the first one.  Concurrent
                # requests share the same asyncio.gather below.
                _queries = symbol_queries[:3] if symbol_queries else [query]
                results = await asyncio.gather(
                    *[
                        run_with_correlation(  # D-07: propagate correlation ID
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
                    r = await run_with_correlation(  # D-07
                        loop,
                        None,
                        lambda sq=_sq: _safe_call(
                            "find_symbol", name=sq, workdir=_workdir
                        ),
                    )
                    results.append(r)
                return results

            async def _fetch_references():
                return await run_with_correlation(  # D-07
                    loop,
                    None,
                    lambda: _safe_call("find_references", name=query, workdir=_workdir),
                )

            # P3-2: Pre-retrieve test files for the queried symbols so test context
            # is available from round 0 without waiting for analysis_node.
            async def _fetch_test_files():
                results = []
                try:
                    from src.core.indexing.symbol_graph import SymbolGraph

                    sg = SymbolGraph(_workdir)
                    for _sq in symbol_queries[:2]:
                        tests = await run_with_correlation(  # D-07
                            loop, None, lambda sq=_sq: sg.find_tests_for_module(sq)
                        )
                        if tests and isinstance(tests, list):
                            results.extend(tests[:2])
                except Exception:
                    pass
                return results

            sc_result, sym_results, fr_result, test_file_results = await asyncio.gather(
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
    _turn_count_now = turn_count
    _max_turns_now = max_turns
    _near_limit = _turn_count_now >= _max_turns_now - 2
    if _near_limit:
        try:
            from src.core.orchestration.loop_guards import MODIFYING_TOOLS

            tools_list = [t for t in tools_list if t["name"] not in MODIFYING_TOOLS]
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
    _history_for_prompt = list(state.get("history") or [])
    try:
        from src.core.memory.auto_compactor import (
            AutoCompactConfig,
            should_compact,
            compact_messages,
        )
        from src.core.config_loader import get as _cfg_get

        _ac_max_tokens: int = int(_cfg_get("auto_compact_max_tokens", 10_000) or 10_000)
        _ac_preserve: int = int(_cfg_get("auto_compact_preserve_recent", 4) or 4)
        _ac_config = AutoCompactConfig(
            preserve_recent=_ac_preserve,
            max_tokens=_ac_max_tokens,
        )
        if should_compact(_history_for_prompt, _ac_config):
            _compact_result = compact_messages(_history_for_prompt, _ac_config)
            if _compact_result.removed_message_count > 0:
                _history_for_prompt = _compact_result.compacted_messages
                logger.info(
                    "perception_node CP-6: auto-compacted history — "
                    "removed=%d, new_len=%d",
                    _compact_result.removed_message_count,
                    len(_history_for_prompt),
                )
                try:
                    if orchestrator and hasattr(orchestrator, "event_bus"):
                        orchestrator.event_bus.publish(
                            "context.auto_compacted",
                            {
                                "removed_message_count": _compact_result.removed_message_count,
                                "new_message_count": len(_history_for_prompt),
                                "session_id": state.get("session_id"),
                            },
                        )
                except Exception:
                    pass
    except Exception as _ac_err:
        logger.debug(
            "perception_node CP-6: auto-compaction skipped (non-fatal): %s", _ac_err
        )

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
    provider_capabilities = {}
    if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
        provider_capabilities = orchestrator.get_provider_capabilities()

    # ORCH-W4: Select role based on agent_mode.  When plan_enter has been called,
    # the orchestrator sets _agent_mode="planning" and the state carries agent_mode.
    # Use the strategic role so the LLM focuses on planning rather than execution.
    _agent_mode = (
        state.get("agent_mode")
        or getattr(orchestrator, "_agent_mode", None)
        or "execution"
    )
    _perception_role = "strategic" if _agent_mode == "planning" else "operational"

    messages = builder.build_prompt(
        role_name=_perception_role,
        active_skills=active_skills,
        task_description=state["task"],
        tools=tools_list,
        conversation=_history_for_prompt,
        retrieved_snippets=retrieved_snippets,
        max_tokens=6000,
        provider_capabilities=provider_capabilities,
        model_tier=state.get(
            "model_tier"
        ),  # S1-B/S1-C: pass tier for partial + pruning
    )

    # S9-A: Inject prior-session memories into the system message (round 0 only).
    if _prior_context_block and messages and messages[0].get("role") == "system":
        # MED-18 fix: copy the dict rather than mutating it in-place so callers
        # that retained a reference to the original list entry are not surprised.
        messages[0] = {
            **messages[0],
            "content": _prior_context_block + "\n\n" + messages[0]["content"],
        }

    # MEM-2: Inject recent cross-session decisions on round 0 (non-critical).
    if (
        (state.get("rounds") or 0) == 0
        and messages
        and messages[0].get("role") == "system"
    ):
        try:
            _ss = getattr(orchestrator, "session_store", None) if orchestrator else None
            if _ss and hasattr(_ss, "read_recent_decisions"):
                _recent = _ss.read_recent_decisions(max_entries=5)
                if _recent:
                    _dec_lines = "\n".join(
                        f"- {d.get('decision', '')} ({d.get('created_at', '')})"
                        for d in _recent
                    )
                    # MED-18 fix: copy instead of mutating in-place.
                    messages[0] = {
                        **messages[0],
                        "content": (
                            f"## Recent task decisions (cross-session memory)\n{_dec_lines}\n\n"
                            + messages[0]["content"]
                        ),
                    }
        except Exception:
            pass  # MEM-2 injection must never block perception

    # ORCH-W1: Inject max_steps.txt warning into the system message when near the turn limit.
    if _near_limit and messages and messages[0].get("role") == "system":
        try:
            from pathlib import Path as _Path

            # Template lives at src/core/prompts/templates/max_steps.txt
            _tpl_path = (
                _Path(__file__).parent.parent.parent.parent
                / "prompts"
                / "templates"
                / "max_steps.txt"
            )
            if _tpl_path.exists():
                _max_steps_text = _tpl_path.read_text(encoding="utf-8").strip()
                if _max_steps_text:
                    messages[0]["content"] += f"\n\n{_max_steps_text}"
        except Exception:
            pass

    # Determine model/provider
    provider = None
    model = None
    if adapter:
        logger.info(f"perception_node: adapter type: {type(adapter)}")
        if hasattr(adapter, "provider") and isinstance(adapter.provider, dict):
            provider = (
                adapter.provider.get("name") or adapter.provider.get("type") or None
            )
            logger.info(f"perception_node: provider from adapter: {provider}")
        if hasattr(adapter, "models") and adapter.models:
            model = adapter.models[0]
        elif hasattr(adapter, "default_model") and adapter.default_model:
            model = adapter.default_model
            logger.info(f"perception_node: model from adapter: {model}")
    else:
        logger.warning("perception_node: adapter is None!")

    # S1-A: Classify model tier and inject into state for ContextBuilder + execution_node.
    _model_tier_str: str | None = None
    if model:
        try:
            from src.core.inference.model_tiers import classify_model

            ctx_window = 0
            if adapter and hasattr(adapter, "context_window"):
                ctx_window = int(adapter.context_window or 0)
            _model_tier_str = classify_model(model, ctx_window).value
        except Exception:
            pass

    # Determine deterministic overrides if orchestrator requests them
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

    # LLM Inference
    logger.info(
        f"perception_node: calling call_model with provider={provider}, model={model}"
    )

    # Dynamically resolve cancel_event from orchestrator if not in state
    cancel_event = state.get("cancel_event")
    if not cancel_event and orchestrator:
        cancel_event = getattr(orchestrator, "cancel_event", None)

    try:
        # F14: call_model is always async; use create_task directly.
        llm_task = asyncio.create_task(
            call_model(
                messages,
                provider=provider,
                model=model,
                stream=False,
                format_json=False,
                tools=None,
                session_id=state.get("session_id"),
                **llm_kwargs,
            )
        )
        # Interrupt Polling: Check cancel_event every 0.2s during LLM generation
        while not llm_task.done():
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                llm_task.cancel()
                logger.info("perception_node: Task canceled mid-generation")
                return {
                    "history": state.get("history", []),
                    "next_action": None,
                    "rounds": state.get("rounds", 0) + 1,
                    "errors": ["canceled"],
                }
            await asyncio.sleep(0.2)
        resp = await llm_task
    except asyncio.CancelledError:
        logger.info("perception_node: Task cancelled")
        return {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "errors": ["canceled"],
        }
    except Exception as e:
        logger.error(f"call_model failed: {e}")
        resp = {"ok": False, "error": str(e)}
        _notify_provider_limit(str(e))

    # Phase 4: Track token usage for budget management
    _overflow_compaction = {}
    _session_cost_delta: float = 0.0
    if isinstance(resp, dict):
        # CP-9: token counts are at the top-level of the normalized response
        # dict produced by generate() in the adapter, NOT nested under "usage".
        # The usage = resp.get("usage", {}) pattern was a pre-existing bug — it
        # always yielded an empty dict so this block never executed.  Read from
        # the canonical top-level keys instead.
        _resp_prompt_tokens: int = int(resp.get("prompt_tokens") or 0)
        _resp_completion_tokens: int = int(resp.get("completion_tokens") or 0)
        _resp_total_tokens: int = int(
            resp.get("total_tokens") or _resp_prompt_tokens + _resp_completion_tokens
        )
        # CP-9: Anthropic cache token counts (zero for non-Anthropic providers).
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
                    # S6-A: Accumulate session cost using pricing table.
                    try:
                        from src.core.inference.provider_context import (
                            estimate_cost_usd,
                        )

                        _active_model = resp.get("model") or (
                            adapter.default_model
                            if adapter and hasattr(adapter, "default_model")
                            else ""
                        )
                        _session_cost_delta = estimate_cost_usd(
                            _resp_prompt_tokens,
                            _resp_completion_tokens,
                            _active_model or "",
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Token tracking error: {e}")

        # Context overflow detection with reserved buffer (mirrors opencode's 20k reserve).
        # If the prompt already consumed more than (context_budget - RESERVED_OUTPUT_BUFFER)
        # tokens, the next round will almost certainly exceed the model's context window.
        # Trigger compaction now rather than waiting for a truncation error.
        try:
            from src.core.inference.provider_context import get_context_budget

            _prompt_tokens = _resp_prompt_tokens
            _RESERVED_OUTPUT_BUFFER = (
                4096  # tokens reserved for model to generate output
            )
            _budget = get_context_budget()
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
                                "budget": _budget,
                                "reserved": _RESERVED_OUTPUT_BUFFER,
                                "session_id": state.get("session_id"),
                            },
                        )
                except Exception:
                    pass
        except Exception as _ov_err:
            logger.debug(f"context overflow check error (non-fatal): {_ov_err}")

    # Debug: log raw response for troubleshooting
    try:
        logger.info(
            f"perception_node: raw LLM resp content: {repr(resp.get('choices', [{}])[0].get('message', {}).get('content', '')[:100])}"
        )
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
        if resp.get("choices"):
            ch = resp["choices"][0].get("message")
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

    # Infinite Loop Prevention: Detect empty/stripped responses
    empty_response_count = int(state.get("empty_response_count") or 0)
    content_stripped = content.strip() if content else ""

    # Check if content is empty or just contains thinking blocks
    is_empty_response = (
        not content_stripped
        or content_stripped.replace("<think>", "").replace("</think>", "").strip() == ""
    )

    if is_empty_response:
        empty_response_count += 1
        # Use info instead of warning to avoid log spam
        logger.info(
            f"perception_node: Empty/stripped response (count: {empty_response_count})"
        )

        # After 3 consecutive empty responses, force stop the loop
        if empty_response_count >= 3:
            logger.error(
                "perception_node: 3 consecutive empty responses - breaking loop"
            )
            return {
                "history": state.get("history", []),
                "next_action": None,
                "rounds": state.get("rounds", 0) + 1,
                "last_result": {
                    "ok": False,
                    "error": "Infinite loop detected: model produced 3 consecutive empty responses",
                },
                "errors": ["infinite_loop_empty_response"],
                "empty_response_count": 0,
            }

        # Inject corrective prompt for empty responses
        corrective_prompt = (
            "\n\n<system_reminder>\n"
            "You must output a valid YAML tool call. Do not output empty responses or thinking blocks only.\n"
            "If you cannot determine the next action, use the 'respond' tool to explain what you need.\n"
            "</system_reminder>\n"
        )
        # FIX: Return only NEW messages for LangGraph to append, not the full history
        new_messages = [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": corrective_prompt
                + "\n\nPlease provide a valid YAML tool call for your next action.",
            },
        ]

        return {
            "history": new_messages,
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "empty_response_count": empty_response_count,
        }

    # Reset empty response counter on successful content
    empty_response_count = 0

    # Parse tools
    # If the assistant content already contains a tool_execution_result (i.e. a previous
    # tool run result), we should not parse it as a new tool block. Parsing and
    # executing could cause immediate repetition of the same tool call.
    tool_call = None
    try:
        # Check if we should skip parsing based on prior history.
        # We should skip ONLY if:
        # 1. The current content itself contains tool_execution_result (we're re-parsing the same thing)
        # 2. NOT because the last message was a tool result (we need to parse new responses!)
        prior_history = state.get("history") or []
        try:
            if isinstance(prior_history, list) and prior_history:
                last_msg = prior_history[-1]
                if last_msg.get("role") == "tool":
                    logger.info("perception_node: last message was a tool result")
        except Exception:
            pass

        # Parse if: content exists AND content doesn't contain tool_execution_result
        # We should ALWAYS try to parse, even after tool results - we need new tool calls!
        # Phase 7: Check for native JSON tool_calls first, then fall back to YAML parsing
        tool_call = None
        message_obj = (
            resp.get("choices", [{}])[0].get("message", {})
            if isinstance(resp, dict)
            else {}
        )

        # 1. Check for Native JSON Tool Calls (Frontier Models)
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
                            import json

                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if name:
                        tool_call = {"name": name, "arguments": args or {}}
                        logger.info(f"perception_node: native function call: {name}")

        # 2. Fallback to YAML parsing (Local Models)
        if (
            not tool_call
            and content
            and "tool_execution_result" not in content
            and '"tool_execution_result"' not in content
        ):
            tool_call = parse_tool_block(content)
        elif not tool_call:
            logger.info(
                "perception_node: skipping parse_tool_block because content contains tool_execution_result"
            )

        # F8: Prompt injection guard — reject tool calls that are verbatim copies of
        # user-role history messages. A user submitting YAML-tool-looking text could
        # trick the LLM into reflecting it back, causing unintended tool execution.
        # HR-1 fix: require both tool name AND ≥1 argument key to match so that
        # casual mentions of a tool name ("edit_file the config") don't false-positive.
        if tool_call is not None:
            tool_name_extracted = tool_call.get("name", "")
            user_messages = [
                m.get("content", "")
                for m in (state.get("history") or [])
                if m.get("role") == "user"
            ]
            _name_pattern = f"name: {tool_name_extracted}"
            _tool_args = tool_call.get("arguments") or {}
            _arg_keys = list(_tool_args.keys())[:3]
            _inj_detected = False
            for um in user_messages:
                if not um or _name_pattern not in um:
                    continue
                # Name matched — require at least one argument key also present
                # to distinguish YAML injection from a normal mention of the tool.
                if _arg_keys:
                    if any(f"{k}:" in um for k in _arg_keys):
                        _inj_detected = True
                        break
                else:
                    # No-argument tool — only flag if "arguments:" block is also present
                    if "arguments:" in um:
                        _inj_detected = True
                        break
            if _inj_detected:
                logger.warning(
                    f"perception_node: F8 injection guard — tool call '{tool_name_extracted}' "
                    "matches a user-role message (name + args); rejecting to prevent prompt injection"
                )
                tool_call = None

    except Exception:
        tool_call = None
    try:
        logger.info(f"perception_node: parsed tool_call: {repr(tool_call)}")
    except Exception:
        pass

    # ULTIMATE FALLBACK: If tool_call is None and content was supposed to have YAML,
    # treat as empty to trigger loop breaker
    content_stripped = content.strip() if content else ""
    thinking_only = (
        content_stripped.replace("<think>", "").replace("</think>", "").strip() == ""
    )

    # Completion detection: if the model output looks like a task-completion summary
    # (RESULT/STATUS format, or "task is complete" phrasing) rather than a tool call,
    # treat it as terminal — break the loop instead of forcing more tool calls.
    if tool_call is None and content_stripped and not thinking_only:
        _lower = content_stripped.lower()
        _completion_signals = (
            "status: complete",
            "status:done",
            "task is complete",
            "task complete",
            "task is done",
            "task finished",
            "no further action",
            "no further tool",
            "no more action",
            "result:",
            "files_changed:",
        )
        if any(sig in _lower for sig in _completion_signals):
            logger.info(
                "perception_node: detected completion-format text "
                f"(no tool call, content starts with: {content_stripped[:80]!r}) "
                "— breaking loop"
            )
            return {
                "history": [{"role": "assistant", "content": content_stripped}],
                "next_action": None,
                "rounds": (state.get("rounds") or 0) + 1,
                "empty_response_count": 0,
            }

    # Detect truncated YAML: model started a ```yaml block but couldn't complete it.
    # This happens when the context window is full and the response is cut off mid-YAML.
    # The content is non-empty so the thinking_only/empty checks below would miss it,
    # causing an infinite loop. Route through the empty_response_count guard instead.
    _is_truncated_yaml = (
        tool_call is None
        and content_stripped
        and not thinking_only
        and "```yaml" in content_stripped
        and not any(
            sig in content_stripped.lower()
            for sig in ("status: complete", "task is complete", "result:")
        )
    )

    if tool_call is None and (
        not content_stripped or thinking_only or _is_truncated_yaml
    ):
        # No valid tool found: content is empty, thinking-only, or a truncated YAML block.
        empty_response_count = int(state.get("empty_response_count") or 0) + 1
        # Use info instead of warning to avoid log spam
        logger.info(
            f"perception_node: No tool call extracted (count: {empty_response_count})"
        )

        if empty_response_count >= 3:
            logger.error(
                "perception_node: 3 consecutive failed tool extractions - breaking loop"
            )
            return {
                "history": [{"role": "assistant", "content": content or ""}],
                "next_action": None,
                "rounds": state.get("rounds", 0) + 1,
                "last_result": {
                    "ok": False,
                    "error": "Infinite loop detected: model failed to generate valid tool calls 3 times",
                },
                "errors": ["infinite_loop_no_tool"],
                "empty_response_count": 0,
            }

        # Inject corrective prompt — context-window-aware when YAML was truncated
        if _is_truncated_yaml:
            corrective_prompt = (
                "\n\n<system_reminder>\n"
                "Your response was cut off because the context window is full. "
                "Output a MINIMAL YAML tool call — tool name and at most 2 arguments. "
                "Keep your entire response under 50 tokens. No thinking. No preamble.\n"
                "```yaml\nname: tool_name\narguments:\n  key: value\n```\n"
                "</system_reminder>\n"
            )
        else:
            corrective_prompt = (
                "\n\n<system_reminder>\n"
                "CRITICAL: You MUST output a valid YAML tool call block. "
                "Format:\n```yaml\nname: tool_name\narguments:\n  key: value\n```\n"
                "Do NOT output thinking blocks only. Do NOT output empty content.\n"
                "</system_reminder>\n"
            )
        new_messages = [
            {"role": "assistant", "content": content or ""},
            {
                "role": "user",
                "content": corrective_prompt
                + "\n\nProvide a valid YAML tool call now.",
            },
        ]
        return {
            "history": new_messages,
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "empty_response_count": empty_response_count,
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

    result = {
        "history": new_messages,
        "next_action": tool_call,
        "rounds": state.get("rounds", 0) + 1,
        "turn_count": turn_count,
        "empty_response_count": empty_response_count,
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
                _prior_snaps = list(state.get("snapshots") or [])
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
        from src.core.orchestration.graph.builder import _task_is_complex as _tic

        _tc_flag = "complex" if _tic(state) else "simple"
        result["task_complexity"] = _tc_flag
        logger.info("perception_node WF-1: task_complexity=%s", _tc_flag)
    except Exception:
        pass  # Never block on routing helper failure; builder falls back to heuristic

    return result
