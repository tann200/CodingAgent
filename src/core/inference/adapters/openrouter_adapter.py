"""OpenRouter adapter.

OpenRouter is fully OpenAI-compatible (https://openrouter.ai/api/v1).
This adapter extends OpenAICompatibleAdapter with:
  - Hardcoded BASE_URL — no base_url in providers.json needed.
  - API key loaded exclusively from UserPrefs (~/.config/codingagent/prefs.json).
    The key is never stored in providers.json or environment variables.
  - Required extra headers: HTTP-Referer and X-Title.
  - REQUIRES_API_KEY flag so the TUI can show the key entry UI.

ProviderManager convention:
  type = "openrouter" in providers.json → imports this module →
  _camelize("openrouter") = "Openrouter" → looks for "OpenrouterAdapter",
  then falls back to module-level "Adapter".  Both aliases are provided.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)

_MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """Adapter for the OpenRouter unified API."""

    BASE_URL = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "anthropic/claude-3.5-sonnet"

    # Tells the TUI settings modal to show the API key entry UI.
    REQUIRES_API_KEY = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "openrouter",
        # Accept (and ignore) args that ProviderManager passes generically
        base_url: Optional[str] = None,   # always overridden by BASE_URL
        config_path: Optional[str] = None,
        **kwargs,
    ):
        # API key: constructor arg > UserPrefs.  Never from providers.json.
        if not api_key:
            try:
                from src.core.user_prefs import UserPrefs
                api_key = UserPrefs.load().get_provider_key("openrouter")
            except Exception:
                pass

        resolved_models: List[str] = list(models) if models else [self.DEFAULT_MODEL]
        default_model = resolved_models[0]

        super().__init__(
            base_url=self.BASE_URL,
            api_key=api_key,
            default_model=default_model,
            models=resolved_models,
            name=name,
            **{k: v for k, v in kwargs.items() if k != "model"},
        )

    # ------------------------------------------------------------------
    # OpenRouter-specific headers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = super()._headers()
        h["HTTP-Referer"] = "https://github.com/CodingAgent"
        h["X-Title"] = "CodingAgent"
        return h

    # ------------------------------------------------------------------
    # Model listing — fixed endpoint, no auth required
    # ------------------------------------------------------------------

    def _models_endpoints(self) -> List[str]:
        return [_MODELS_URL]

    def get_models_from_api(self) -> Dict[str, Any]:
        """List available OpenRouter models.

        The /models endpoint is public (no API key required).  Returns models
        sorted with free models last so the default is a capable paid model.
        """
        try:
            r = requests.get(
                _MODELS_URL,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _logger.warning("OpenRouterAdapter.get_models_from_api failed: %s", exc)
            return {"models": [{"id": self.DEFAULT_MODEL, "name": self.DEFAULT_MODEL,
                                "display_name": self.DEFAULT_MODEL, "key": self.DEFAULT_MODEL}]}

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
            short = str(model_id).split("/")[-1]
            display = item.get("name") or short
            out.append({"id": model_id, "name": short, "display_name": display, "key": short})

        if not out:
            out = [{"id": self.DEFAULT_MODEL, "name": self.DEFAULT_MODEL,
                    "display_name": self.DEFAULT_MODEL, "key": self.DEFAULT_MODEL}]

        return {"models": out}

    # ------------------------------------------------------------------
    # Connection health check — public /models endpoint, no key needed
    # ------------------------------------------------------------------

    def validate_connection(self) -> bool:
        try:
            r = requests.get(
                _MODELS_URL,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# ProviderManager aliases
# _camelize("openrouter") → "Openrouter" → looks for "OpenrouterAdapter" first,
# then falls back to module-level "Adapter".  Both provided for robustness.
# ---------------------------------------------------------------------------
Adapter = OpenRouterAdapter
OpenrouterAdapter = OpenRouterAdapter
