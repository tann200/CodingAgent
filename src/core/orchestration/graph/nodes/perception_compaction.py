import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _bootstrap_history_for_prompt(state: Mapping[str, Any]) -> list:
    """Start from compacted history when present, then append unseen raw turns."""
    prior_compacted = state.get("_compacted_history")
    if prior_compacted and isinstance(prior_compacted, list):
        history_for_prompt = list(prior_compacted)
        compacted_contents = {
            msg.get("content", "") for msg in prior_compacted if isinstance(msg, dict)
        }
        raw_history = list(state.get("history") or [])
        for message in raw_history:
            if (
                isinstance(message, dict)
                and message.get("content", "") not in compacted_contents
            ):
                history_for_prompt.append(message)
        return history_for_prompt
    return list(state.get("history") or [])


def _run_auto_compaction(
    history_for_prompt: list,
    adapter: Any,
    orchestrator: Any,
    state: Mapping[str, Any],
    *,
    auto_compact_config_cls: Any,
    should_compact_fn: Any,
    compact_messages_fn: Any,
    cfg_get_fn: Any,
    get_context_budget_fn: Any,
) -> tuple[list, list | None]:
    """Run the deterministic auto-compaction logic."""
    new_compacted_history = None
    try:
        if (
            auto_compact_config_cls is None
            or should_compact_fn is None
            or compact_messages_fn is None
            or cfg_get_fn is None
        ):
            raise RuntimeError("auto_compactor unavailable")

        context_window = 0
        try:
            if adapter and hasattr(adapter, "context_window"):
                context_window = int(adapter.context_window or 0)
            if not context_window and get_context_budget_fn is not None:
                context_window = get_context_budget_fn()
        except Exception:
            pass

        config_default_max = int(cfg_get_fn("auto_compact_max_tokens", 10_000) or 10_000)
        auto_compact_max_tokens = (
            int(context_window * 0.85) if context_window > 0 else config_default_max
        )
        auto_compact_preserve = int(cfg_get_fn("auto_compact_preserve_recent", 4) or 4)
        auto_compact_config = auto_compact_config_cls(
            preserve_recent=auto_compact_preserve,
            max_tokens=auto_compact_max_tokens,
        )

        compaction_last_round = state.get("_compaction_last_round")
        current_rounds = int(state.get("rounds") or 0)
        compaction_min_gap = 3
        compaction_last_round_int = (
            int(compaction_last_round) if compaction_last_round is not None else None
        )
        gap: int | None = None
        if compaction_last_round_int is not None:
            gap = current_rounds - compaction_last_round_int
            compaction_on_cooldown = gap < compaction_min_gap
        else:
            compaction_on_cooldown = False

        if compaction_on_cooldown:
            logger.debug(
                f"perception_node CP-6: skipping compaction — cooldown active (last={compaction_last_round_int}, current={current_rounds}, gap={gap} < {compaction_min_gap})"
            )
        elif should_compact_fn(history_for_prompt, auto_compact_config):
            compact_result = compact_messages_fn(history_for_prompt, auto_compact_config)
            if compact_result.removed_message_count > 0:
                history_for_prompt = compact_result.compacted_messages
                new_compacted_history = compact_result.compacted_messages
                logger.info(
                    "perception_node CP-6: auto-compacted history — removed=%d, new_len=%d",
                    compact_result.removed_message_count,
                    len(history_for_prompt),
                )
                try:
                    if orchestrator and hasattr(orchestrator, "event_bus"):
                        orchestrator.event_bus.publish(
                            "context.auto_compacted",
                            {
                                "removed_message_count": compact_result.removed_message_count,
                                "new_message_count": len(history_for_prompt),
                                "session_id": state.get("session_id"),
                            },
                        )
                except Exception:
                    pass
    except Exception as auto_compaction_error:
        logger.debug(
            "perception_node CP-6: auto-compaction skipped (non-fatal): %s",
            auto_compaction_error,
        )

    return history_for_prompt, new_compacted_history
