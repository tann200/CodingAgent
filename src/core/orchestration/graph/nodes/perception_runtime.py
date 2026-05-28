import logging
from typing import Any, Mapping, Optional


def _resolve_orchestrator_and_cancellation(
    *,
    state: Mapping[str, Any],
    config: Any,
    resolve_orchestrator_fn: Any,
    logger: logging.Logger,
) -> tuple[Any, Optional[dict[str, Any]]]:
    """Resolve perception orchestrator and return standard early errors/cancel payloads."""
    orchestrator = resolve_orchestrator_fn(state, config)
    if orchestrator is None:
        logger.error("perception_node: orchestrator is None in config")
        return None, {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": ["orchestrator not found in config"],
        }

    cancel_event = state.get("cancel_event")
    if not cancel_event:
        cancel_event = getattr(orchestrator, "cancel_event", None)
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("perception_node: Task canceled by user")
        return orchestrator, {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
            "empty_response_count": 0,
        }

    return orchestrator, None


def _maybe_handle_turn_limit(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    turn_count: int,
    max_turns: int,
    logger: logging.Logger,
) -> Optional[dict[str, Any]]:
    """Return the standard turn-limit payload when perception exceeds max_turns."""
    if turn_count <= max_turns:
        return None

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


def _validate_call_model_and_adapter(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    call_model_fn: Any,
    logger: logging.Logger,
) -> tuple[Any, Optional[dict[str, Any]]]:
    """Validate perception runtime dependencies before prompt construction."""
    base_payload: dict[str, Any] = {
        "history": [],
        "next_action": None,
        "rounds": (state.get("rounds") or 0) + 1,
    }

    if not callable(call_model_fn):
        logger.error("perception_node: call_model is not callable: %s", call_model_fn)
        return None, {**base_payload, "errors": ["call_model not available"]}

    try:
        adapter = orchestrator.adapter
    except Exception as exc:
        logger.error("perception_node: failed to get adapter: %s", exc)
        return None, {**base_payload, "errors": [f"adapter error: {exc}"]}

    if adapter is None:
        logger.warning("perception_node: orchestrator.adapter is None")
        return None, {**base_payload, "errors": ["adapter is None"]}

    return adapter, None


def _filter_tools_near_turn_limit(
    *,
    tools_list: list[dict[str, Any]],
    turn_count: int,
    max_turns: int,
    modifying_tools: set[str] | list[str] | tuple[str, ...],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Remove modifying tools from the prompt when nearing the turn limit."""
    near_limit = int(turn_count) >= (int(max_turns) - 2)
    if not near_limit:
        return tools_list

    try:
        if modifying_tools:
            tools_list = [
                tool for tool in tools_list if tool.get("name") not in modifying_tools
            ]
        logger.info(
            "perception_node: near turn limit (%d/%d) — write tools removed from prompt",
            int(turn_count),
            int(max_turns),
        )
    except Exception:
        pass
    return tools_list


def _compute_active_skills_for_task(
    *,
    task: str,
    logger: logging.Logger,
    debug_keywords: tuple[str, ...] = (
        "debug",
        "fix",
        "error",
        "bug",
        "search",
        "find",
        "analyze",
    ),
) -> list[str]:
    """Return dynamically injected skills for the current task."""
    active_skills: list[str] = []
    task_lower = task.lower()
    if any(keyword in task_lower for keyword in debug_keywords):
        active_skills.append("context_hygiene")
        logger.info(
            "perception_node: injected context_hygiene skill for debugging/searching task"
        )
    return active_skills


def _select_perception_role(state: Mapping[str, Any], orchestrator: Any) -> str:
    """Map agent_mode to the canonical toolset role name.

    All five toolset roles are reachable:
      planning  → strategic   (planner toolset)
      analyzing → analyst     (analysis toolset)
      reviewing → reviewer    (review toolset)
      debugging → debugger    (debug toolset)
      *         → operational (coding toolset, default)
    """
    agent_mode = (
        state.get("agent_mode")
        or getattr(orchestrator, "_agent_mode", None)
        or "execution"
    )
    _MODE_TO_ROLE = {
        "planning": "strategic",
        "plan": "strategic",
        "analyzing": "analyst",
        "analysis": "analyst",
        "reviewing": "reviewer",
        "review": "reviewer",
        "debugging": "debugger",
        "debug": "debugger",
    }
    return _MODE_TO_ROLE.get(agent_mode, "operational")


def _resolve_perception_provider_context(
    *,
    orchestrator: Any,
    adapter: Any,
    resolve_provider_caps_fn: Any,
    resolve_active_model_name_fn: Any,
    classify_model_tier_fn: Any,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Resolve provider/model metadata used by perception prompt and warnings."""
    try:
        provider_capabilities = resolve_provider_caps_fn(orchestrator, adapter)
    except Exception:
        provider_capabilities = {}

    active_model_name = resolve_active_model_name_fn(provider_capabilities, orchestrator)

    try:
        caps = resolve_provider_caps_fn(orchestrator, adapter)
    except Exception:
        caps = {}

    provider = caps.get("provider_name")
    model = caps.get("model")
    model_tier_str = classify_model_tier_fn(model, adapter, logger)

    return {
        "provider_capabilities": provider_capabilities,
        "active_model_name": active_model_name,
        "provider": provider,
        "model": model,
        "model_tier_str": model_tier_str,
    }


def _resolve_active_model_name(
    provider_capabilities: Mapping[str, Any] | None,
    orchestrator: Any,
    *,
    extract_str: Any,
) -> str:
    """Resolve the active model name used for perception prompt selection."""
    active_model_name = ""
    try:
        if provider_capabilities and provider_capabilities.get("model"):
            active_model_name = provider_capabilities.get("model") or ""
        elif orchestrator and getattr(orchestrator, "adapter", None):
            models = getattr(orchestrator.adapter, "models", None)
            if models:
                if isinstance(models, (list, tuple)):
                    for model in models:
                        model_name = extract_str(model)
                        if model_name:
                            active_model_name = model_name
                            break
                else:
                    model_name = extract_str(models)
                    if model_name:
                        active_model_name = model_name

            if not active_model_name and hasattr(orchestrator.adapter, "default_model"):
                default_model = extract_str(
                    getattr(orchestrator.adapter, "default_model", None)
                )
                if default_model:
                    active_model_name = default_model
    except Exception:
        pass
    return active_model_name


def _build_llm_kwargs(orchestrator: Any, logger: logging.Logger) -> dict:
    """Build call-model kwargs, including deterministic and thinking-mode settings."""
    llm_kwargs: dict[str, Any] = {}
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

    try:
        if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
            caps = orchestrator.get_provider_capabilities()
            if isinstance(caps, dict):
                model_name = caps.get("model") or caps.get("default_model") or ""
                if model_name:
                    from src.core.inference.thinking_utils import (
                        budget_max_tokens,
                        is_reasoning_model,
                        supports_no_think,
                    )

                    if is_reasoning_model(model_name):
                        current_max = llm_kwargs.get("max_tokens", 0)
                        if current_max > 0:
                            adjusted_max = budget_max_tokens(current_max, model_name)
                            llm_kwargs["max_tokens"] = adjusted_max
                            logger.info(
                                "[THINKING_MODE] Increased max_tokens for reasoning model %s: %s -> %s",
                                model_name,
                                current_max,
                                adjusted_max,
                            )

                        if supports_no_think(model_name):
                            llm_kwargs["think"] = False
                            logger.info(
                                "[THINKING_MODE] Disabled thinking for model %s",
                                model_name,
                            )
                    elif supports_no_think(model_name):
                        llm_kwargs["think"] = False
                        logger.info(
                            "[THINKING_MODE] Disabled thinking for non-reasoning model %s",
                            model_name,
                        )
    except Exception as exc:
        logger.debug("[THINKING_MODE] Error in thinking mode handling: %s", exc)

    return llm_kwargs


def _maybe_warn_small_context_window(
    *,
    state: Mapping[str, Any],
    orchestrator: Any,
    adapter: Any,
    model: str | None,
    model_tier_str: str | None,
    logger: logging.Logger,
) -> None:
    """Emit the GAP-10 warning when a small/frontier model has a tiny context window."""
    rounds_now = state.get("rounds") or 0
    if rounds_now != 0 or model_tier_str not in ("small", "frontier"):
        return

    try:
        ctx_win = 0
        if adapter and hasattr(adapter, "context_window"):
            ctx_win = int(adapter.context_window or 0)
        if not (0 < ctx_win < 16384):
            return

        warn_msg = (
            f"Context window is very small ({ctx_win:,} tokens) for "
            f"{model}. "
            "The model supports up to 128K-256K tokens. "
            "Increase n_ctx in LM Studio to at least 32768 for better "
            "agentic performance."
        )
        logger.warning("GAP-10 context-window warning: %s", warn_msg)
        try:
            if orchestrator and hasattr(orchestrator, "event_bus"):
                orchestrator.event_bus.publish(
                    "ui.notification",
                    {
                        "level": "warning",
                        "message": warn_msg,
                        "source": "context_window_check",
                    },
                )
        except Exception:
            pass
    except Exception:
        pass
