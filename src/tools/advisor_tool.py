"""Opt-in, strictly budgeted strategic advisor tool (TASK-36).

Allows a local 9B model to request a focused strategic or debugging review
from a stronger advisor model (or optional human handoff) without transferring
the full session to cloud inference.

Contract invariants:
- **Dormant by default:** disabled unless ``advisor_model`` is configured
  (via ``AdvisorConfig``, ``/advisor model <name>``, or
  ``CODINGAGENT_ADVISOR_MODEL``) or ``handoff_enabled=True`` (``/advisor handoff on``).
- **Hard session cap:** ``max_uses=3`` per session (enforced before any call).
- **Redacted payloads:** secrets are redacted from ``question`` and
  ``context_summary`` before leaving the session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src.core.security.redaction import redact_secrets

DEFAULT_ADVISOR_MAX_USES = 3


@dataclass
class AdvisorConfig:
    """Configuration for the opt-in ``ask_advisor`` tool."""

    advisor_model: Optional[str] = None
    advisor_provider: Optional[str] = None
    handoff_enabled: bool = False
    max_uses: int = DEFAULT_ADVISOR_MAX_USES

    @classmethod
    def from_env(cls) -> "AdvisorConfig":
        model = (os.getenv("CODINGAGENT_ADVISOR_MODEL") or "").strip() or None
        provider = (os.getenv("CODINGAGENT_ADVISOR_PROVIDER") or "").strip() or None
        handoff = (os.getenv("CODINGAGENT_ADVISOR_HANDOFF") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        raw_max = os.getenv("CODINGAGENT_ADVISOR_MAX_USES")
        try:
            max_uses = max(1, int(raw_max)) if raw_max else DEFAULT_ADVISOR_MAX_USES
        except ValueError:
            max_uses = DEFAULT_ADVISOR_MAX_USES
        return cls(
            advisor_model=model,
            advisor_provider=provider,
            handoff_enabled=handoff,
            max_uses=max_uses,
        )

    @property
    def is_enabled(self) -> bool:
        return bool(self.advisor_model or self.handoff_enabled)


@dataclass
class AdvisorSessionState:
    """Per-session advisor configuration and call budget tracker."""

    config: AdvisorConfig = field(default_factory=AdvisorConfig.from_env)
    uses: int = 0
    history: list[Dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, int(self.config.max_uses) - int(self.uses))

    def reset_budget(self) -> None:
        self.uses = 0
        self.history.clear()

    def status_summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.is_enabled,
            "advisor_model": self.config.advisor_model,
            "advisor_provider": self.config.advisor_provider,
            "handoff_enabled": self.config.handoff_enabled,
            "uses": self.uses,
            "max_uses": self.config.max_uses,
            "remaining": self.remaining,
        }


AdvisorCallFn = Callable[[str, str, Optional[str], Optional[str]], str]
HandoffFn = Callable[[str, str], str]


def _default_advisor_model_call(
    question: str,
    context_summary: str,
    advisor_model: Optional[str],
    advisor_provider: Optional[str],
) -> str:
    """Invoke the configured advisor model for a single tool-free review turn."""
    from src.core.inference.llm_manager import call_model

    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise senior engineering advisor. Provide direct, "
                "actionable debugging or architectural guidance based on the "
                "summary below. Do not invent file contents not shown."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Context summary:\n{context_summary}\n\n"
                f"Question:\n{question}"
            ),
        },
    ]
    resp = call_model(
        messages,
        provider=advisor_provider,
        model=advisor_model,
        tools=[],
        stream=False,
    )
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict) and msg.get("content"):
                return str(msg["content"])
        if resp.get("content"):
            return str(resp["content"])
    return str(resp or "")


def ask_advisor(
    question: str,
    context_summary: str = "",
    *,
    state: Optional[AdvisorSessionState] = None,
    call_fn: Optional[AdvisorCallFn] = None,
    handoff_fn: Optional[HandoffFn] = None,
) -> Dict[str, Any]:
    """Request focused strategic/debugging advice under strict opt-in budget limits."""
    sess = state if state is not None else AdvisorSessionState()
    cfg = sess.config

    if not cfg.is_enabled:
        return {
            "status": "disabled",
            "error": (
                "Advisor is disabled by default. Enable it with "
                "'/advisor model <name>' or '/advisor handoff on' "
                "(or set CODINGAGENT_ADVISOR_MODEL)."
            ),
            "uses": sess.uses,
            "max_uses": cfg.max_uses,
        }

    if sess.uses >= cfg.max_uses:
        return {
            "status": "budget_exhausted",
            "error": (
                f"Advisor session budget exhausted ({sess.uses}/{cfg.max_uses} uses). "
                "Proceed with your own analysis or ask the user directly."
            ),
            "uses": sess.uses,
            "max_uses": cfg.max_uses,
        }

    clean_q = redact_secrets(str(question or "").strip())
    clean_ctx = redact_secrets(str(context_summary or "").strip())
    if not clean_q:
        return {
            "status": "error",
            "error": "question must be a non-empty string.",
            "uses": sess.uses,
            "max_uses": cfg.max_uses,
        }

    sess.uses += 1

    # Human handoff takes precedence when explicitly enabled without an advisor model
    if cfg.handoff_enabled and not cfg.advisor_model:
        if handoff_fn is not None:
            advice = str(handoff_fn(clean_q, clean_ctx))
            sess.history.append({"mode": "handoff", "question": clean_q, "advice": advice})
            return {
                "status": "ok",
                "mode": "handoff",
                "advice": advice,
                "uses": sess.uses,
                "max_uses": cfg.max_uses,
                "remaining": sess.remaining,
            }
        sess.history.append({"mode": "handoff_required", "question": clean_q})
        return {
            "status": "handoff_required",
            "mode": "handoff",
            "question": clean_q,
            "context_summary": clean_ctx,
            "uses": sess.uses,
            "max_uses": cfg.max_uses,
            "remaining": sess.remaining,
        }

    fn = call_fn or _default_advisor_model_call
    try:
        advice = str(
            fn(clean_q, clean_ctx, cfg.advisor_model, cfg.advisor_provider)
        ).strip()
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Advisor model call failed: {redact_secrets(str(exc))}",
            "uses": sess.uses,
            "max_uses": cfg.max_uses,
            "remaining": sess.remaining,
        }

    sess.history.append(
        {"mode": "model", "model": cfg.advisor_model, "question": clean_q, "advice": advice}
    )
    return {
        "status": "ok",
        "mode": "model",
        "advisor_model": cfg.advisor_model,
        "advice": advice,
        "uses": sess.uses,
        "max_uses": cfg.max_uses,
        "remaining": sess.remaining,
    }


__all__ = [
    "DEFAULT_ADVISOR_MAX_USES",
    "AdvisorConfig",
    "AdvisorSessionState",
    "ask_advisor",
]
