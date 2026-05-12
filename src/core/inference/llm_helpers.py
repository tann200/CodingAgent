import asyncio
import logging
import os
from typing import Any, Mapping

from src.core.inference.llm_manager import call_model

logger = logging.getLogger(__name__)

# G12: opt-in token streaming via env var.
# When true, perception_node and inference_loop fallback pass stream=True
# so _consume_sse_stream publishes llm.token events per chunk.
_STREAMING_ENABLED: bool = os.getenv("CODING_AGENT_STREAM_TOKENS", "").lower() in ("1", "true", "yes")


async def _await_llm_task(
    llm_task: "asyncio.Task",
    cancel_event: Any | None,
    state: Mapping[str, Any],
    perc_deadline: float | None,
    perc_timeout: int | None,
) -> tuple[bool, Any]:
    try:
        while not llm_task.done():
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                llm_task.cancel()
                logger.info("llm_helpers: Task canceled mid-generation")
                return True, {
                    "history": state.get("history", []),
                    "next_action": None,
                    "rounds": state.get("rounds", 0) + 1,
                    "errors": ["canceled"],
                }
            if (
                perc_deadline is not None
                and asyncio.get_running_loop().time() >= perc_deadline
            ):
                llm_task.cancel()
                logger.warning(f"llm_helpers: LLM call timed out after {perc_timeout}s")
                return True, {
                    "history": state.get("history", []),
                    "next_action": "wait_for_user",
                    "rounds": state.get("rounds", 0) + 1,
                    "errors": [f"llm_timeout:{perc_timeout}s"],
                }
            await asyncio.wait([llm_task], timeout=0.2)
        resp = await llm_task
        return False, resp
    except asyncio.CancelledError:
        logger.info("llm_helpers: Task cancelled")
        return True, {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "errors": ["canceled"],
        }
    except Exception as e:
        logger.error(f"call_model failed: {e}")
        return False, {"ok": False, "error": str(e)}


async def call_model_with_timeout(
    messages: list,
    provider: Any,
    model: Any,
    state: Mapping[str, Any],
    orchestrator: Any,
    llm_kwargs: dict,
    tools: list[Any] | None = None,
    call_model_fn: Any | None = None,
) -> tuple[dict | None, Any]:
    """Call the shared call_model helper with configurable timeout and cancel handling.

    Returns (early_result_or_None, resp_or_error)
    """
    try:
        # Allow injection of a custom call_model function (useful for tests)
        _call = call_model_fn or call_model
        llm_task = asyncio.create_task(
            _call(
                messages,
                provider=provider,
                model=model,
                stream=_STREAMING_ENABLED,
                format_json=False,
                tools=tools,
                session_id=state.get("session_id"),
                **llm_kwargs,
            )
        )

        # configurable timeout
        _perc_llm_timeout: int | None = 120
        try:
            from src.core.orchestration.project_settings import (
                get_active_settings as _gas_perc,
            )

            _ps_perc = _gas_perc()
            if _ps_perc is not None:
                _perc_llm_timeout = _ps_perc.max_llm_wait_seconds or None
        except Exception:
            pass
        _perc_deadline = (
            asyncio.get_running_loop().time() + _perc_llm_timeout
            if _perc_llm_timeout
            else None
        )

        cancel_event = state.get("cancel_event")
        if not cancel_event and orchestrator:
            cancel_event = getattr(orchestrator, "cancel_event", None)

        early_exit, value = await _await_llm_task(
            llm_task, cancel_event, state, _perc_deadline, _perc_llm_timeout
        )
        if early_exit:
            return value, None
        logger.info(
            "call_model_with_timeout: raw value type=%s finish_reason=%s tool_calls_len=%s content_len=%s",
            type(value),
            value.get("finish_reason") if isinstance(value, dict) else "n/a",
            len(value.get("tool_calls") or []) if isinstance(value, dict) else "n/a",
            len(value.get("content", "") or "") if isinstance(value, dict) else "n/a",
        )
        return None, value
    except asyncio.CancelledError:
        logger.info("llm_helpers: Task cancelled")
        return (
            {
                "history": state.get("history", []),
                "next_action": None,
                "rounds": state.get("rounds", 0) + 1,
                "errors": ["canceled"],
            },
            None,
        )
    except Exception as e:
        logger.error(f"call_model failed: {e}")
        return None, {"ok": False, "error": str(e)}
