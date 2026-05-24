import logging
from pathlib import Path
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
    """Run context compaction via :class:`CompactionService`.

    The ``auto_compact_config_cls``, ``should_compact_fn``, ``compact_messages_fn``,
    ``cfg_get_fn``, and ``get_context_budget_fn`` parameters are retained for
    backward-compatibility with the ``perception_node.py`` wrapper — they are no
    longer used directly; ``CompactionService`` resolves the algorithm internally.
    """
    new_compacted_history = None
    try:
        # --- Cooldown guard (unchanged from previous implementation) ----------
        compaction_last_round = state.get("_compaction_last_round")
        current_rounds = int(state.get("rounds") or 0)
        compaction_min_gap = 3
        compaction_last_round_int = (
            int(compaction_last_round) if compaction_last_round is not None else None
        )
        if compaction_last_round_int is not None:
            gap = current_rounds - compaction_last_round_int
            if gap < compaction_min_gap:
                logger.debug(
                    "perception_node CP-6: skipping compaction — cooldown active "
                    "(last=%d, current=%d, gap=%d < %d)",
                    compaction_last_round_int,
                    current_rounds,
                    gap,
                    compaction_min_gap,
                )
                return history_for_prompt, None

        # --- Determine token limit from adapter / config ---------------------
        context_window = 0
        try:
            if adapter and hasattr(adapter, "context_window"):
                context_window = int(adapter.context_window or 0)
            if not context_window and get_context_budget_fn is not None:
                context_window = get_context_budget_fn()
        except Exception:
            pass

        config_default_max = 10_000
        if cfg_get_fn is not None:
            try:
                config_default_max = int(
                    cfg_get_fn("auto_compact_max_tokens", 10_000) or 10_000
                )
            except Exception:
                pass

        token_limit = (
            int(context_window * 0.85) if context_window > 0 else config_default_max
        )

        # --- Delegate to CompactionService -----------------------------------
        from src.core.memory.compaction_service import CompactionService

        working_dir: Path | None = None
        try:
            wd = getattr(orchestrator, "working_dir", None)
            if wd:
                working_dir = Path(wd)
        except Exception:
            pass

        event_bus = getattr(orchestrator, "event_bus", None)
        service = CompactionService(
            history=history_for_prompt,
            working_dir=working_dir,
            event_bus=event_bus,
            # Always use deterministic path in the hot perception loop so we
            # never block on an LLM call during compaction.
            prefer_deterministic=True,
            compact_threshold=0.85,
        )

        if not service.should_compact(token_limit=token_limit):
            return history_for_prompt, None

        result = service.compact()
        if result.success and result.compacted_history:
            history_for_prompt = result.compacted_history
            new_compacted_history = result.compacted_history
            logger.info(
                "perception_node CP-6: auto-compacted history via CompactionService "
                "(method=%s, tokens %d→%d, new_len=%d)",
                result.method,
                result.tokens_before,
                result.tokens_after,
                len(history_for_prompt),
            )
            # Publish context.auto_compacted for TUI status bar (richer payload
            # than the old context.compacted event).
            try:
                if event_bus:
                    event_bus.publish(
                        "context.auto_compacted",
                        {
                            "method": result.method,
                            "tokens_before": result.tokens_before,
                            "tokens_after": result.tokens_after,
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
