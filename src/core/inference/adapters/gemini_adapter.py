"""Google Gemini adapter (via Google AI Studio OpenAI-compatible API).

Uses Google's official OpenAI-compatible endpoint:
    https://generativelanguage.googleapis.com/v1beta/openai

This adapter extends OpenAICompatibleAdapter with:
  - Hardcoded BASE_URL — no base_url in providers.json needed.
  - API key resolution order:
      1. Constructor argument
      2. UserPrefs (~/.coding_agent/prefs.json, key "gemini")
      3. GEMINI_API_KEY environment variable
      4. GOOGLE_API_KEY environment variable
  - Strict OpenAI payload hygiene: skips legacy ``functions`` mirroring and
    Ollama's non-standard ``think`` key.
  - Native tool calling enabled (``supports_native_tools = True``).
  - Curated default Gemini 2.5 models suited for fast coding and reasoning.
  - REQUIRES_API_KEY flag so the TUI can show the key entry UI.

ProviderManager convention:
  type = "gemini" in providers.json → imports this module →
  _camelize("gemini") = "Gemini" → looks for "GeminiAdapter", then "Adapter".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.utils.strings import valid_str as _valid_str

_logger = logging.getLogger(__name__)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_MODELS_URL = f"{_GEMINI_BASE_URL}/models"

_DEFAULT_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]

_NON_CHAT_SUBSTRINGS = (
    "embedding",
    "imagen",
    "-image",
    "veo",
    "tts",
    "aqa",
    "-live",
    "native-audio",
    "transcribe",
    "robotics",
    "computer-use",
    "nano-banana",
    "decision",
    "lyria",
    "genie",
)


class GeminiAdapter(OpenAICompatibleAdapter):
    """Adapter for Google Gemini via AI Studio's OpenAI-compatible API."""

    BASE_URL = _GEMINI_BASE_URL
    DEFAULT_MODEL = "gemini-3.6-flash"

    # Tells the TUI settings modal to show the API key entry UI.
    REQUIRES_API_KEY = True

    # Native OpenAI-style function calling is supported natively.
    supports_native_tools = True

    # Gemini rejects legacy `functions` mirrored from `tools`.
    _skip_functions_compat = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "gemini",
        small_model: Optional[str] = None,
        base_url: Optional[str] = None,
        config_path: Optional[str] = None,
        **kwargs,
    ):
        # API key resolution: constructor arg > UserPrefs > GEMINI_API_KEY > GOOGLE_API_KEY.
        if not api_key:
            try:
                from src.core.user_prefs import UserPrefs

                api_key = UserPrefs.load().get_provider_key("gemini")
            except Exception as exc:
                _logger.debug("gemini_adapter: failed to load API key from UserPrefs: %s", exc)
        if not api_key:
            api_key = (
                os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or None
            )

        resolved_models: List[str] = []
        if models:
            for m in models:
                if isinstance(m, dict):
                    fid = m.get("id") or m.get("key") or m.get("name") or m.get("model")
                else:
                    fid = m
                if fid and _valid_str(fid):
                    resolved_models.append(self._normalize_model_id(str(fid).strip()))
        if not resolved_models:
            resolved_models = list(_DEFAULT_MODELS)
        default_model = resolved_models[0] if resolved_models else self.DEFAULT_MODEL

        effective_base_url = (
            str(base_url).rstrip("/")
            if base_url and _valid_str(base_url)
            else self.BASE_URL
        )

        super().__init__(
            base_url=effective_base_url,
            api_key=api_key,
            default_model=default_model,
            models=resolved_models,
            name=name,
            **{k: v for k, v in kwargs.items() if k != "model"},
        )

        if small_model:
            self.small_model = self._normalize_model_id(str(small_model))

    @staticmethod
    def _normalize_model_id(model_id: str) -> str:
        """Strip Google's optional ``models/`` resource prefix."""
        if model_id.startswith("models/"):
            return model_id[len("models/") :]
        return model_id

    def _compose(self, path: str) -> Optional[str]:
        """Append *path* directly to the OpenAI-compat Gemini base URL."""
        if not self.base_url:
            return None
        return f"{str(self.base_url).rstrip('/')}/{path.lstrip('/')}"

    def _apply_provider_flags(self, payload: Dict[str, Any]) -> None:
        """Keep Gemini OpenAI-compat payloads clean (no legacy functions or Ollama think)."""
        payload.pop("think", None)
        if "tools" in payload:
            payload.pop("functions", None)

    def _models_endpoints(self) -> List[str]:
        ep = self._compose("models")
        return [ep] if ep else [_MODELS_URL]

    def get_models_from_api(self) -> Dict[str, Any]:
        """List available Gemini chat models, falling back to curated defaults."""
        if not self.api_key:
            return self._default_model_list()

        url = self._compose("models") or _MODELS_URL
        try:
            r = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _logger.warning("GeminiAdapter.get_models_from_api failed: %s", exc)
            return self._default_model_list()

        raw = data.get("data") or data.get("models") or []
        if not isinstance(raw, list) or not raw:
            return self._default_model_list()

        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get("name")
            if not raw_id:
                continue
            model_id = self._normalize_model_id(str(raw_id).strip())
            lower = model_id.lower()
            # Keep Gemini and Gemma chat/coding models; exclude media/audio/embedding endpoints
            if not (lower.startswith("gemini") or lower.startswith("gemma")):
                continue
            if any(skip in lower for skip in _NON_CHAT_SUBSTRINGS):
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            out.append(
                {
                    "id": model_id,
                    "name": model_id,
                    "display_name": str(item.get("display_name") or model_id),
                    "key": model_id,
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

    def validate_connection(self) -> bool:
        if not self.api_key:
            return False
        url = self._compose("models") or _MODELS_URL
        try:
            r = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False


# ProviderManager aliases
Adapter = GeminiAdapter
