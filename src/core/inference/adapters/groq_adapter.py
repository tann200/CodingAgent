"""Groq adapter.

Groq provides fast inference (LPU-based) for open-source models and is
fully OpenAI-compatible (https://api.groq.com/openai/v1).

This adapter extends OpenAICompatibleAdapter with:
  - Hardcoded BASE_URL — no base_url in providers.json needed.
  - API key loaded from UserPrefs (~/.coding_agent/prefs.json) or the
    GROQ_API_KEY environment variable.
  - Strict OpenAI payload hygiene: skips deprecated ``functions`` mirroring
    and never sends Ollama's non-standard ``think`` field (Groq rejects both
    with HTTP 400).
  - A curated default model list focused on fast open-weight coding models.
  - REQUIRES_API_KEY flag so the TUI can show the key entry UI.

Model tier mapping
------------------
  SMALL  : llama-3.1-8b-instant / openai/gpt-oss-20b — ultra-fast
  MEDIUM : llama-3.3-70b-versatile / qwen/qwen3.8-27b — strong coding + tools
  LARGE  : openai/gpt-oss-120b — high-capability open-weight reasoning

ProviderManager convention:
  type = "groq" in providers.json → imports this module →
  _camelize("groq") = "Groq" → looks for "GroqAdapter", then "Adapter".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.utils.strings import valid_str as _valid_str

_logger = logging.getLogger(__name__)

_MODELS_URL = "https://api.groq.com/openai/v1/models"

# Curated model list — ordered from fastest (SMALL) to most capable (MEDIUM/LARGE).
# Keep in sync with model_tiers.py classification patterns.
_DEFAULT_MODELS = [
    "llama-3.1-8b-instant",  # SMALL  — 8B, ~500 tok/s, best latency
    "llama-3.3-70b-versatile",  # MEDIUM — 70B, strong general capability
    "qwen/qwen3.8-27b",  # MEDIUM — Qwen 3.8 27B coding + native tools
    "openai/gpt-oss-120b",  # LARGE  — 120B open-weight reasoning + tools
    "openai/gpt-oss-20b",  # SMALL  — 20B fast open-weight + tools
]


class GroqAdapter(OpenAICompatibleAdapter):
    """Adapter for Groq's fast-inference LPU API."""

    BASE_URL = "https://api.groq.com/openai/v1"
    DEFAULT_MODEL = "llama-3.1-8b-instant"

    # Tells the TUI settings modal to show the API key entry UI.
    REQUIRES_API_KEY = True

    # Native OpenAI-style function calling is supported natively.
    supports_native_tools = True

    # Groq strictly enforces the modern OpenAI `tools` schema and rejects
    # legacy `functions` entries copied from `tools`.
    _skip_functions_compat = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "groq",
        small_model: Optional[str] = None,
        # Accept (and ignore) args that ProviderManager passes generically
        base_url: Optional[str] = None,  # always overridden by BASE_URL
        config_path: Optional[str] = None,
        **kwargs,
    ):
        # API key resolution: constructor arg > UserPrefs > GROQ_API_KEY env var.
        if not api_key:
            try:
                from src.core.user_prefs import UserPrefs

                api_key = UserPrefs.load().get_provider_key("groq")
            except Exception as exc:
                _logger.debug("groq_adapter: failed to load API key from UserPrefs: %s", exc)
        if not api_key:
            api_key = os.environ.get("GROQ_API_KEY") or None

        # Sanitize models list to avoid leaking MagicMock placeholders
        resolved_models: List[str] = []
        if models:
            for m in models:
                if isinstance(m, dict):
                    fid = m.get("id") or m.get("key") or m.get("name") or m.get("model")
                else:
                    fid = m
                if fid and _valid_str(fid):
                    resolved_models.append(str(fid).strip())
        if not resolved_models:
            resolved_models = list(_DEFAULT_MODELS)
        default_model = resolved_models[0] if resolved_models else self.DEFAULT_MODEL

        super().__init__(
            base_url=self.BASE_URL,
            api_key=api_key,
            default_model=default_model,
            models=resolved_models,
            name=name,
            **{k: v for k, v in kwargs.items() if k != "model"},
        )

        # Optional small_model override from providers.json
        if small_model:
            self.small_model = small_model

    # ------------------------------------------------------------------
    # Groq-specific headers — standard Bearer auth, no extra headers needed
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return super()._headers()

    def _apply_provider_flags(self, payload: Dict[str, Any]) -> None:
        """Keep Groq payloads strictly OpenAI-compatible.

        Groq rejects both legacy ``functions`` mirrored from ``tools``
        (``'functions.0.name' : property 'name' is missing``) and Ollama's
        non-standard ``think`` key (``property 'think' is unsupported``).
        """
        payload.pop("think", None)
        if "tools" in payload:
            payload.pop("functions", None)

    # ------------------------------------------------------------------
    # Model listing — Groq /models endpoint requires auth
    # ------------------------------------------------------------------

    def _models_endpoints(self) -> List[str]:
        return [_MODELS_URL]

    def get_models_from_api(self) -> Dict[str, Any]:
        """List available Groq models.

        The /models endpoint requires an API key.  Falls back to the
        curated default list when the key is not available or the request
        fails.
        """
        if not self.api_key:
            return self._default_model_list()

        try:
            r = requests.get(
                _MODELS_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _logger.warning("GroqAdapter.get_models_from_api failed: %s", exc)
            return self._default_model_list()

        raw = data.get("data") or data.get("models") or []
        if not isinstance(raw, list) or not raw:
            return self._default_model_list()

        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("name")
            if not model_id:
                continue
            display = item.get("name") or str(model_id)
            out.append(
                {
                    "id": model_id,
                    "name": str(model_id),
                    "display_name": display,
                    "key": str(model_id),
                }
            )

        return {"models": out} if out else self._default_model_list()

    def _default_model_list(self) -> Dict[str, Any]:
        return {
            "models": [
                {"id": m, "name": m, "display_name": m, "key": m}
                for m in _DEFAULT_MODELS
            ]
        }

    # ------------------------------------------------------------------
    # Connection health check — requires API key
    # ------------------------------------------------------------------

    def validate_connection(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = requests.get(
                _MODELS_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# ProviderManager aliases
# _camelize("groq") → "Groq" → looks for "GroqAdapter" first, then "Adapter".
# ---------------------------------------------------------------------------
Adapter = GroqAdapter
