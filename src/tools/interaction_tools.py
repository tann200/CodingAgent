"""
Interaction tools for the coding agent.

Provides:
- ``ask_user``            — pause and ask a clarifying question
- ``submit_plan_for_review`` — HITL plan approval
- ``send_user_message``   — CP-15: proactive mid-turn message to the user
  (port of claw-code's ``SendUserMessage`` / ``Brief`` tool)
"""

import logging
import threading
from typing import Any, Dict, List, Literal, Optional

from src.tools._tool import tool
from src.core.orchestration.event_bus import get_event_bus as _get_event_bus

logger = logging.getLogger(__name__)

_USER_RESPONSE_TIMEOUT = 300  # 5 minutes


@tool(tags=["coding", "planning", "debug", "review"])
def ask_user(question: str, choices: Optional[List[str]] = None) -> Dict[str, Any]:
    """Pause and ask the user a clarifying question.

    Use when requirements are ambiguous, when a destructive action needs
    confirmation, or when multiple valid approaches exist and the user
    should choose. Do NOT use for routine status updates.

    Args:
        question: The question to present to the user.
        choices: Optional list of choice strings for multiple-choice questions.
                 When provided, the TUI displays them as options and validates
                 the response against the list (case-insensitive).

    Returns:
        status, question, answer (string from user), choice_index (int, if choices given).
    """
    if not question or not question.strip():
        return {"status": "error", "error": "question must be non-empty"}

    if choices is not None:
        if not isinstance(choices, list) or len(choices) < 2:
            return {
                "status": "error",
                "error": "choices must be a list with at least 2 items",
            }
        # Append options to the displayed question so TUI/fallback shows them
        options_str = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(choices))
        display_question = f"{question}\n\nOptions:\n{options_str}"
    else:
        display_question = question

    reply_event = threading.Event()
    reply_container: Dict[str, str] = {}

    def _on_user_response(payload: dict) -> None:
        reply_container["answer"] = payload.get("answer", "")
        reply_event.set()

    bus = None
    try:

        bus = get_event_bus()
        bus.subscribe("user.response", _on_user_response)
        bus.publish(
            "agent.waiting_for_user",
            {
                "question": display_question,
                "choices": choices or [],
            },
        )

        replied = reply_event.wait(timeout=_USER_RESPONSE_TIMEOUT)

        if not replied:
            return {
                "status": "timeout",
                "question": question,
                "answer": "",
                "error": f"No user response within {_USER_RESPONSE_TIMEOUT}s",
            }

        answer = reply_container.get("answer", "")
        result: Dict[str, Any] = {
            "status": "ok",
            "question": question,
            "answer": answer,
        }

        if choices:
            # Try numeric index first, then case-insensitive match
            choice_index = None
            try:
                idx = int(answer.strip()) - 1
                if 0 <= idx < len(choices):
                    choice_index = idx
                    answer = choices[idx]
            except (ValueError, TypeError):
                for i, c in enumerate(choices):
                    if c.strip().lower() == answer.strip().lower():
                        choice_index = i
                        break
            result["answer"] = answer
            result["choice_index"] = choice_index

        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        if bus is not None:
            try:
                bus.unsubscribe("user.response", _on_user_response)
            except Exception:
                pass


@tool(tags=["planning"])
def submit_plan_for_review(
    plan_summary: str,
    plan_steps: list,
    risk_level: str = "medium",
) -> Dict[str, Any]:
    """Submit the current plan for user review before execution begins.

    Use before any sequence of write operations, before running commands
    that cannot be undone, or when the plan involves more than 3 files.
    Blocks until the user approves, requests changes, or rejects.

    Args:
        plan_summary: One-paragraph summary of what the plan does.
        plan_steps: List of step descriptions.
        risk_level: "low", "medium", or "high".

    Returns:
        status, decision ("approved"/"rejected"/"revised"), feedback.
    """
    if not plan_steps:
        return {"status": "error", "error": "plan_steps must be non-empty"}

    reply_event = threading.Event()
    reply_container: Dict[str, str] = {}

    def _on_plan_response(payload: dict) -> None:
        reply_container["decision"] = payload.get("decision", "")
        reply_container["feedback"] = payload.get("feedback", "")
        reply_event.set()

    bus = None
    try:

        bus = get_event_bus()
        bus.subscribe("plan_review.response", _on_plan_response)
        bus.publish(
            "agent.plan_review_requested",
            {
                "plan_summary": plan_summary,
                "plan_steps": plan_steps,
                "risk_level": risk_level,
            },
        )

        replied = reply_event.wait(timeout=_USER_RESPONSE_TIMEOUT)

        if not replied:
            return {
                "status": "timeout",
                "decision": "timeout",
                "error": f"No user response within {_USER_RESPONSE_TIMEOUT}s",
            }

        decision = reply_container.get("decision", "")
        return {
            "status": "ok",
            "decision": decision,
            "feedback": reply_container.get("feedback", ""),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        if bus is not None:
            try:
                bus.unsubscribe("plan_review.response", _on_plan_response)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CP-15: send_user_message — proactive mid-turn message tool
# ---------------------------------------------------------------------------


@tool(tags=["communication"])
def send_user_message(
    message: str,
    attachments: Optional[List[str]] = None,
    status: Literal["normal", "proactive"] = "normal",
) -> Dict[str, Any]:
    """Send an interim message to the user without ending the current turn.

    Port of claw-code's ``SendUserMessage`` (alias ``Brief``) tool.  Use this
    to deliver a progress update, a partial finding, or any proactive
    notification mid-turn so the user sees it immediately.

    Unlike ``ask_user``, this tool does **not** block waiting for a reply — it
    fires-and-forgets the message and returns immediately.

    Args:
        message: The text to send to the user.
        attachments: Optional list of file paths or short text snippets to
            attach to the message (forwarded to the UI as-is).
        status: ``"normal"`` for a direct response; ``"proactive"`` for an
            unsolicited update.  Consumers (TUI, bridge) may render the two
            styles differently.

    Returns:
        ok, delivered, message.
    """
    if not message or not message.strip():
        return {"status": "error", "error": "message must be non-empty"}

    payload: Dict[str, Any] = {
        "message": message.strip(),
        "attachments": list(attachments or []),
        "status": status,
    }

    try:

        bus = get_event_bus()
        bus.publish("agent.message", payload)
    except Exception as exc:
        logger.debug("send_user_message: event bus unavailable: %s", exc)
        # Degrade gracefully — still return success so the LLM can continue

    return {
        "status": "ok",
        "ok": True,
        "delivered": True,
        "message": message.strip(),
    }
