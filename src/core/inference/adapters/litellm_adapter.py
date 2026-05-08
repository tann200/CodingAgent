"""LiteLLM proxy adapter.

LiteLLM (https://litellm.ai) is a proxy that exposes 100+ models via a
single OpenAI-compatible API endpoint.  Users run it locally or deploy it
as a service, then point CodingAgent at it.

This adapter extends OpenAICompatibleAdapter with:
  - base_url from providers.json (required — no fixed default).
  - Optional API key from UserPrefs or environment (LITELLM_API_KEY).
  - No REQUIRES_API_KEY flag — LiteLLM can run without auth (local setup).
  - Model list from providers.json (or fetched via /models if omitted).
  - GAP-4: model_tier property — classifies the active model so context_builder
    and perception_node behave correctly for frontier vs local models.
  - GAP-4: thinking_utils integration — strips <think> blocks from responses
    produced by reasoning models (Qwen3, DeepSeek-R1, etc.) routed via LiteLLM.

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
from src.core.utils.strings import valid_str as _valid_str

_logger = logging.getLogger(__name__)

# GAP-4: lazy imports for model classification and thinking-token stripping.
# Wrapped in try/except so the adapter remains functional in minimal test envs.
try:
    from src.core.inference.model_tiers import (
        ModelTier,
        classify_model as _classify_model,
    )
except Exception:  # pragma: no cover
    _classify_model = None  # type: ignore[assignment]
    ModelTier = None  # type: ignore[assignment]

try:
    from src.core.inference.thinking_utils import (
        is_reasoning_model as _is_reasoning_model,
        strip_thinking as _strip_thinking,
    )
except Exception:  # pragma: no cover
    _is_reasoning_model = None  # type: ignore[assignment]
    _strip_thinking = None  # type: ignore[assignment]

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

        # Sanitize incoming models list to avoid leaking MagicMock placeholders
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
            resolved_models = [_DEFAULT_MODEL]
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
    # GAP-4: Model tier classification
    # ------------------------------------------------------------------

    @property
    def model_tier(self) -> str:
        """Return the ModelTier string for the active model (GAP-4).

        Used by context_builder.py and perception_node.py to apply tier-aware
        tool limits, prompt formats, and step limits.  Falls back to "medium"
        when classification is unavailable (e.g. in test environments).
        """
        if _classify_model is None:
            return "medium"
        try:
            model = (
                self.models[0]
                if isinstance(self.models, list) and self.models
                else self.default_model or ""
            )
            if not model:
                return "medium"
            ctx_window = int(getattr(self, "context_window", 0) or 0)
            tier = _classify_model(model, ctx_window)
            return tier.value
        except Exception:
            return "medium"

    # ------------------------------------------------------------------
    # GAP-4: Thinking-token stripping for reasoning models
    # ------------------------------------------------------------------

    def generate(self, messages, model=None, stream=False, timeout=None, **kwargs):
        """Call the parent generate() then strip <think> blocks if needed.

        LiteLLM routes requests to many providers, including local reasoning
        models (Qwen3, DeepSeek-R1) that emit raw <think>...</think> blocks.
        The base OpenAICompatibleAdapter already falls back to reasoning_content
        when content is empty, but does not strip inline <think> tags.  This
        override applies strip_thinking() on the way out so callers always
        receive clean text.
        """
        result = super().generate(
            messages, model=model, stream=stream, timeout=timeout, **kwargs
        )

        # Only strip when the active model is a known reasoning model and
        # thinking_utils is available.
        if _is_reasoning_model is None or _strip_thinking is None or stream:
            return result

        try:
            active_model = model or (
                self.models[0]
                if isinstance(self.models, list) and self.models
                else self.default_model or ""
            )
            if not _is_reasoning_model(active_model):
                return result

            choices = result.get("choices") or []
            if not choices:
                return result

            cleaned_choices = []
            for choice in choices:
                msg = choice.get("message", {})
                content = msg.get("content", "") or ""
                if "<think>" in content:
                    content = _strip_thinking(content)
                cleaned_choices.append(
                    {**choice, "message": {**msg, "content": content}}
                )
            return {**result, "choices": cleaned_choices}
        except Exception:
            return result

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
