from src.core.messaging.event_types import ContextOverflow
import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _process_post_call_tokens(
    resp: Any,
    state: Mapping[str, Any],
    orchestrator: Any,
    adapter: Any,
    *,
    estimate_cost_usd: Any,
) -> tuple[dict | None, dict, float]:
    """Process post-LLM token usage and context-overflow handling."""
    overflow_compaction: dict = {}
    session_cost_delta = 0.0

    if isinstance(resp, dict) and resp.get("context_overflow"):
        logger.warning(
            "perception_node: context overflow error from provider — triggering reactive compaction"
        )
        overflow_compaction = {"_budget_compaction": True, "_should_distill": True}
        try:
            if orchestrator and hasattr(orchestrator, "event_bus"):
                orchestrator.event_bus.publish_typed(ContextOverflow(prompt_tokens=0, budget=0, reserved=0, session_id=state.get("session_id"), source="api_error"))
        except Exception:
            pass

        overflow_history_keep = 6
        raw_history = list(state.get("history") or [])
        truncated = (
            raw_history[-overflow_history_keep:]
            if len(raw_history) > overflow_history_keep
            else raw_history
        )
        logger.warning(
            "perception_node: context overflow early-exit — "
            f"truncating history {len(raw_history)} → {len(truncated)} messages; "
            "errors=['context_overflow'] will route to memory_sync"
        )
        return (
            {
                "history": [],
                "_compacted_history": truncated,
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
            overflow_compaction,
            session_cost_delta,
        )

    if isinstance(resp, dict):
        resp_prompt_tokens = int(resp.get("prompt_tokens") or 0)
        resp_completion_tokens = int(resp.get("completion_tokens") or 0)
        resp_total_tokens = int(
            resp.get("total_tokens") or resp_prompt_tokens + resp_completion_tokens
        )

        has_usage = (resp_prompt_tokens + resp_completion_tokens) > 0
        if has_usage and orchestrator:
            try:
                token_monitor = getattr(orchestrator, "token_monitor", None)
                if token_monitor:
                    token_monitor.record_usage(
                        session_id=state.get("session_id", "default"),
                        prompt_tokens=resp_prompt_tokens,
                        completion_tokens=resp_completion_tokens,
                        total_tokens=resp_total_tokens,
                    )
                    try:
                        if estimate_cost_usd is not None:
                            active_model = resp.get("model") or (
                                adapter.default_model
                                if adapter and hasattr(adapter, "default_model")
                                else ""
                            )
                            session_cost_delta = estimate_cost_usd(
                                resp_prompt_tokens,
                                resp_completion_tokens,
                                active_model or "",
                            )
                    except Exception:
                        pass
            except Exception as error:
                logger.debug(f"Token tracking error: {error}")

        try:
            from src.core.inference.provider_context import get_actual_context_window

            prompt_tokens = resp_prompt_tokens
            reserved_output_buffer = 4096
            budget = get_actual_context_window()
            available = budget - reserved_output_buffer
            if prompt_tokens > 0 and prompt_tokens >= available:
                logger.warning(
                    f"perception_node: context overflow detected — prompt_tokens={prompt_tokens} >= available={available} "
                    f"(budget={budget}, reserved={reserved_output_buffer}); triggering compaction"
                )
                overflow_compaction = {
                    "_budget_compaction": True,
                    "_should_distill": True,
                }
                try:
                    if orchestrator and hasattr(orchestrator, "event_bus"):
                        orchestrator.event_bus.publish_typed(ContextOverflow(prompt_tokens=prompt_tokens, budget=budget, reserved=reserved_output_buffer, session_id=state.get("session_id")))
                except Exception:
                    pass
        except Exception as overflow_error:
            logger.debug(
                f"context overflow check error (non-fatal): {overflow_error}"
            )

    return None, overflow_compaction, session_cost_delta
