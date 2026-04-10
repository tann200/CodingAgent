"""LiteLLM proxy adapter.

LiteLLM (https://litellm.ai) is a proxy that exposes 100+ models via a
single OpenAI-compatible API endpoint.  Users run it locally or deploy it
as a service, then point CodingAgent at it.

This adapter extends OpenAICompatibleAdapter with:
  - base_url from providers.json (required — no fixed default).
  - Optional API key from UserPrefs or environment (LITELLM_API_KEY).
  - No REQUIRES_API_KEY flag — LiteLLM can run without auth (local setup).
  - Model list from providers.json (or fetched via /models if omitted).

Configuration example in providers.json::

    {
        "name": "LiteLLM",
        "type": "litellm",
        "base_url": "http://localhost:4000",
        "models": ["gpt-4o", "claude-3.5-sonnet", "llama-3.1-8b"],
        "active": false
    }

ProviderManager convention:
  type = "litellm" in providers.json → imports this module →
  _camelize("litellm") = "Litellm" → looks for "LitellmAdapter", then "Adapter".
  Both aliases are provided.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:4000"
_DEFAULT_MODEL = "gpt-4o"


class LiteLLMAdapter(OpenAICompatibleAdapter):
    """Adapter for LiteLLM proxy — unlocks 100+ models via one integration."""

    # No hardcoded BASE_URL — must be configured in providers.json.
    # Local installs often run without auth; API key is optional.
    REQUIRES_API_KEY = False

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "litellm",
        small_model: Optional[str] = None,
        config_path: Optional[str] = None,
        **kwargs,
    ):
        # base_url: providers.json > default localhost
        resolved_url = base_url or _DEFAULT_BASE_URL

        # API key: constructor arg > UserPrefs > LITELLM_API_KEY env var.
        # LiteLLM can run without auth (common for local deploys) so a missing
        # key is not treated as an error.
        if not api_key:
            try:
                from src.core.user_prefs import UserPrefs

                api_key = UserPrefs.load().get_provider_key("litellm")
            except Exception:
                pass
        if not api_key:
            api_key = os.environ.get("LITELLM_API_KEY") or None

        resolved_models: List[str] = list(models) if models else [_DEFAULT_MODEL]
        default_model = resolved_models[0]

        super().__init__(
            base_url=resolved_url,
            api_key=api_key,
            default_model=default_model,
            models=resolved_models,
            name=name,
            **{k: v for k, v in kwargs.items() if k != "model"},
        )

        if small_model:
            self.small_model = small_model

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    def get_models_from_api(self) -> Dict[str, Any]:
        """List models from the LiteLLM /models endpoint.

        Falls back to the configured model list when the proxy is unreachable.
        """
        import requests

        url = self._compose("models")
        if not url:
            return self._default_model_list()

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _logger.warning("LiteLLMAdapter.get_models_from_api failed: %s", exc)
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
                {"id": m, "name": m, "display_name": m, "key": m} for m in self.models
            ]
        }

    # ------------------------------------------------------------------
    # Connection health check
    # ------------------------------------------------------------------

    def validate_connection(self) -> bool:
        """Returns True when the LiteLLM proxy is reachable at base_url."""
        import requests

        url = self._compose("models")
        if not url:
            return False

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            r = requests.get(url, headers=headers, timeout=5)
            return r.status_code < 500
        except Exception:
            return False


# ---------------------------------------------------------------------------
# ProviderManager aliases
# _camelize("litellm") → "Litellm" → looks for "LitellmAdapter" first,
# then falls back to module-level "Adapter".
# ---------------------------------------------------------------------------
Adapter = LiteLLMAdapter
LitellmAdapter = LiteLLMAdapter
