"""
Interaction tools for the coding agent.

Provides ask_user (pause and ask a question) and submit_plan_for_review
(HITL plan approval) — two capabilities that require the agent to pause
execution and wait for user input via the EventBus + TUI.
"""

import logging
import threading
from typing import Any, Dict

from src.tools._tool import tool

logger = logging.getLogger(__name__)

_USER_RESPONSE_TIMEOUT = 300  # 5 minutes


@tool(tags=["coding", "planning", "debug", "review"])
def ask_user(question: str) -> Dict[str, Any]:
    """Pause and ask the user a clarifying question.

    Use when requirements are ambiguous, when a destructive action needs
    confirmation, or when multiple valid approaches exist and the user
    should choose. Do NOT use for routine status updates.

    Args:
        question: The question to present to the user.

    Returns:
        status, question, answer (string from user).
    """
    if not question or not question.strip():
        return {"status": "error", "error": "question must be non-empty"}

    reply_event = threading.Event()
    reply_container: Dict[str, str] = {}

    def _on_user_response(payload: dict) -> None:
        reply_container["answer"] = payload.get("answer", "")
        reply_event.set()

    bus = None
    try:
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()
        bus.subscribe("user.response", _on_user_response)
        bus.publish("agent.waiting_for_user", {"question": question})

        replied = reply_event.wait(timeout=_USER_RESPONSE_TIMEOUT)

        if not replied:
            return {
                "status": "timeout",
                "question": question,
                "answer": "",
                "error": f"No user response within {_USER_RESPONSE_TIMEOUT}s",
            }
        return {
            "status": "ok",
            "question": question,
            "answer": reply_container.get("answer", ""),
        }
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
        from src.core.orchestration.event_bus import get_event_bus

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
