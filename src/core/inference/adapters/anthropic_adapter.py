"""Anthropic adapter.

Extends OpenAICompatibleAdapter for the native Anthropic Messages API
(https://api.anthropic.com/v1).

Key differences from vanilla OpenAI-compat adapters:

CP-12 — prompt-cache boundary wiring
    The ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY`` sentinel inserted by
    ``ContextBuilder`` is used here to split the system prompt into two
    content blocks.  The static (pre-sentinel) block receives
    ``cache_control: {"type": "ephemeral"}`` so Anthropic caches it across
    turns.  The dynamic (post-sentinel) block is left undecorated so it is
    re-evaluated every turn.

    This overrides ``OpenAICompatibleAdapter._preprocess_messages()`` which
    only strips the sentinel without setting cache_control.

Authentication:
    API key loaded from:
      1. Constructor argument
      2. ``UserPrefs`` (~/.config/codingagent/prefs.json, key "anthropic")
      3. ``ANTHROPIC_API_KEY`` environment variable

ProviderManager convention:
    type = "anthropic" in providers.json → imports this module →
    _camelize("anthropic") = "Anthropic" → looks for "AnthropicAdapter",
    then falls back to module-level "Adapter".  Both aliases are provided.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)

_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"

# Minimum token count for a static block to be worth caching.
# Anthropic requires ≥ 1024 tokens for cache_control to take effect; we use
# a conservative word-count heuristic (≈ 4 chars/token) rather than a real
# tokeniser so we don't introduce a heavy dependency.
_MIN_STATIC_CHARS_FOR_CACHE = 512  # ≈ 128 tokens — small but safe floor


class AnthropicAdapter(OpenAICompatibleAdapter):
    """Adapter for the native Anthropic Messages API.

    Inherits all HTTP/retry/normalisation logic from OpenAICompatibleAdapter
    and only overrides the parts that differ for Anthropic:
      - Fixed base URL
      - API-key header format (``x-api-key`` + ``anthropic-version``)
      - CP-12: ``_preprocess_messages()`` — splits on the dynamic boundary
        sentinel and sets ``cache_control`` on the static block
    """

    BASE_URL = _ANTHROPIC_BASE_URL
    REQUIRES_API_KEY = True

    # Default to claude-3-5-sonnet (broadly available, good cache hit rate)
    DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

    def __init__(
        self,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "anthropic",
        # Accept (and silently override) generic args passed by ProviderManager
        base_url: Optional[str] = None,  # always overridden by BASE_URL
        config_path: Optional[str] = None,
        **kwargs,
    ):
        # Resolve API key: constructor > UserPrefs > env var
        if not api_key:
            try:
                from src.core.user_prefs import UserPrefs  # type: ignore[import]

                api_key = UserPrefs.load().get_provider_key("anthropic")
            except Exception:
                pass
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY") or None

        resolved_models: List[str] = list(models) if models else [self.DEFAULT_MODEL]
        default_model = resolved_models[0]

        super().__init__(
            base_url=self.BASE_URL,
            api_key=api_key,
            default_model=default_model,
            models=resolved_models,
            name=name,
            **{k: v for k, v in kwargs.items() if k not in ("model",)},
        )

    # ------------------------------------------------------------------
    # Anthropic-specific HTTP headers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """Return headers required by the Anthropic Messages API.

        Uses ``x-api-key`` (not ``Authorization: Bearer``) and must include
        ``anthropic-version``.  The ``anthropic-beta: prompt-caching-2024-07-31``
        header enables prompt caching so our ``cache_control`` blocks are
        honoured.
        """
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    # ------------------------------------------------------------------
    # CP-12: Prompt-cache boundary preprocessing
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess_messages(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Split system prompt on the dynamic-boundary sentinel and wire cache_control.

        The Anthropic Messages API accepts ``system`` as an array of content
        blocks rather than a plain string.  This method converts any system
        message whose content contains ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY`` into
        two blocks:

        1. Static block  — everything before the sentinel.
           ``cache_control: {"type": "ephemeral"}`` is attached so Anthropic
           caches this block across turns (prompt caching).
        2. Dynamic block — everything after the sentinel.
           No ``cache_control`` so it is re-evaluated every turn.

        When the sentinel is absent the system message is left as a plain
        string (OpenAI-compat wire format still works for Anthropic).
        """
        try:
            from src.core.context.context_builder import (  # type: ignore[import]
                SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
            )
        except Exception:
            return messages

        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system" and isinstance(msg.get("content"), str):
                content: str = msg["content"]
                if SYSTEM_PROMPT_DYNAMIC_BOUNDARY in content:
                    # Split into static (before sentinel) and dynamic (after sentinel).
                    # The sentinel is surrounded by "\n\n" joins in the builder.
                    parts = content.split(SYSTEM_PROMPT_DYNAMIC_BOUNDARY, maxsplit=1)
                    static_text = parts[0].strip()
                    dynamic_text = parts[1].strip() if len(parts) > 1 else ""

                    blocks: List[Dict[str, Any]] = []

                    if static_text:
                        static_block: Dict[str, Any] = {
                            "type": "text",
                            "text": static_text,
                        }
                        # Only attach cache_control when the static block is
                        # large enough for Anthropic's caching to be effective.
                        if len(static_text) >= _MIN_STATIC_CHARS_FOR_CACHE:
                            static_block["cache_control"] = {"type": "ephemeral"}
                        blocks.append(static_block)

                    if dynamic_text:
                        blocks.append({"type": "text", "text": dynamic_text})

                    if blocks:
                        out.append({**msg, "content": blocks})
                        continue

                    # Degenerate case: sentinel present but no content —
                    # fall through to the plain-string path below.

            out.append(msg)
        return out

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    def _models_endpoints(self) -> List[str]:
        return [_ANTHROPIC_MODELS_URL]

    def get_models_from_api(self) -> Dict[str, Any]:
        """List available Anthropic models."""
        import requests  # local import to stay consistent with rest of codebase

        if not self.api_key:
            return {
                "models": [
                    {
                        "id": self.DEFAULT_MODEL,
                        "name": self.DEFAULT_MODEL,
                        "display_name": self.DEFAULT_MODEL,
                        "key": self.DEFAULT_MODEL,
                    }
                ]
            }
        try:
            r = requests.get(
                _ANTHROPIC_MODELS_URL,
                headers=self._headers(),
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _logger.warning("AnthropicAdapter.get_models_from_api failed: %s", exc)
            return {
                "models": [
                    {
                        "id": self.DEFAULT_MODEL,
                        "name": self.DEFAULT_MODEL,
                        "display_name": self.DEFAULT_MODEL,
                        "key": self.DEFAULT_MODEL,
                    }
                ]
            }

        raw = data.get("data") or data.get("models") or []
        if not isinstance(raw, list):
            raw = []

        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("name")
            if not model_id:
                continue
            display = item.get("display_name") or item.get("name") or str(model_id)
            out.append(
                {
                    "id": model_id,
                    "name": model_id,
                    "display_name": display,
                    "key": model_id,
                }
            )

        if not out:
            out = [
                {
                    "id": self.DEFAULT_MODEL,
                    "name": self.DEFAULT_MODEL,
                    "display_name": self.DEFAULT_MODEL,
                    "key": self.DEFAULT_MODEL,
                }
            ]

        return {"models": out}

    # ------------------------------------------------------------------
    # Connection health check
    # ------------------------------------------------------------------

    def validate_connection(self) -> bool:
        """Return True if the API key is present and the models endpoint responds."""
        import requests  # local import

        if not self.api_key:
            return False
        try:
            r = requests.get(
                _ANTHROPIC_MODELS_URL,
                headers=self._headers(),
                timeout=10,
            )
            return r.status_code in (200, 206)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# ProviderManager aliases
# _camelize("anthropic") → "Anthropic" → looks for "AnthropicAdapter" first,
# then falls back to module-level "Adapter".
# ---------------------------------------------------------------------------
Adapter = AnthropicAdapter
